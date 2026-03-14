from __future__ import annotations

import importlib.util
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
