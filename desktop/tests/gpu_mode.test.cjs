const test = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_DESKTOP_GPU_MODE,
  applyDesktopGpuMode,
  parseEnvLine,
  readDesktopGpuModeFromEnvFile,
  resolveDesktopGpuMode,
} = require("../dist-electron/electron/gpu_mode.js");

test("parseEnvLine handles exports, quotes, and inline comments", () => {
  assert.deepEqual(parseEnvLine('export TIYA_DESKTOP_GPU_MODE="enabled"'), ["TIYA_DESKTOP_GPU_MODE", "enabled"]);
  assert.deepEqual(parseEnvLine("TIYA_DESKTOP_GPU_MODE=disabled # comment"), ["TIYA_DESKTOP_GPU_MODE", "disabled"]);
  assert.equal(parseEnvLine("  # comment"), null);
});

test("readDesktopGpuModeFromEnvFile falls back to disabled when missing", () => {
  const mode = readDesktopGpuModeFromEnvFile("/tmp/missing-tiya.env", () => {
    throw new Error("missing");
  });

  assert.equal(mode, DEFAULT_DESKTOP_GPU_MODE);
});

test("resolveDesktopGpuMode prefers explicit process environment over file contents", () => {
  const mode = resolveDesktopGpuMode({
    processEnv: { TIYA_DESKTOP_GPU_MODE: "enabled" },
    envFilePath: "/tmp/tiya.env",
    reader: () => "TIYA_DESKTOP_GPU_MODE=disabled\n",
  });

  assert.equal(mode, "enabled");
});

test("resolveDesktopGpuMode reads tiya.env when process environment is unset", () => {
  const mode = resolveDesktopGpuMode({
    processEnv: {},
    envFilePath: "/tmp/tiya.env",
    reader: () => "DEFAULT_PROVIDER=codex\nTIYA_DESKTOP_GPU_MODE=enabled\n",
  });

  assert.equal(mode, "enabled");
});

test("applyDesktopGpuMode disables hardware acceleration only in disabled mode", () => {
  const calls = [];
  const app = {
    disableHardwareAcceleration: () => {
      calls.push("disable");
    },
    commandLine: {
      appendSwitch: (name) => {
        calls.push(`switch:${name}`);
      },
    },
  };

  applyDesktopGpuMode(app, "enabled");
  assert.deepEqual(calls, []);

  applyDesktopGpuMode(app, "disabled");
  assert.deepEqual(calls, ["disable", "switch:disable-gpu"]);
});
