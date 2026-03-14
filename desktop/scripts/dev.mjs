import { spawn } from "node:child_process";
import { watch } from "node:fs";
import { access } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const desktopRoot = path.resolve(process.cwd());
const bin = (name) => path.join(desktopRoot, "node_modules", ".bin", name);

function run(command, args, extraEnv = {}) {
  return spawn(command, args, {
    cwd: desktopRoot,
    stdio: "inherit",
    env: {
      ...process.env,
      ...extraEnv
    }
  });
}

async function waitFor(filePath, timeoutMs = 30000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    try {
      await access(filePath);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw new Error(`Timed out waiting for ${filePath}`);
}

const tsc = run(bin("tsc"), ["-p", "tsconfig.electron.json", "--watch", "--preserveWatchOutput"]);
const vite = run(bin("vite"), ["--host", "127.0.0.1", "--port", "5173", "--strictPort"]);

const cleanup = () => {
  tsc.kill("SIGTERM");
  vite.kill("SIGTERM");
  if (electron) {
    electron.kill("SIGTERM");
  }
};

let electron;
let restartTimer = null;

async function waitForRenderer(timeoutMs = 30000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const response = await fetch("http://127.0.0.1:5173");
      if (response.ok) {
        return;
      }
    } catch {
      // Keep polling until Vite is ready.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("Timed out waiting for the renderer dev server");
}

function startElectron() {
  if (electron) {
    electron.kill("SIGTERM");
  }
  electron = run(bin("electron"), ["dist-electron/electron/main.js"], {
    TIYA_DESKTOP_DEV_SERVER_URL: "http://127.0.0.1:5173"
  });
}

function scheduleRestart() {
  if (restartTimer) {
    clearTimeout(restartTimer);
  }
  restartTimer = setTimeout(() => {
    startElectron();
  }, 180);
}

process.on("SIGINT", () => {
  cleanup();
  process.exit(0);
});
process.on("SIGTERM", () => {
  cleanup();
  process.exit(0);
});

await Promise.all([
  waitFor(path.join(desktopRoot, "dist-electron", "electron", "main.js")),
  waitForRenderer()
]);

startElectron();

watch(path.join(desktopRoot, "dist-electron", "electron"), (_eventType, filename) => {
  if (!filename || !filename.endsWith(".js")) {
    return;
  }
  scheduleRestart();
});
