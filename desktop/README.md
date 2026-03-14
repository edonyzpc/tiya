# tiya Desktop

## Development

- From the repo root you can drive the desktop workspace with `uv run desktop ...`.
- Install desktop dependencies with `npm ci`.
- Run the desktop shell in development with `npm run dev`.
- Typecheck both renderer and Electron code with `npm run typecheck`.

Examples from the repo root:

- `uv run desktop install`
- `uv run desktop dev`
- `uv run desktop start`
- `uv run desktop typecheck`

## Packaging

- Build a local unpacked smoke artifact with `npm run package:dir`.
- Generate the committed icon set with `npm run build:icons`.
- Build a Linux installable package with `npm run package:deb`.
- Build a Linux RPM package with `npm run package:rpm`.
- Build Linux preview artifacts with `npm run package:linux` to produce `zip + deb + rpm`.
- Build a macOS DMG with `npm run package:dmg`.
- Build macOS preview artifacts with `npm run package:mac` to produce `zip + dmg`.
- Sidecar binaries are produced into `../dist/desktop-sidecars/` before Electron packaging runs.
- The same packaging flows are available from the repo root with `uv run desktop package dir|deb|rpm|linux|dmg|mac`.
- Install the generated Debian package with `sudo apt install ./desktop/release/tiya-0.1.0-linux-<amd64|arm64>.deb`.
- Linux secret storage requires `secret-tool`; on Debian/Ubuntu install it with `sudo apt install libsecret-tools`.
- Linux `zip`/`rpm` artifacts are preview builds and do not guarantee that `secret-tool` is present on the target host.

## Lockfiles

- Commit `desktop/package-lock.json` whenever desktop dependencies change.
- Use `npm ci` in CI and local verification to enforce lockfile fidelity.
- Commit `uv.lock` whenever Python packaging dependencies change, including sidecar build tools such as `PyInstaller`.
- Do not hand-edit either lockfile.

## CI Release Flow

- GitHub Actions workflow: [`../.github/workflows/desktop-package.yml`](../.github/workflows/desktop-package.yml)
- Pull requests and branch pushes run Linux smoke packaging on both `x64` and `arm64`, producing `zip + deb + rpm`.
- `workflow_dispatch` and tags matching `desktop-v*` build preview artifacts for Linux `x64/arm64` and macOS `x64/arm64`, then upload workflow artifacts plus `SHA256SUMS.txt`.
- Automatic GitHub Release publication stays disabled until signing and macOS notarization are implemented.
