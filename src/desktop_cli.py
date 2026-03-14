from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_ROOT = REPO_ROOT / "desktop"


class DesktopCliError(RuntimeError):
    pass


def _require_desktop_root() -> Path:
    if not DESKTOP_ROOT.is_dir():
        raise DesktopCliError(f"desktop 工程目录不存在: {DESKTOP_ROOT}")
    package_json = DESKTOP_ROOT / "package.json"
    if not package_json.is_file():
        raise DesktopCliError(f"未找到 desktop/package.json: {package_json}")
    return DESKTOP_ROOT


def _resolve_npm() -> str:
    npm = shutil.which("npm")
    if not npm:
        raise DesktopCliError("未找到 npm，请先安装 Node.js/npm 后再执行 desktop 命令")
    return npm


def _run_npm(npm_args: list[str]) -> int:
    desktop_root = _require_desktop_root()
    npm = _resolve_npm()
    completed = subprocess.run([npm, *npm_args], cwd=str(desktop_root), check=False)
    return int(completed.returncode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desktop", description="Run tiya desktop workflows from the repo root.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_install = subparsers.add_parser("install", help="Install desktop dependencies with npm ci")
    parser_install.set_defaults(npm_args=["ci"])

    parser_dev = subparsers.add_parser("dev", help="Run the desktop shell in development mode")
    parser_dev.set_defaults(npm_args=["run", "dev"])

    parser_start = subparsers.add_parser("start", help="Start the built desktop shell")
    parser_start.set_defaults(npm_args=["run", "start"])

    parser_build = subparsers.add_parser("build", help="Build renderer and Electron bundles")
    parser_build.set_defaults(npm_args=["run", "build"])

    parser_typecheck = subparsers.add_parser("typecheck", help="Typecheck renderer and Electron code")
    parser_typecheck.set_defaults(npm_args=["run", "typecheck"])

    parser_package = subparsers.add_parser("package", help="Build desktop packaging artifacts")
    package_subparsers = parser_package.add_subparsers(dest="package_target", required=True)

    package_commands = {
        "dir": ["run", "package:dir"],
        "deb": ["run", "package:deb"],
        "rpm": ["run", "package:rpm"],
        "linux": ["run", "package:linux"],
        "dmg": ["run", "package:dmg"],
        "mac": ["run", "package:mac"],
    }
    for target, npm_args in package_commands.items():
        target_parser = package_subparsers.add_parser(target, help=f"Run npm {' '.join(npm_args)}")
        target_parser.set_defaults(npm_args=npm_args)

    parser_npm = subparsers.add_parser("npm", help="Pass raw npm arguments through to the desktop workspace")
    parser_npm.add_argument("npm_args", nargs=argparse.REMAINDER)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    npm_args = list(args.npm_args)
    if args.command == "npm" and npm_args and npm_args[0] == "--":
        npm_args = npm_args[1:]
    if args.command == "npm" and not npm_args:
        parser.error("desktop npm 需要附带 npm 参数，例如: uv run desktop npm -- run build:icons")
    try:
        return _run_npm(npm_args)
    except DesktopCliError as exc:
        print(f"[error] {exc}")
        return 1

