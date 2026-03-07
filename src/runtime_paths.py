import os
from dataclasses import dataclass
from pathlib import Path
import platform
from typing import Mapping, Optional

from .instance_lock import token_hash


APP_NAME = "tiya"
INSTANCE_DIR_NAME = "instances"


def _env_value(environ: Mapping[str, str], key: str) -> Optional[str]:
    value = environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def default_runtime_home(system_name: Optional[str] = None) -> Path:
    current_system = system_name or platform.system()
    if current_system == "Darwin":
        return Path("~/Library/Application Support").expanduser() / APP_NAME
    return Path("~/.local/state").expanduser() / APP_NAME


def resolve_runtime_home(environ: Mapping[str, str]) -> Path:
    explicit = _env_value(environ, "TIYA_HOME")
    if explicit:
        return Path(explicit).expanduser()

    xdg_state_home = _env_value(environ, "XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home).expanduser() / APP_NAME

    return default_runtime_home()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    instance_name: str
    instance_dir: Path
    storage_dir: Path
    db_file: Path
    pid_file: Path
    log_file: Path
    state_file: Path
    lock_base: Path
    attachments_dir: Path

    @classmethod
    def for_token(cls, token: str, environ: Optional[Mapping[str, str]] = None) -> "RuntimePaths":
        env = environ or os.environ
        root = resolve_runtime_home(env)
        instance_name = token_hash(token)
        return cls.for_instance_name(root=root, instance_name=instance_name)

    @classmethod
    def for_instance_name(cls, root: Path, instance_name: str) -> "RuntimePaths":
        resolved_root = root.expanduser()
        instance_dir = resolved_root / INSTANCE_DIR_NAME / instance_name
        storage_dir = resolved_root / "storage"
        return cls(
            root=resolved_root,
            instance_name=instance_name,
            instance_dir=instance_dir,
            storage_dir=storage_dir,
            db_file=storage_dir / "tiya.db",
            pid_file=instance_dir / "bot.pid",
            log_file=instance_dir / "bot.log",
            state_file=instance_dir / "bot_state.json",
            lock_base=instance_dir / "bot.lock",
            attachments_dir=instance_dir / "attachments",
        )


def list_runtime_instances(environ: Optional[Mapping[str, str]] = None) -> list[RuntimePaths]:
    env = environ or os.environ
    root = resolve_runtime_home(env)
    instances_dir = root / INSTANCE_DIR_NAME
    if not instances_dir.is_dir():
        return []

    items: list[RuntimePaths] = []
    for entry in sorted(instances_dir.iterdir()):
        if not entry.is_dir():
            continue
        items.append(RuntimePaths.for_instance_name(root=root, instance_name=entry.name))
    return items
