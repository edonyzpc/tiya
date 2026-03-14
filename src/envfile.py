from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Sequence


def parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None

    value = value.strip()
    if value and value[0] in ("'", '"') and len(value) >= 2 and value[-1] == value[0]:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_dotenv_line(line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def load_env_file_into_environ(path: Path, environ: Optional[dict[str, str]] = None) -> dict[str, str]:
    target = environ if environ is not None else os.environ
    values = read_env_file(path)
    target.update(values)
    return values


def format_env_value(value: str) -> str:
    if value == "":
        return '""'
    needs_quotes = any(ch.isspace() for ch in value) or "#" in value or '"' in value or "'" in value
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(path: Path, values: Mapping[str, str], *, key_order: Optional[Sequence[str]] = None) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    keys = list(key_order or values.keys())
    extras = [key for key in values.keys() if key not in keys]
    lines: list[str] = []
    for key in [*keys, *extras]:
        raw = values.get(key)
        if raw is None:
            continue
        lines.append(f"{key}={format_env_value(str(raw))}")

    content = "\n".join(lines)
    if content:
        content += "\n"
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)
