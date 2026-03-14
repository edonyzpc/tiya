from __future__ import annotations

import shutil
from pathlib import Path

from PyInstaller.__main__ import run as pyinstaller_run


ROOT = Path(__file__).resolve().parent.parent
DIST_ROOT = ROOT / "dist" / "desktop-sidecars"
BUILD_ROOT = ROOT / "build" / "desktop-sidecars"
ENTRYPOINTS = (
    ("tiya-supervisor", ROOT / "packaging" / "tiya_supervisor_entry.py"),
    ("tiya-worker", ROOT / "packaging" / "tiya_worker_entry.py"),
)
EXTRA_PYINSTALLER_ARGS: dict[str, tuple[tuple[str, str], ...]] = {
    # Claude SDK is imported lazily via importlib in the worker, so PyInstaller
    # needs explicit collection hints for the packaged desktop runtime.
    "tiya-worker": (
        ("--collect-submodules", "claude_agent_sdk"),
        ("--collect-data", "claude_agent_sdk"),
    ),
}


def build_one(name: str, entrypoint: Path) -> None:
    workpath = BUILD_ROOT / name
    specpath = BUILD_ROOT / "spec"
    args = [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(workpath),
        "--specpath",
        str(specpath),
        "--paths",
        str(ROOT),
    ]
    for flag, value in EXTRA_PYINSTALLER_ARGS.get(name, ()):
        args.extend([flag, value])
    args.append(str(entrypoint))
    pyinstaller_run(args)


def main() -> int:
    if DIST_ROOT.exists():
        shutil.rmtree(DIST_ROOT)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    for name, entrypoint in ENTRYPOINTS:
        build_one(name, entrypoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
