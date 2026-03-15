import { access, chmod, cp, mkdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const SIDECAR_NAMES = ["tiya-supervisor", "tiya-worker"];
const ARCHITECTURES = {
  x64: "macos-x64",
  arm64: "macos-arm64"
};
const desktopRoot = path.resolve(process.cwd());
const defaultOutputRoot = path.resolve(desktopRoot, "..", "dist", "desktop-sidecars");

function usage() {
  console.error(
    "Usage: node scripts/prepare-mac-universal-sidecars.mjs --x64 <dir> --arm64 <dir> [--output <dir>]"
  );
}

function parseArgs(argv) {
  const options = {
    output: defaultOutputRoot
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--x64" || arg === "--arm64" || arg === "--output") {
      const value = argv[index + 1];
      if (!value) {
        usage();
        process.exit(1);
      }
      options[arg.slice(2)] = path.resolve(desktopRoot, value);
      index += 1;
      continue;
    }
    usage();
    process.exit(1);
  }

  if (!options.x64 || !options.arm64) {
    usage();
    process.exit(1);
  }
  return options;
}

function wrapperScript(binaryName) {
  return `#!/bin/sh
set -eu

SELF_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
case "$(uname -m)" in
  arm64|aarch64)
    TARGET_DIR="macos-arm64"
    ;;
  x86_64|amd64)
    TARGET_DIR="macos-x64"
    ;;
  *)
    echo "Unsupported macOS architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

exec "$SELF_DIR/$TARGET_DIR/${binaryName}" "$@"
`;
}

async function ensureSidecarTree(rootDir, binaryName) {
  const binaryPath = path.join(rootDir, binaryName, binaryName);
  try {
    await access(binaryPath);
  } catch {
    console.error(`Expected sidecar binary at ${binaryPath}`);
    process.exit(1);
  }
}

async function copySidecarVariant(outputRoot, archKey, sourceRoot, binaryName) {
  const variantDir = path.join(outputRoot, binaryName, ARCHITECTURES[archKey]);
  await cp(path.join(sourceRoot, binaryName), variantDir, {
    recursive: true,
    // PyInstaller bundles Python.framework as a symlink tree. Preserve the
    // original links verbatim so the copied framework still has a valid macOS
    // bundle layout for codesign inside the final app.
    verbatimSymlinks: true
  });
}

const options = parseArgs(process.argv.slice(2));

for (const binaryName of SIDECAR_NAMES) {
  await ensureSidecarTree(options.x64, binaryName);
  await ensureSidecarTree(options.arm64, binaryName);
}

await rm(options.output, { recursive: true, force: true });
await mkdir(options.output, { recursive: true });

for (const binaryName of SIDECAR_NAMES) {
  const targetRoot = path.join(options.output, binaryName);
  await mkdir(targetRoot, { recursive: true });
  await copySidecarVariant(options.output, "x64", options.x64, binaryName);
  await copySidecarVariant(options.output, "arm64", options.arm64, binaryName);
  const wrapperPath = path.join(targetRoot, binaryName);
  await writeFile(wrapperPath, wrapperScript(binaryName), "utf8");
  await chmod(wrapperPath, 0o755);
}
