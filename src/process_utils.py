from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Optional


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    stat: str
    cmdline: str

    @property
    def is_zombie(self) -> bool:
        stat = (self.stat or "").upper()
        if "Z" in stat:
            return True
        return "<defunct>" in (self.cmdline or "").lower()


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def parse_ps_output(pid: int, stdout: str) -> Optional[ProcessSnapshot]:
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        stat = parts[0]
        cmdline = parts[1].strip() if len(parts) > 1 else ""
        return ProcessSnapshot(pid=pid, stat=stat, cmdline=cmdline)
    return None


def _read_proc_stat(pid: int) -> str:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return ""
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not raw:
        return ""
    closing = raw.rfind(")")
    if closing < 0:
        parts = raw.split()
        return parts[2] if len(parts) > 2 else ""
    remainder = raw[closing + 1 :].strip().split()
    return remainder[0] if remainder else ""


def _read_proc_cmdline(pid: int) -> str:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.is_file():
        return ""
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _read_proc_snapshot(pid: int) -> Optional[ProcessSnapshot]:
    stat = _read_proc_stat(pid)
    cmdline = _read_proc_cmdline(pid)
    if not stat and not cmdline:
        return None
    return ProcessSnapshot(pid=pid, stat=stat, cmdline=cmdline)


def _read_ps_snapshot(pid: int) -> Optional[ProcessSnapshot]:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return parse_ps_output(pid, result.stdout)


def read_process_snapshot(pid: int) -> Optional[ProcessSnapshot]:
    if pid <= 0 or not pid_exists(pid):
        return None
    proc_snapshot = _read_proc_snapshot(pid)
    if proc_snapshot is not None:
        return proc_snapshot
    return _read_ps_snapshot(pid)


def read_process_cmdline(pid: int) -> str:
    snapshot = read_process_snapshot(pid)
    if snapshot is None:
        return ""
    return snapshot.cmdline
