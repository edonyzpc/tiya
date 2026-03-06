from pathlib import Path

from src.runtime_paths import APP_NAME, RuntimePaths, default_runtime_home, resolve_runtime_home


def test_default_runtime_home_uses_macos_convention():
    assert default_runtime_home("Darwin") == Path("~/Library/Application Support").expanduser() / APP_NAME


def test_default_runtime_home_uses_linux_state_dir():
    assert default_runtime_home("Linux") == Path("~/.local/state").expanduser() / APP_NAME


def test_resolve_runtime_home_prefers_xdg_over_platform_default():
    resolved = resolve_runtime_home({"XDG_STATE_HOME": "~/state-home"})

    assert resolved == Path("~/state-home").expanduser() / APP_NAME


def test_resolve_runtime_home_prefers_explicit_tiya_home():
    resolved = resolve_runtime_home({"TIYA_HOME": "~/custom-tiya", "XDG_STATE_HOME": "~/ignored"})

    assert resolved == Path("~/custom-tiya").expanduser()


def test_runtime_paths_include_attachments_dir():
    paths = RuntimePaths.for_instance_name(root=Path("/tmp/tiya"), instance_name="abc123")

    assert paths.attachments_dir == Path("/tmp/tiya/instances/abc123/attachments")
