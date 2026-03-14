import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";

const desktopRoot = path.resolve(process.cwd());
const builderTargets = process.argv.slice(2);

if (builderTargets.length === 0) {
  console.error("Usage: node scripts/package-mac.mjs <target...>");
  process.exit(1);
}

const sharedEnv = {
  ...process.env,
  TIYA_DESKTOP_TARGET_ARCH: "universal"
};

function commandForNpm() {
  return process.platform === "win32" ? "npm.cmd" : "npm";
}

async function run(command, args) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: desktopRoot,
      stdio: "inherit",
      env: sharedEnv
    });

    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`${command} exited with code ${code ?? "unknown"}`));
    });
  });
}

const npm = commandForNpm();

await run(npm, ["run", "build:icons"]);
await run(npm, ["run", "prepare:electron"]);
await run(npm, ["run", "build"]);
await run(npm, ["run", "build:sidecar"]);
await run(process.execPath, [
  "scripts/run-builder.mjs",
  "--mac",
  ...builderTargets,
  "--universal",
  "--publish",
  "never"
]);
