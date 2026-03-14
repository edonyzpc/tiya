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


def test_linux_packaging_supports_arm64_repack_flow():
    repo_root = Path(__file__).resolve().parent.parent
    package_json = json.loads((repo_root / "desktop" / "package.json").read_text(encoding="utf-8"))
    workflow = (repo_root / ".github" / "workflows" / "desktop-package.yml").read_text(encoding="utf-8")
    repack_script = (repo_root / "desktop" / "scripts" / "build-rpm-prepackaged.mjs").read_text(encoding="utf-8")

    assert package_json["scripts"]["package:linux-no-rpm"].endswith(
        'node scripts/run-builder.mjs --linux zip --publish never && node scripts/build-deb-manual.mjs'
    )
    assert package_json["scripts"]["package:rpm:prepackaged"] == "node scripts/build-rpm-prepackaged.mjs"
    assert "--prepackaged" in repack_script
    assert "linux-arm64-unpacked.tar.gz" in workflow
    assert "package:rpm:prepackaged" in workflow


def test_macos_packaging_uses_universal_targets():
    repo_root = Path(__file__).resolve().parent.parent
    package_json = json.loads((repo_root / "desktop" / "package.json").read_text(encoding="utf-8"))
    builder_config = (repo_root / "desktop" / "electron-builder.yml").read_text(encoding="utf-8")
    packaging_script = (repo_root / "desktop" / "scripts" / "package-mac.mjs").read_text(encoding="utf-8")
    sidecar_bundle_script = (
        repo_root / "desktop" / "scripts" / "prepare-mac-universal-sidecars.mjs"
    ).read_text(encoding="utf-8")
    workflow = (repo_root / ".github" / "workflows" / "desktop-package.yml").read_text(encoding="utf-8")

    assert package_json["scripts"]["package:dmg"] == "node scripts/package-mac.mjs dmg"
    assert package_json["scripts"]["package:mac"] == "node scripts/package-mac.mjs zip dmg"
    assert "target: zip\n      arch: universal" in builder_config
    assert "target: dmg\n      arch: universal" in builder_config
    assert "Missing prepared macOS universal sidecar asset" in packaging_script
    assert "macos-x64" in sidecar_bundle_script
    assert "macos-arm64" in sidecar_bundle_script
    assert 'case "$(uname -m)"' in sidecar_bundle_script
    beta_universal = workflow.split("  beta-macos-universal:\n", maxsplit=1)[1].split(
        "  release-metadata:\n", maxsplit=1
    )[0]
    release_universal = workflow.split("  release-macos-universal:\n", maxsplit=1)[1].split(
        "  publish-release:\n", maxsplit=1
    )[0]
    assert "uses: astral-sh/setup-uv@v5" in beta_universal
    assert "name: Sync Python dependencies" in beta_universal
    assert "run: uv sync --group dev" in beta_universal
    assert "uses: actions/setup-python@v5" in release_universal
    assert "uses: astral-sh/setup-uv@v5" in release_universal
    assert "name: Sync Python dependencies" in release_universal
    assert "run: uv sync --group dev" in release_universal


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


def test_macos_sidecar_builder_defaults_to_host_arch(monkeypatch):
    builder = _load_sidecar_builder()
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "arm64")
    monkeypatch.delenv("TIYA_DESKTOP_TARGET_ARCH", raising=False)

    assert builder.resolve_pyinstaller_target_arch() == "arm64"


def test_macos_sidecar_builder_uses_requested_arch(monkeypatch):
    builder = _load_sidecar_builder()
    captured: list[list[str]] = []
    monkeypatch.setattr(builder, "pyinstaller_run", lambda args: captured.append(list(args)))
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("TIYA_DESKTOP_TARGET_ARCH", "x64")

    builder.build_one("tiya-supervisor", builder.ROOT / "packaging" / "tiya_supervisor_entry.py")

    assert captured
    args = captured[0]
    assert "--target-arch" in args
    assert "x86_64" in args
