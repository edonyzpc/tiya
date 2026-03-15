from pathlib import Path

from src.runtime_paths import APP_NAME, RuntimePaths, default_runtime_home, default_working_dir, resolve_runtime_home


def test_default_runtime_home_uses_home_hidden_dir_on_macos():
    assert default_runtime_home("Darwin") == Path(f"~/.{APP_NAME}").expanduser()


def test_default_runtime_home_uses_home_hidden_dir_on_linux():
    assert default_runtime_home("Linux") == Path(f"~/.{APP_NAME}").expanduser()


def test_default_working_dir_uses_home_hidden_dir():
    assert default_working_dir() == Path(f"~/.{APP_NAME}").expanduser()


def test_resolve_runtime_home_prefers_xdg_over_platform_default():
    resolved = resolve_runtime_home({"XDG_STATE_HOME": "~/state-home"})

    assert resolved == Path("~/state-home").expanduser() / APP_NAME


def test_resolve_runtime_home_prefers_explicit_tiya_home():
    resolved = resolve_runtime_home({"TIYA_HOME": "~/custom-tiya", "XDG_STATE_HOME": "~/ignored"})

    assert resolved == Path("~/custom-tiya").expanduser()


def test_runtime_paths_include_storage_and_attachments_dirs():
    paths = RuntimePaths.for_instance_name(root=Path("/tmp/tiya"), instance_name="abc123")

    assert paths.storage_dir == Path("/tmp/tiya/storage")
    assert paths.db_file == Path("/tmp/tiya/storage/tiya.db")
    assert paths.attachments_dir == Path("/tmp/tiya/instances/abc123/attachments")
