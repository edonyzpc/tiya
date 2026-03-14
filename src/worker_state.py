from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


def read_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def update_worker_state(
    path: Path,
    *,
    phase: str,
    pid: int,
    ready_at: Optional[int] = None,
    error: Optional[str] = None,
) -> dict[str, object]:
    current = read_state(path)
    payload: dict[str, object] = {
        "phase": phase,
        "pid": pid,
        "updatedAt": int(time.time()),
        "readyAt": ready_at if ready_at is not None else current.get("readyAt"),
        "error": error or None,
    }
    write_state(path, payload)
    return payload
