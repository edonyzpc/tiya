import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const desktopRoot = path.resolve(process.cwd());
const repoRoot = path.resolve(desktopRoot, "..");

async function exists(targetPath) {
  try {
    await access(targetPath);
    return true;
  } catch {
    return false;
  }
}

async function resolvePython() {
  const configured = process.env.TIYA_DESKTOP_PYTHON;
  if (configured) {
    return configured;
  }
  const candidates = [
    path.join(repoRoot, ".venv", "bin", "python"),
    "/usr/bin/python3",
    "/usr/local/bin/python3"
  ];
  for (const candidate of candidates) {
    if (await exists(candidate)) {
      return candidate;
    }
  }
  return "python3";
}

const python = await resolvePython();

await new Promise((resolve, reject) => {
  const child = spawn(python, [path.join(repoRoot, "packaging", "build_desktop_sidecars.py")], {
    cwd: repoRoot,
    stdio: "inherit",
    env: process.env
  });

  child.on("error", reject);
  child.on("exit", (code) => {
    if (code === 0) {
      resolve();
      return;
    }
    reject(new Error(`Sidecar build failed with exit code ${code ?? "unknown"}`));
  });
});
