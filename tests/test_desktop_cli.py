from __future__ import annotations

from pathlib import Path

from src import desktop_cli


def test_dev_runs_npm_in_desktop_workspace(monkeypatch, tmp_path: Path):
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir(parents=True)
    (desktop_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(desktop_cli, "DESKTOP_ROOT", desktop_root)
    monkeypatch.setattr(desktop_cli.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    called = {}

    def _fake_run(cmd, cwd, check):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["check"] = check
        return type("CompletedProcess", (), {"returncode": 0})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", _fake_run)

    rc = desktop_cli.main(["dev"])

    assert rc == 0
    assert called["cmd"] == ["/usr/bin/npm", "run", "dev"]
    assert called["cwd"] == str(desktop_root)
    assert called["check"] is False


def test_package_deb_maps_to_expected_npm_script(monkeypatch, tmp_path: Path):
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir(parents=True)
    (desktop_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(desktop_cli, "DESKTOP_ROOT", desktop_root)
    monkeypatch.setattr(desktop_cli.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    called = {}

    def _fake_run(cmd, cwd, check):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["check"] = check
        return type("CompletedProcess", (), {"returncode": 0})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", _fake_run)

    rc = desktop_cli.main(["package", "deb"])

    assert rc == 0
    assert called["cmd"] == ["/usr/bin/npm", "run", "package:deb"]
    assert called["cwd"] == str(desktop_root)
    assert called["check"] is False


def test_install_uses_npm_ci(monkeypatch, tmp_path: Path):
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir(parents=True)
    (desktop_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(desktop_cli, "DESKTOP_ROOT", desktop_root)
    monkeypatch.setattr(desktop_cli.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    called = {}

    def _fake_run(cmd, cwd, check):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["check"] = check
        return type("CompletedProcess", (), {"returncode": 0})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", _fake_run)

    rc = desktop_cli.main(["install"])

    assert rc == 0
    assert called["cmd"] == ["/usr/bin/npm", "ci"]
    assert called["cwd"] == str(desktop_root)
    assert called["check"] is False


def test_npm_passthrough_strips_separator(monkeypatch, tmp_path: Path):
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir(parents=True)
    (desktop_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(desktop_cli, "DESKTOP_ROOT", desktop_root)
    monkeypatch.setattr(desktop_cli.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    called = {}

    def _fake_run(cmd, cwd, check):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["check"] = check
        return type("CompletedProcess", (), {"returncode": 0})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", _fake_run)

    rc = desktop_cli.main(["npm", "--", "run", "build:icons"])

    assert rc == 0
    assert called["cmd"] == ["/usr/bin/npm", "run", "build:icons"]
    assert called["cwd"] == str(desktop_root)
    assert called["check"] is False


def test_returns_error_when_npm_is_missing(monkeypatch, tmp_path: Path, capsys):
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir(parents=True)
    (desktop_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(desktop_cli, "DESKTOP_ROOT", desktop_root)
    monkeypatch.setattr(desktop_cli.shutil, "which", lambda name: None)

    rc = desktop_cli.main(["dev"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "未找到 npm" in out

