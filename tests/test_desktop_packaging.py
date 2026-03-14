from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def test_manual_deb_package_declares_libsecret_tools_dependency():
    script_path = Path(__file__).resolve().parent.parent / "desktop" / "scripts" / "build-deb-manual.mjs"
    content = script_path.read_text(encoding="utf-8")

    assert "libsecret-tools" in content


def test_manual_deb_package_maps_supported_architectures():
    script_path = Path(__file__).resolve().parent.parent / "desktop" / "scripts" / "build-deb-manual.mjs"
    content = script_path.read_text(encoding="utf-8")

    assert "TIYA_DESKTOP_TARGET_ARCH" in content
    assert 'x64: {' in content
    assert 'debArch: "amd64"' in content
    assert 'arm64: {' in content
    assert 'debArch: "arm64"' in content
    assert "linux-${builderArch}-unpacked" in content


def test_desktop_lockfile_includes_supported_native_binaries():
    repo_root = Path(__file__).resolve().parent.parent
    package_json = json.loads((repo_root / "desktop" / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((repo_root / "desktop" / "package-lock.json").read_text(encoding="utf-8"))

    expected_optional_dependencies = {
        "@esbuild/darwin-arm64": "0.25.12",
        "@esbuild/darwin-x64": "0.25.12",
        "@esbuild/linux-arm64": "0.25.12",
        "@esbuild/linux-x64": "0.25.12",
        "@rollup/rollup-darwin-arm64": "4.59.0",
        "@rollup/rollup-darwin-x64": "4.59.0",
        "@rollup/rollup-linux-arm64-gnu": "4.59.0",
        "@rollup/rollup-linux-x64-gnu": "4.59.0",
    }

    assert package_json["optionalDependencies"] == expected_optional_dependencies

    root_package = package_lock["packages"][""]
    assert root_package["optionalDependencies"] == expected_optional_dependencies

    expected_lock_entries = [
        "node_modules/@esbuild/darwin-arm64",
        "node_modules/@esbuild/darwin-x64",
        "node_modules/@esbuild/linux-arm64",
        "node_modules/@esbuild/linux-x64",
        "node_modules/@rollup/rollup-darwin-arm64",
        "node_modules/@rollup/rollup-darwin-x64",
        "node_modules/@rollup/rollup-linux-arm64-gnu",
        "node_modules/@rollup/rollup-linux-x64-gnu",
    ]
    for entry in expected_lock_entries:
        assert entry in package_lock["packages"]


def test_macos_packaging_uses_universal_targets():
    repo_root = Path(__file__).resolve().parent.parent
    package_json = json.loads((repo_root / "desktop" / "package.json").read_text(encoding="utf-8"))
    builder_config = (repo_root / "desktop" / "electron-builder.yml").read_text(encoding="utf-8")

    assert package_json["scripts"]["package:dmg"] == "node scripts/package-mac.mjs dmg"
    assert package_json["scripts"]["package:mac"] == "node scripts/package-mac.mjs zip dmg"
    assert "target: zip\n      arch: universal" in builder_config
    assert "target: dmg\n      arch: universal" in builder_config


def _load_sidecar_builder():
    script_path = Path(__file__).resolve().parent.parent / "packaging" / "build_desktop_sidecars.py"
    spec = importlib.util.spec_from_file_location("test_build_desktop_sidecars", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_sidecar_collects_claude_sdk(monkeypatch):
    builder = _load_sidecar_builder()
    captured: list[list[str]] = []
    monkeypatch.setattr(builder, "pyinstaller_run", lambda args: captured.append(list(args)))

    builder.build_one("tiya-worker", builder.ROOT / "packaging" / "tiya_worker_entry.py")

    assert captured
    args = captured[0]
    assert "--collect-submodules" in args
    assert "claude_agent_sdk" in args
    assert "--collect-data" in args


def test_macos_sidecar_builder_uses_universal2(monkeypatch):
    builder = _load_sidecar_builder()
    captured: list[list[str]] = []
    monkeypatch.setattr(builder, "pyinstaller_run", lambda args: captured.append(list(args)))
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("TIYA_DESKTOP_TARGET_ARCH", "universal")

    builder.build_one("tiya-supervisor", builder.ROOT / "packaging" / "tiya_supervisor_entry.py")

    assert captured
    args = captured[0]
    assert "--target-arch" in args
    assert "universal2" in args
