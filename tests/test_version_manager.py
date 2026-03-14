from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_version_manager():
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "version_manager.py"
    spec = importlib.util.spec_from_file_location("test_version_manager", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "desktop").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "tiya"\nversion = "0.1.2"\n',
        encoding="utf-8",
    )
    (root / "src" / "__init__.py").write_text(
        '__all__ = ["__version__"]\n\n__version__ = "0.1.2"\n',
        encoding="utf-8",
    )
    (root / "desktop" / "package.json").write_text(
        json.dumps({"name": "tiya-desktop", "version": "0.1.2"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "desktop" / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "tiya-desktop",
                "version": "0.1.2",
                "lockfileVersion": 3,
                "packages": {"": {"name": "tiya-desktop", "version": "0.1.2"}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_set_repo_version_updates_python_and_desktop_metadata(tmp_path: Path):
    version_manager = _load_version_manager()
    _write_fixture_repo(tmp_path)

    files = version_manager.VersionFiles(tmp_path)
    version_manager.set_repo_version(files, "0.2.0")

    assert 'version = "0.2.0"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "0.2.0"' in (tmp_path / "src" / "__init__.py").read_text(encoding="utf-8")

    package_json = json.loads((tmp_path / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert package_json["version"] == "0.2.0"

    package_lock = json.loads((tmp_path / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    assert package_lock["version"] == "0.2.0"
    assert package_lock["packages"][""]["version"] == "0.2.0"


def test_set_desktop_version_only_updates_desktop_metadata(tmp_path: Path):
    version_manager = _load_version_manager()
    _write_fixture_repo(tmp_path)

    files = version_manager.VersionFiles(tmp_path)
    version_manager.set_desktop_version(files, "0.2.0-beta.15")

    assert 'version = "0.1.2"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '__version__ = "0.1.2"' in (tmp_path / "src" / "__init__.py").read_text(encoding="utf-8")

    package_json = json.loads((tmp_path / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert package_json["version"] == "0.2.0-beta.15"

    package_lock = json.loads((tmp_path / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    assert package_lock["version"] == "0.2.0-beta.15"
    assert package_lock["packages"][""]["version"] == "0.2.0-beta.15"


def test_build_beta_version_uses_repo_version_and_run_number():
    version_manager = _load_version_manager()

    assert version_manager.build_beta_version("0.1.2", "42") == "0.1.2-beta.42"


def test_verify_tag_matches_repo_version(tmp_path: Path):
    version_manager = _load_version_manager()
    _write_fixture_repo(tmp_path)

    files = version_manager.VersionFiles(tmp_path)

    assert version_manager.verify_tag_matches_repo(files, "v0.1.2") == "0.1.2"
    assert version_manager.verify_tag_matches_repo(files, "refs/tags/v0.1.2") == "0.1.2"


def test_verify_tag_rejects_mismatched_version(tmp_path: Path):
    version_manager = _load_version_manager()
    _write_fixture_repo(tmp_path)

    files = version_manager.VersionFiles(tmp_path)

    with pytest.raises(ValueError, match="does not match repo version"):
        version_manager.verify_tag_matches_repo(files, "v0.1.3")


def test_repo_version_metadata_is_in_sync():
    version_manager = _load_version_manager()
    repo_root = Path(__file__).resolve().parent.parent
    files = version_manager.VersionFiles(repo_root)

    repo_version = version_manager.read_repo_version(files)
    init_content = files.init.read_text(encoding="utf-8")
    package_json = json.loads(files.desktop_package_json.read_text(encoding="utf-8"))
    package_lock = json.loads(files.desktop_package_lock.read_text(encoding="utf-8"))

    assert f'__version__ = "{repo_version}"' in init_content
    assert package_json["version"] == repo_version
    assert package_lock["version"] == repo_version
    assert package_lock["packages"][""]["version"] == repo_version
