import { chmod, cp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

if (process.platform !== "linux") {
  console.error("Manual Debian packaging is only supported on Linux hosts.");
  process.exit(1);
}

const desktopRoot = path.resolve(process.cwd());
const releaseRoot = path.join(desktopRoot, "release");
const appRoot = path.join(releaseRoot, "linux-unpacked");
const packageJsonPath = path.join(desktopRoot, "package.json");
const packageJson = JSON.parse(await readFile(packageJsonPath, "utf8"));
const version = packageJson.version;
const outputPath = path.join(releaseRoot, `tiya-${version}-linux-amd64.deb`);
const stagingRoot = path.join(os.tmpdir(), `tiya-deb-${process.pid}`);

async function run(command, args, options = {}) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: "inherit",
      ...options
    });

    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(new Error(`${command} exited with code ${code ?? "unknown"}`));
    });
  });
}

async function ensureAppBundle() {
  try {
    await readFile(path.join(appRoot, "resources.pak"));
  } catch {
    console.error("Expected Electron bundle at release/linux-unpacked. Run the build/package steps first.");
    process.exit(1);
  }
}

function controlFile() {
  return [
    "Package: tiya-desktop",
    `Version: ${version}`,
    "Section: utility",
    "Priority: optional",
    "Architecture: amd64",
    "Maintainer: tiya contributors",
    "Depends: libgtk-3-0, libnotify4, libnss3, libxss1, libxtst6, xdg-utils, libatspi2.0-0, libuuid1, libsecret-1-0, libsecret-tools",
    "Recommends: libappindicator3-1",
    "Homepage: https://github.com/edonyzpc/tiya",
    "Description: tiya desktop control console",
    " Desktop GUI for configuring and managing the tiya runtime.",
    ""
  ].join("\n");
}

function maintainerScript() {
  return `#!/bin/sh
set -e

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
`;
}

function desktopEntry() {
  return `[Desktop Entry]
Version=1.0
Type=Application
Name=tiya
Comment=Desktop GUI for configuring and managing the tiya runtime.
Exec=/opt/tiya/tiya-desktop %U
Icon=tiya-desktop
Terminal=false
Categories=Utility;Development;
StartupWMClass=tiya
`;
}

async function stagePackage() {
  const pkgRoot = path.join(stagingRoot, "pkg");
  const debianDir = path.join(pkgRoot, "DEBIAN");
  const optDir = path.join(pkgRoot, "opt", "tiya");
  const binDir = path.join(pkgRoot, "usr", "bin");
  const applicationsDir = path.join(pkgRoot, "usr", "share", "applications");
  const iconsRoot = path.join(pkgRoot, "usr", "share", "icons", "hicolor");
  const iconSizes = ["16", "32", "48", "64", "128", "256", "512", "1024"];

  await rm(stagingRoot, { recursive: true, force: true });
  await mkdir(debianDir, { recursive: true });
  await mkdir(optDir, { recursive: true });
  await mkdir(binDir, { recursive: true });
  await mkdir(applicationsDir, { recursive: true });

  for (const size of iconSizes) {
    await mkdir(path.join(iconsRoot, `${size}x${size}`, "apps"), { recursive: true });
  }

  await cp(appRoot, optDir, { recursive: true });
  await symlink("/opt/tiya/tiya-desktop", path.join(binDir, "tiya-desktop"));

  await writeFile(path.join(debianDir, "control"), controlFile(), "utf8");
  await writeFile(path.join(debianDir, "postinst"), maintainerScript(), "utf8");
  await writeFile(path.join(debianDir, "postrm"), maintainerScript(), "utf8");
  await chmod(path.join(debianDir, "postinst"), 0o755);
  await chmod(path.join(debianDir, "postrm"), 0o755);

  await writeFile(path.join(applicationsDir, "tiya-desktop.desktop"), desktopEntry(), "utf8");

  for (const size of iconSizes) {
    const source = path.join(desktopRoot, "assets", "icons", `${size}x${size}.png`);
    const target = path.join(iconsRoot, `${size}x${size}`, "apps", "tiya-desktop.png");
    await cp(source, target);
  }

  return pkgRoot;
}

await ensureAppBundle();
const pkgRoot = await stagePackage();
await rm(outputPath, { force: true });
await run("dpkg-deb", ["--build", "-Zgzip", "-z1", "--root-owner-group", pkgRoot, outputPath], {
  cwd: desktopRoot
});
