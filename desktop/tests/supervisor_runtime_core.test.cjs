const test = require("node:test");
const assert = require("node:assert/strict");

const {
  buildSupervisorLaunchSpec,
  resolveDesktopRuntimeRoot,
  buildSupervisorRuntimeLayout,
  childIsAlive,
  ensureOwnedSupervisor,
  migrateLegacySupervisorRuntime,
  shutdownOwnedSupervisor,
} = require("../dist-electron/electron/supervisor_runtime_core.js");

test("buildSupervisorRuntimeLayout uses supervisor paths and keeps legacy daemon fallback", () => {
  const layout = buildSupervisorRuntimeLayout("/tmp/tiya-runtime");

  assert.equal(layout.supervisorDir, "/tmp/tiya-runtime/supervisor");
  assert.equal(layout.socketPath, "/tmp/tiya-runtime/supervisor/tiya.sock");
  assert.equal(layout.legacySupervisorDir, "/tmp/tiya-runtime/daemon");
  assert.equal(layout.legacySocketPath, "/tmp/tiya-runtime/daemon/tiya.sock");
});

test("resolveDesktopRuntimeRoot defaults to ~/.tiya", () => {
  const runtimeRoot = resolveDesktopRuntimeRoot({
    homeDir: "/Users/demo",
  });

  assert.equal(runtimeRoot, "/Users/demo/.tiya");
});

test("resolveDesktopRuntimeRoot expands explicit TIYA_HOME", () => {
  const runtimeRoot = resolveDesktopRuntimeRoot({
    configuredHome: "~/custom-tiya",
    homeDir: "/Users/demo",
  });

  assert.equal(runtimeRoot, "/Users/demo/custom-tiya");
});

test("migrateLegacySupervisorRuntime renames the legacy daemon directory when needed", async () => {
  const layout = buildSupervisorRuntimeLayout("/tmp/tiya-runtime");
  const renamed = [];

  await migrateLegacySupervisorRuntime({
    layout,
    pathExists: async (targetPath) => targetPath === layout.legacySupervisorDir,
    renamePath: async (sourcePath, targetPath) => {
      renamed.push({ sourcePath, targetPath });
    },
    mkdirPath: async () => undefined,
  });

  assert.deepEqual(renamed, [{ sourcePath: layout.legacySupervisorDir, targetPath: layout.supervisorDir }]);
});

test("buildSupervisorLaunchSpec keeps packaged supervisor attached to desktop", () => {
  const spec = buildSupervisorLaunchSpec({
    packaged: true,
    paths: {
      backendRoot: "/opt/tiya/resources/tiya-backend",
      projectRoot: "/mnt/code/tiya",
    },
    env: {
      ENV_FILE: "/tmp/tiya.env",
      TIYA_HOME: "/tmp/runtime",
    },
    pythonExecutable: "/usr/bin/python3",
    packagedSupervisorExecutable: "/opt/tiya/resources/tiya-backend/tiya-supervisor/tiya-supervisor",
    packagedWorkerExecutable: "/opt/tiya/resources/tiya-backend/tiya-worker/tiya-worker",
    sourceModule: "src.supervisor",
  });

  assert.equal(spec.command, "/opt/tiya/resources/tiya-backend/tiya-supervisor/tiya-supervisor");
  assert.deepEqual(spec.args, []);
  assert.equal(spec.options.cwd, "/opt/tiya/resources/tiya-backend");
  assert.equal(spec.options.detached, false);
  assert.equal(spec.options.stdio, "ignore");
  assert.equal(spec.options.env.TIYA_WORKER_EXECUTABLE, "/opt/tiya/resources/tiya-backend/tiya-worker/tiya-worker");
});

test("buildSupervisorLaunchSpec uses Python module entry in development", () => {
  const spec = buildSupervisorLaunchSpec({
    packaged: false,
    paths: {
      backendRoot: "/opt/tiya/resources/tiya-backend",
      projectRoot: "/mnt/code/tiya",
    },
    env: {
      ENV_FILE: "/tmp/tiya.env",
      TIYA_HOME: "/tmp/runtime",
    },
    pythonExecutable: "/mnt/code/tiya/.venv/bin/python",
    packagedSupervisorExecutable: "/unused",
    packagedWorkerExecutable: "/unused-worker",
    sourceModule: "src.supervisor",
  });

  assert.equal(spec.command, "/mnt/code/tiya/.venv/bin/python");
  assert.deepEqual(spec.args, ["-m", "src.supervisor"]);
  assert.equal(spec.options.cwd, "/mnt/code/tiya");
  assert.equal(spec.options.detached, false);
  assert.equal(spec.options.stdio, "ignore");
});

test("childIsAlive only returns true for live attached children", () => {
  assert.equal(childIsAlive(null), false);
  assert.equal(childIsAlive({ exitCode: 0, signalCode: null, kill: () => undefined }), false);
  assert.equal(childIsAlive({ exitCode: null, signalCode: "SIGTERM", kill: () => undefined }), false);
  assert.equal(childIsAlive({ exitCode: null, signalCode: null, kill: () => undefined }), true);
});

test("ensureOwnedSupervisor returns current child when desktop already owns the supervisor", async () => {
  const currentChild = { exitCode: null, signalCode: null, kill: () => undefined };
  let spawnCalled = 0;

  const result = await ensureOwnedSupervisor({
    paths: { socketPath: "/tmp/tiya.sock" },
    desktopPid: 123,
    currentChild,
    readStatus: async () => ({ desktopPid: 123, supervisorPid: 999, workerPid: null, phase: "stopped" }),
    spawnChild: async () => {
      spawnCalled += 1;
      return currentChild;
    },
    shutdownOwnedSupervisor: async () => undefined,
    sleep: async () => undefined,
    timeoutMs: 10,
  });

  assert.equal(result, currentChild);
  assert.equal(spawnCalled, 0);
});

test("ensureOwnedSupervisor tears down a foreign supervisor before spawning a new owned one", async () => {
  const spawnedChild = {
    exitCode: null,
    signalCode: null,
    kill: () => undefined,
  };
  const shutdownCalls = [];
  const statusQueue = [
    { desktopPid: 777, supervisorPid: 41, workerPid: null, phase: "running" },
    { desktopPid: 123, supervisorPid: 42, workerPid: null, phase: "starting" },
  ];

  const result = await ensureOwnedSupervisor({
    paths: { socketPath: "/tmp/tiya.sock" },
    desktopPid: 123,
    currentChild: null,
    readStatus: async () => statusQueue.shift() ?? null,
    spawnChild: async () => spawnedChild,
    shutdownOwnedSupervisor: async (pid) => {
      shutdownCalls.push(pid);
    },
    sleep: async () => undefined,
    timeoutMs: 10,
  });

  assert.equal(result, spawnedChild);
  assert.deepEqual(shutdownCalls, [41]);
});

test("ensureOwnedSupervisor terminates the child when boot times out", async () => {
  const killSignals = [];
  const spawnedChild = {
    exitCode: null,
    signalCode: null,
    kill: (signal) => {
      killSignals.push(signal);
      spawnedChild.exitCode = 0;
    },
  };

  await assert.rejects(
    ensureOwnedSupervisor({
      paths: { socketPath: "/tmp/tiya.sock" },
      desktopPid: 123,
      currentChild: null,
      readStatus: async () => null,
      spawnChild: async () => spawnedChild,
      shutdownOwnedSupervisor: async () => undefined,
      sleep: async () => undefined,
      timeoutMs: 10,
    }),
    /Timed out while waiting for the desktop-owned supervisor to boot/
  );

  assert.deepEqual(killSignals, ["SIGTERM"]);
});

test("shutdownOwnedSupervisor stops the worker and escalates to SIGTERM when the socket stays up", async () => {
  const rpcCalls = [];
  const killSignals = [];

  await shutdownOwnedSupervisor({
    socketPath: "/tmp/tiya.sock",
    supervisorPid: 4242,
    rpcCall: async (method, params, socketPath) => {
      rpcCalls.push({ method, params, socketPath });
      return {};
    },
    readStatus: async () => ({ desktopPid: 1, supervisorPid: 4242, workerPid: null, phase: "stopped" }),
    sleep: async () => undefined,
    killProcess: (pid, signal) => {
      killSignals.push({ pid, signal });
    },
    softTimeoutMs: 1,
    hardTimeoutMs: 1,
  });

  assert.deepEqual(rpcCalls.map((call) => call.method), ["service.stop", "supervisor.shutdown"]);
  assert.deepEqual(killSignals, [{ pid: 4242, signal: "SIGTERM" }]);
});
