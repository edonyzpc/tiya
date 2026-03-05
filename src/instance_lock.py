import errno
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def build_token_lock_path(base_path: Path, token: str) -> Path:
    base = base_path.expanduser()
    suffix = base.suffix
    hashed = token_hash(token)
    if suffix:
        name = f"{base.stem}.{hashed}{suffix}"
    else:
        name = f"{base.name}.{hashed}.lock"
    return base.with_name(name)


def _read_cmdline(pid: int) -> str:
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.is_file():
        return ""
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


class BotInstanceLock:
    def __init__(self, base_path: Path, token: str):
        self.base_path = base_path.expanduser()
        self.token = token
        self.token_digest = token_hash(token)
        self.path = build_token_lock_path(self.base_path, token)
        self._fd: Optional[int] = None

    def acquire(self) -> tuple[bool, dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False, self.read_owner()

        self._fd = fd
        payload = {
            "pid": os.getpid(),
            "cmdline": _read_cmdline(os.getpid()),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lock_path": str(self.path),
            "token_hash": self.token_digest,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, raw)
        try:
            os.fsync(fd)
        except OSError:
            pass
        return True, payload

    def read_owner(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        return {"raw": raw}

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
