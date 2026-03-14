import { access } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

const desktopRoot = path.resolve(process.cwd());
const electronRoot = path.join(desktopRoot, "node_modules", "electron");
const electronDist = path.join(electronRoot, "dist");
const platformBinary = process.platform === "darwin"
  ? path.join(electronDist, "Electron.app", "Contents", "MacOS", "Electron")
  : path.join(electronDist, process.platform === "win32" ? "electron.exe" : "electron");

async function exists(targetPath) {
  try {
    await access(targetPath);
    return true;
  } catch {
    return false;
  }
}

if (await exists(platformBinary)) {
  process.exit(0);
}

await new Promise((resolve, reject) => {
  const child = spawn("node", [path.join(electronRoot, "install.js")], {
    cwd: desktopRoot,
    stdio: "inherit",
    env: (() => {
      const env = {
        ...process.env,
        electron_config_cache: path.join(desktopRoot, ".cache", "electron")
      };
      delete env.TG_PROXY_URL;
      delete env.HTTP_PROXY;
      delete env.HTTPS_PROXY;
      delete env.ALL_PROXY;
      delete env.http_proxy;
      delete env.https_proxy;
      delete env.all_proxy;
      return env;
    })()
  });

  child.on("error", reject);
  child.on("exit", (code) => {
    if (code === 0) {
      resolve();
      return;
    }
    reject(new Error(`Electron runtime preparation failed with exit code ${code ?? "unknown"}`));
  });
});
