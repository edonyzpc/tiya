import path from "node:path";

import type { DesktopPaths, ServiceStatus } from "../shared/api";

export interface ChildProcessLike {
  exitCode: number | null;
  signalCode: NodeJS.Signals | null;
  kill(signal?: NodeJS.Signals): void;
}

export interface SupervisorLaunchSpec {
  command: string;
  args: string[];
  options: {
    cwd: string;
    detached: false;
    stdio: "ignore";
    env: Record<string, string>;
  };
}

export interface SupervisorRuntimeLayout {
  supervisorDir: string;
  socketPath: string;
  legacySupervisorDir: string;
  legacySocketPath: string;
}

export function childIsAlive(child: ChildProcessLike | null): boolean {
  return Boolean(child && child.exitCode === null && child.signalCode === null);
}

export function buildSupervisorRuntimeLayout(runtimeRoot: string): SupervisorRuntimeLayout {
  return {
    supervisorDir: path.join(runtimeRoot, "supervisor"),
    socketPath: path.join(runtimeRoot, "supervisor", "tiya.sock"),
    legacySupervisorDir: path.join(runtimeRoot, "daemon"),
    legacySocketPath: path.join(runtimeRoot, "daemon", "tiya.sock"),
  };
}

export async function migrateLegacySupervisorRuntime(params: {
  layout: SupervisorRuntimeLayout;
  pathExists: (targetPath: string) => Promise<boolean>;
  renamePath: (sourcePath: string, targetPath: string) => Promise<void>;
  mkdirPath: (targetPath: string) => Promise<void>;
}): Promise<void> {
  const { layout, pathExists, renamePath, mkdirPath } = params;
  if (!(await pathExists(layout.legacySupervisorDir))) {
    await mkdirPath(layout.supervisorDir);
    return;
  }
  if (await pathExists(layout.supervisorDir)) {
    return;
  }
  try {
    await renamePath(layout.legacySupervisorDir, layout.supervisorDir);
  } catch {
    // Ignore; the runtime can still start with fresh supervisor paths.
    await mkdirPath(layout.supervisorDir);
  }
}

export async function waitForChildExit(
  child: ChildProcessLike,
  sleep: (ms: number) => Promise<void>,
  timeoutMs: number
): Promise<boolean> {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (!childIsAlive(child)) {
      return true;
    }
    await sleep(100);
  }
  return !childIsAlive(child);
}

async function waitForStatus(params: {
  socketPath: string;
  readStatus: (socketPath: string) => Promise<ServiceStatus | null>;
  predicate: (status: ServiceStatus) => boolean;
  sleep: (ms: number) => Promise<void>;
  timeoutMs: number;
}): Promise<ServiceStatus | null> {
  const { socketPath, readStatus, predicate, sleep, timeoutMs } = params;
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const status = await readStatus(socketPath);
    if (status && predicate(status)) {
      return status;
    }
    await sleep(200);
  }
  return null;
}

export async function shutdownOwnedSupervisor(params: {
  socketPath: string;
  supervisorPid?: number;
  rpcCall: (method: string, params: Record<string, unknown>, socketPath: string) => Promise<Record<string, unknown>>;
  readStatus: (socketPath: string) => Promise<ServiceStatus | null>;
  sleep: (ms: number) => Promise<void>;
  killProcess: (pid: number, signal: NodeJS.Signals) => void;
  softTimeoutMs?: number;
  hardTimeoutMs?: number;
}): Promise<void> {
  const {
    socketPath,
    supervisorPid,
    rpcCall,
    readStatus,
    sleep,
    killProcess,
    softTimeoutMs = 1200,
    hardTimeoutMs = 3000,
  } = params;
  try {
    await rpcCall("service.stop", {}, socketPath);
  } catch {
    // Ignore; shutdown still tries to tear the supervisor down.
  }
  try {
    await rpcCall("supervisor.shutdown", {}, socketPath);
  } catch {
    // Ignore; we may need to fall back to killing the process.
  }

  const softDeadline = Date.now() + softTimeoutMs;
  while (Date.now() < softDeadline) {
    const status = await readStatus(socketPath);
    if (!status) {
      return;
    }
    await sleep(150);
  }

  if (typeof supervisorPid === "number" && supervisorPid > 1) {
    try {
      killProcess(supervisorPid, "SIGTERM");
    } catch {
      // Process may already be gone.
    }
  }

  const hardDeadline = Date.now() + hardTimeoutMs;
  while (Date.now() < hardDeadline) {
    const status = await readStatus(socketPath);
    if (!status) {
      return;
    }
    await sleep(150);
  }
}

export function buildSupervisorLaunchSpec(params: {
  packaged: boolean;
  paths: Pick<DesktopPaths, "backendRoot" | "projectRoot">;
  env: Record<string, string>;
  pythonExecutable: string;
  packagedSupervisorExecutable: string;
  packagedWorkerExecutable: string;
  sourceModule: string;
}): SupervisorLaunchSpec {
  const {
    packaged,
    paths,
    env,
    pythonExecutable,
    packagedSupervisorExecutable,
    packagedWorkerExecutable,
    sourceModule,
  } = params;

  if (packaged) {
    return {
      command: packagedSupervisorExecutable,
      args: [],
      options: {
        cwd: paths.backendRoot,
        detached: false,
        stdio: "ignore",
        env: {
          ...env,
          TIYA_WORKER_EXECUTABLE: packagedWorkerExecutable,
        },
      },
    };
  }

  return {
    command: pythonExecutable,
    args: ["-m", sourceModule],
    options: {
      cwd: paths.projectRoot,
      detached: false,
      stdio: "ignore",
      env,
    },
  };
}

export async function ensureOwnedSupervisor(params: {
  paths: Pick<DesktopPaths, "socketPath">;
  desktopPid: number;
  currentChild: ChildProcessLike | null;
  readStatus: (socketPath: string) => Promise<ServiceStatus | null>;
  spawnChild: () => Promise<ChildProcessLike> | ChildProcessLike;
  shutdownOwnedSupervisor: (supervisorPid?: number) => Promise<void>;
  sleep: (ms: number) => Promise<void>;
  timeoutMs?: number;
}): Promise<ChildProcessLike | null> {
  const {
    paths,
    desktopPid,
    currentChild,
    readStatus,
    spawnChild,
    shutdownOwnedSupervisor: shutdownSupervisor,
    sleep,
    timeoutMs = 8000,
  } = params;

  const existingStatus = await readStatus(paths.socketPath);
  if (existingStatus?.desktopPid === desktopPid) {
    return currentChild;
  }
  if (existingStatus) {
    await shutdownSupervisor(existingStatus.supervisorPid);
  }

  if (childIsAlive(currentChild)) {
    return currentChild;
  }

  const nextChild = await spawnChild();
  const readyStatus = await waitForStatus({
    socketPath: paths.socketPath,
    readStatus,
    predicate: (status) => status.desktopPid === desktopPid,
    sleep,
    timeoutMs,
  });
  if (readyStatus) {
    return nextChild;
  }

  if (childIsAlive(nextChild)) {
    nextChild.kill("SIGTERM");
    await waitForChildExit(nextChild, sleep, 1000);
  }
  throw new Error("Timed out while waiting for the desktop-owned supervisor to boot");
}
