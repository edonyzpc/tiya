import { promises as fs } from "node:fs";
import path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { app } from "electron";

import type { DesktopPaths, ServiceStatus } from "../shared/api";

import { callRpc, RpcClientError } from "./rpc";
import {
  buildSupervisorLaunchSpec,
  buildSupervisorRuntimeLayout,
  childIsAlive,
  ensureOwnedSupervisor,
  migrateLegacySupervisorRuntime,
  resolveDesktopRuntimeRoot,
  shutdownOwnedSupervisor,
  waitForChildExit,
} from "./supervisor_runtime_core";

export type RuntimeEnvironment = {
  ENV_FILE: string;
  TIYA_HOME: string;
  PYTHONUNBUFFERED: string;
  TIYA_DESKTOP_PID: string;
  TIYA_SECRET_STORE_BACKEND?: string;
};

function executableName(base: string): string {
  return process.platform === "win32" ? `${base}.exe` : base;
}

function fileExists(targetPath: string): Promise<boolean> {
  return fs.access(targetPath).then(
    () => true,
    () => false
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function readStatus(paths: DesktopPaths): Promise<ServiceStatus | null> {
  try {
    return (await callRpc("service.status", {}, paths.socketPath)) as unknown as ServiceStatus;
  } catch {
    return null;
  }
}

async function readStatusAt(socketPath: string): Promise<ServiceStatus | null> {
  try {
    return (await callRpc("service.status", {}, socketPath)) as unknown as ServiceStatus;
  } catch {
    return null;
  }
}

async function findExistingSupervisor(paths: DesktopPaths): Promise<{ status: ServiceStatus; socketPath: string } | null> {
  const layout = buildSupervisorRuntimeLayout(paths.runtimeRoot);
  for (const socketPath of [layout.socketPath, layout.legacySocketPath]) {
    const status = await readStatusAt(socketPath);
    if (status) {
      return { status, socketPath };
    }
  }
  return null;
}

async function waitForStatus(
  paths: DesktopPaths,
  predicate: (status: ServiceStatus) => boolean,
  timeoutMs: number
): Promise<ServiceStatus | null> {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const status = await readStatus(paths);
    if (status && predicate(status)) {
      return status;
    }
    await sleep(200);
  }
  return null;
}

export function resolveDesktopPaths(): DesktopPaths {
  const userData = app.getPath("userData");
  const runtimeRoot = resolveDesktopRuntimeRoot({
    configuredHome: process.env.TIYA_HOME,
    homeDir: app.getPath("home"),
  });
  const layout = buildSupervisorRuntimeLayout(runtimeRoot);
  const backendRoot = app.isPackaged
    ? path.join(process.resourcesPath, "tiya-backend")
    : path.resolve(__dirname, "..", "..", "..");
  return {
    projectRoot: path.resolve(__dirname, "..", "..", ".."),
    backendRoot,
    userData,
    envFile: path.join(userData, "config", "tiya.env"),
    runtimeRoot,
    socketPath: layout.socketPath
  };
}

export async function ensureRuntimeDirectories(paths: DesktopPaths): Promise<void> {
  const layout = buildSupervisorRuntimeLayout(paths.runtimeRoot);
  await fs.mkdir(path.dirname(paths.envFile), { recursive: true });
  await fs.mkdir(paths.runtimeRoot, { recursive: true });
  await fs.mkdir(layout.supervisorDir, { recursive: true });
}

async function migrateLegacyRuntimeIfIdle(paths: DesktopPaths): Promise<void> {
  const layout = buildSupervisorRuntimeLayout(paths.runtimeRoot);
  await migrateLegacySupervisorRuntime({
    layout,
    pathExists: fileExists,
    renamePath: (sourcePath, targetPath) => fs.rename(sourcePath, targetPath),
    mkdirPath: async (targetPath) => {
      await fs.mkdir(targetPath, { recursive: true });
    },
  });
}

export function resolveRuntimeEnvironment(paths: DesktopPaths): RuntimeEnvironment {
  const overlay: RuntimeEnvironment = {
    ENV_FILE: paths.envFile,
    TIYA_HOME: paths.runtimeRoot,
    PYTHONUNBUFFERED: "1",
    TIYA_DESKTOP_PID: String(process.pid)
  };
  if (process.env.TIYA_SECRET_STORE_BACKEND) {
    overlay.TIYA_SECRET_STORE_BACKEND = process.env.TIYA_SECRET_STORE_BACKEND;
  }
  return overlay;
}

export async function resolvePythonExecutable(projectRoot: string): Promise<string> {
  const configured = process.env.TIYA_DESKTOP_PYTHON;
  if (configured) {
    return configured;
  }

  const candidates = [
    path.join(projectRoot, ".venv", "bin", "python"),
    "/usr/bin/python3",
    "/usr/local/bin/python3"
  ];
  for (const candidate of candidates) {
    if (await fileExists(candidate)) {
      return candidate;
    }
  }
  return "python3";
}

function resolvePackagedExecutable(paths: DesktopPaths, name: "tiya-supervisor" | "tiya-worker"): string {
  return path.join(paths.backendRoot, name, executableName(name));
}

async function spawnSupervisor(paths: DesktopPaths): Promise<ChildProcess> {
  const overlay = resolveRuntimeEnvironment(paths);
  const env = {
    ...process.env,
    ...overlay
  };
  const spec = buildSupervisorLaunchSpec({
    packaged: app.isPackaged,
    paths,
    env,
    pythonExecutable: await resolvePythonExecutable(paths.projectRoot),
    packagedSupervisorExecutable: resolvePackagedExecutable(paths, "tiya-supervisor"),
    packagedWorkerExecutable: resolvePackagedExecutable(paths, "tiya-worker"),
    sourceModule: "src.supervisor"
  });
  return spawn(spec.command, spec.args, spec.options);
}

export async function ensureSupervisor(paths: DesktopPaths, child: ChildProcess | null): Promise<ChildProcess | null> {
  const existing = await findExistingSupervisor(paths);
  if (existing && existing.socketPath !== paths.socketPath) {
    await shutdownOwnedSupervisor({
      socketPath: existing.socketPath,
      supervisorPid: existing.status.supervisorPid,
      rpcCall: callRpc,
      readStatus: readStatusAt,
      sleep,
      killProcess: (pid, signal) => {
        process.kill(pid, signal);
      }
    });
  }
  if (!existing || existing.socketPath !== paths.socketPath) {
    await migrateLegacyRuntimeIfIdle(paths);
  }
  await ensureRuntimeDirectories(paths);
  try {
    return (await ensureOwnedSupervisor({
      paths,
      desktopPid: process.pid,
      currentChild: child,
      readStatus: (socketPath) => readStatus({ ...paths, socketPath }),
      spawnChild: () => spawnSupervisor(paths),
      shutdownOwnedSupervisor: (supervisorPid) =>
        shutdownOwnedSupervisor({
          socketPath: paths.socketPath,
          supervisorPid,
          rpcCall: callRpc,
          readStatus: (socketPath) => readStatus({ ...paths, socketPath }),
          sleep,
          killProcess: (pid, signal) => {
            process.kill(pid, signal);
          }
        }),
      sleep,
    })) as ChildProcess | null;
  } catch (error) {
    throw new RpcClientError(error instanceof Error ? error.message : String(error));
  }
}

export async function shutdownSupervisor(paths: DesktopPaths, child: ChildProcess | null): Promise<void> {
  const existing = await findExistingSupervisor(paths);
  if (existing && existing.status.desktopPid === process.pid) {
    await shutdownOwnedSupervisor({
      socketPath: existing.socketPath,
      supervisorPid: existing.status.supervisorPid,
      rpcCall: callRpc,
      readStatus: readStatusAt,
      sleep,
      killProcess: (pid, signal) => {
        process.kill(pid, signal);
      }
    });
  }

  if (!child || !childIsAlive(child)) {
    return;
  }

  child.kill("SIGTERM");
  if (await waitForChildExit(child, sleep, 5000)) {
    return;
  }
  child.kill("SIGKILL");
  await waitForChildExit(child, sleep, 1000);
}
