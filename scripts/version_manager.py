from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STABLE_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DESKTOP_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
PYPROJECT_VERSION_RE = re.compile(r'(?m)^version = "([^"]+)"$')
INIT_VERSION_RE = re.compile(r'(?m)^__version__ = "([^"]+)"$')


@dataclass(frozen=True)
class VersionFiles:
    root: Path

    @property
    def pyproject(self) -> Path:
        return self.root / "pyproject.toml"

    @property
    def init(self) -> Path:
        return self.root / "src" / "__init__.py"

    @property
    def desktop_package_json(self) -> Path:
        return self.root / "desktop" / "package.json"

    @property
    def desktop_package_lock(self) -> Path:
        return self.root / "desktop" / "package-lock.json"


def _replace_once(pattern: re.Pattern[str], content: str, replacement: str, label: str) -> str:
    updated, count = pattern.subn(replacement, content, count=1)
    if count != 1:
        raise ValueError(f"expected exactly one {label} declaration")
    return updated


def normalize_release_version(raw: str) -> str:
    version = raw.strip()
    if version.startswith("refs/tags/"):
        version = version.removeprefix("refs/tags/")
    if version.startswith("v"):
        version = version[1:]
    if not STABLE_VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid stable version: {raw!r}")
    return version


def normalize_desktop_version(raw: str) -> str:
    version = raw.strip()
    if not DESKTOP_VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid desktop package version: {raw!r}")
    return version


def read_repo_version(files: VersionFiles) -> str:
    content = files.pyproject.read_text(encoding="utf-8")
    match = PYPROJECT_VERSION_RE.search(content)
    if match is None:
        raise ValueError("project version not found in pyproject.toml")
    return normalize_release_version(match.group(1))


def set_repo_version(files: VersionFiles, version: str) -> None:
    stable_version = normalize_release_version(version)

    pyproject = files.pyproject.read_text(encoding="utf-8")
    pyproject = _replace_once(
        PYPROJECT_VERSION_RE,
        pyproject,
        f'version = "{stable_version}"',
        "pyproject version",
    )
    files.pyproject.write_text(pyproject, encoding="utf-8")

    init_py = files.init.read_text(encoding="utf-8")
    init_py = _replace_once(
        INIT_VERSION_RE,
        init_py,
        f'__version__ = "{stable_version}"',
        "__version__",
    )
    files.init.write_text(init_py, encoding="utf-8")

    set_desktop_version(files, stable_version)


def read_desktop_version(files: VersionFiles) -> str:
    package_json = json.loads(files.desktop_package_json.read_text(encoding="utf-8"))
    version = package_json.get("version")
    if not isinstance(version, str):
        raise ValueError("desktop/package.json version is missing")
    return normalize_desktop_version(version)


def set_desktop_version(files: VersionFiles, version: str) -> None:
    desktop_version = normalize_desktop_version(version)

    package_json = json.loads(files.desktop_package_json.read_text(encoding="utf-8"))
    package_json["version"] = desktop_version
    files.desktop_package_json.write_text(json.dumps(package_json, indent=2) + "\n", encoding="utf-8")

    package_lock = json.loads(files.desktop_package_lock.read_text(encoding="utf-8"))
    package_lock["version"] = desktop_version
    package_lock.setdefault("packages", {}).setdefault("", {})["version"] = desktop_version
    files.desktop_package_lock.write_text(json.dumps(package_lock, indent=2) + "\n", encoding="utf-8")


def build_beta_version(base_version: str, run_number: str) -> str:
    stable_version = normalize_release_version(base_version)
    try:
        beta_number = int(run_number)
    except ValueError as exc:
        raise ValueError(f"invalid beta run number: {run_number!r}") from exc
    if beta_number < 1:
        raise ValueError("beta run number must be >= 1")
    return f"{stable_version}-beta.{beta_number}"


def verify_tag_matches_repo(files: VersionFiles, tag: str) -> str:
    tag_version = normalize_release_version(tag)
    repo_version = read_repo_version(files)
    if tag_version != repo_version:
        raise ValueError(f"tag version {tag_version} does not match repo version {repo_version}")
    return tag_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage stable and CI build versions for tiya.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_show = subparsers.add_parser("show", help="Print the stable repo version from pyproject.toml")
    parser_show.set_defaults(func=cmd_show)

    parser_set = subparsers.add_parser("set", help="Write the stable version into Python and desktop metadata")
    parser_set.add_argument("version", help="Stable release version, such as 0.2.0")
    parser_set.set_defaults(func=cmd_set)

    parser_show_desktop = subparsers.add_parser("show-desktop", help="Print the current desktop package version")
    parser_show_desktop.set_defaults(func=cmd_show_desktop)

    parser_set_desktop = subparsers.add_parser("set-desktop", help="Write the desktop package version only")
    parser_set_desktop.add_argument("version", help="Desktop build version, such as 0.2.0-beta.15")
    parser_set_desktop.set_defaults(func=cmd_set_desktop)

    parser_beta = subparsers.add_parser("beta-version", help="Build a CI beta package version from the stable repo version")
    parser_beta.add_argument("--run-number", required=True, help="GitHub Actions run number")
    parser_beta.set_defaults(func=cmd_beta_version)

    parser_verify_tag = subparsers.add_parser("verify-tag", help="Ensure a release tag matches the stable repo version")
    parser_verify_tag.add_argument("tag", help="Git tag, such as v0.2.0")
    parser_verify_tag.set_defaults(func=cmd_verify_tag)

    return parser


def cmd_show(_: argparse.Namespace) -> int:
    print(read_repo_version(VersionFiles(ROOT)))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    set_repo_version(VersionFiles(ROOT), args.version)
    print(normalize_release_version(args.version))
    return 0


def cmd_show_desktop(_: argparse.Namespace) -> int:
    print(read_desktop_version(VersionFiles(ROOT)))
    return 0


def cmd_set_desktop(args: argparse.Namespace) -> int:
    set_desktop_version(VersionFiles(ROOT), args.version)
    print(normalize_desktop_version(args.version))
    return 0


def cmd_beta_version(args: argparse.Namespace) -> int:
    version = build_beta_version(read_repo_version(VersionFiles(ROOT)), args.run_number)
    print(version)
    return 0


def cmd_verify_tag(args: argparse.Namespace) -> int:
    print(verify_tag_matches_repo(VersionFiles(ROOT), args.tag))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
