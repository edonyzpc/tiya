import { access } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

if (process.platform !== "linux") {
  console.error("Prepackaged RPM builds are only supported on Linux hosts.");
  process.exit(1);
}

const desktopRoot = path.resolve(process.cwd());
const releaseRoot = path.join(desktopRoot, "release");
const ARCHITECTURE_MAP = {
  x64: {
    builderArch: "x64",
    cliFlag: "--x64"
  },
  amd64: {
    builderArch: "x64",
    cliFlag: "--x64"
  },
  arm64: {
    builderArch: "arm64",
    cliFlag: "--arm64"
  },
  aarch64: {
    builderArch: "arm64",
    cliFlag: "--arm64"
  }
};

function resolveTargetArchitecture() {
  const requestedArch = (process.env.TIYA_DESKTOP_TARGET_ARCH ?? process.arch).trim();
  const mapping = ARCHITECTURE_MAP[requestedArch];
  if (mapping) {
    return mapping;
  }

  console.error(
    `Unsupported RPM packaging architecture: ${requestedArch}. ` +
      "Set TIYA_DESKTOP_TARGET_ARCH to one of x64, amd64, arm64, or aarch64."
  );
  process.exit(1);
}

async function run(command, args, options = {}) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: "inherit",
      ...options
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

async function resolvePrepackagedDir(builderArch) {
  const configuredDir = process.env.TIYA_DESKTOP_PREPACKAGED_DIR;
  const candidates = [
    configuredDir ? path.resolve(desktopRoot, configuredDir) : null,
    path.join(releaseRoot, `linux-${builderArch}-unpacked`),
    path.join(releaseRoot, "linux-unpacked")
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      await access(path.join(candidate, "resources.pak"));
      return candidate;
    } catch {
      // Try the next possible output directory.
    }
  }

  console.error(
    `Expected Electron bundle at ${candidates.join(" or ")}. ` +
      "Extract or build the unpacked Linux app first."
  );
  process.exit(1);
}

const { builderArch, cliFlag } = resolveTargetArchitecture();
const prepackagedDir = await resolvePrepackagedDir(builderArch);

await run(process.execPath, [
  "scripts/run-builder.mjs",
  "--linux",
  "rpm",
  cliFlag,
  "--prepackaged",
  prepackagedDir,
  "--publish",
  "never"
], { cwd: desktopRoot });
