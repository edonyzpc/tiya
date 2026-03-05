from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import MutableMapping, Optional

from instance_lock import BotInstanceLock


REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = REPO_ROOT / ".runtime"
PID_FILE = RUNTIME_DIR / "bot.pid"
LOG_FILE = RUNTIME_DIR / "bot.log"
STATE_PATH = RUNTIME_DIR / "bot_state.json"
BOT_ENTRY = REPO_ROOT / "tiya.py"

TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")
USER_IDS_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")

PROXY_PRIORITY = ("TG_PROXY_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
PROXY_NORMALIZED_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


class CliError(RuntimeError):
    pass


def _env_value(environ: MutableMapping[str, str], key: str) -> Optional[str]:
    value = environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _get_env_file() -> Path:
    raw = os.getenv("ENV_FILE")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return REPO_ROOT / ".env"


def _parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
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


def load_dotenv() -> None:
    env_file = _get_env_file()
    if not env_file.is_file():
        print("[info] 未找到 .env，继续使用当前 shell 环境变量")
        return

    print(f"[info] 加载环境文件: {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if not parsed:
            continue
        key, value = parsed
        os.environ[key] = value


def resolve_preferred_proxy(environ: MutableMapping[str, str]) -> Optional[str]:
    for key in PROXY_PRIORITY:
        value = _env_value(environ, key)
        if value:
            return value
    return None


def normalize_proxy_env(environ: MutableMapping[str, str]) -> None:
    preferred = resolve_preferred_proxy(environ)
    if not preferred:
        return

    if _env_value(environ, "TG_PROXY_URL") is not None:
        environ["TG_PROXY_URL"] = preferred

    for key in PROXY_NORMALIZED_KEYS:
        environ[key] = preferred


def _is_runner_available(bin_name: str) -> bool:
    if "/" in bin_name:
        path = Path(bin_name).expanduser()
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(bin_name) is not None


def _validate_runner_bin(provider: str, bin_name: str, required: bool) -> None:
    if _is_runner_available(bin_name):
        return
    if required:
        raise CliError(f"provider={provider} 可执行文件不可用: {bin_name}")
    print(f"[warn] provider={provider} 可执行文件不可用（可继续启动，但切换后会失败）: {bin_name}")


def has_tg_config() -> bool:
    return _env_value(os.environ, "TELEGRAM_BOT_TOKEN") is not None


def validate_tg_config() -> None:
    token = _env_value(os.environ, "TELEGRAM_BOT_TOKEN")
    if token is None:
        return
    if not TOKEN_RE.fullmatch(token):
        raise CliError("TELEGRAM_BOT_TOKEN 格式无效，应类似: 123456789:ABCDEF...")

    user_ids = _env_value(os.environ, "ALLOWED_TELEGRAM_USER_IDS")
    if user_ids and not USER_IDS_RE.fullmatch(user_ids):
        raise CliError("ALLOWED_TELEGRAM_USER_IDS 格式错误，应为数字 ID，多个用逗号分隔")


def validate_shared_config() -> None:
    default_provider = _env_or_default("DEFAULT_PROVIDER", "codex").lower()
    if default_provider not in ("codex", "claude"):
        raise CliError("DEFAULT_PROVIDER 必须是 codex 或 claude")

    codex_bin = _env_or_default("CODEX_BIN", "codex")
    claude_bin = _env_or_default("CLAUDE_BIN", "claude")
    _validate_runner_bin("codex", codex_bin, required=default_provider == "codex")
    _validate_runner_bin("claude", claude_bin, required=default_provider == "claude")


def _read_pid_file() -> Optional[int]:
    if not PID_FILE.is_file():
        return None
    raw = PID_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid_file(pid: int) -> None:
    PID_FILE.write_text(str(pid), encoding="utf-8")


def _remove_pid_file() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_zombie(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return False
    try:
        parts = stat_path.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return False
    return len(parts) > 2 and parts[2] == "Z"


def _read_cmdline(pid: int) -> str:
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.is_file():
        try:
            return proc_cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def _cmdline_matches(cmdline: str) -> bool:
    if not cmdline:
        return False
    if str(BOT_ENTRY) in cmdline:
        return True
    return BOT_ENTRY.name in cmdline


def _is_pid_running(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    if not _pid_exists(pid):
        return False
    if _is_zombie(pid):
        return False
    return _cmdline_matches(_read_cmdline(pid))


def _find_existing_pid() -> Optional[int]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None

    this_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == this_pid:
            continue
        if not _pid_exists(pid):
            continue
        if _is_zombie(pid):
            continue
        if _cmdline_matches(_read_cmdline(pid)):
            return pid
    return None


def tg_is_running() -> tuple[bool, Optional[int]]:
    pid = _read_pid_file()
    if _is_pid_running(pid):
        return True, pid
    _remove_pid_file()

    existing_pid = _find_existing_pid()
    if existing_pid is None:
        return False, None
    _write_pid_file(existing_pid)
    return True, existing_pid


def _resolve_stream_enabled() -> str:
    explicit = _env_value(os.environ, "TG_STREAM_ENABLED")
    if explicit is not None:
        return explicit
    legacy = _env_value(os.environ, "TELEGRAM_ENABLE_DRAFT_STREAM")
    if legacy is not None:
        return legacy
    return "1"


def _build_child_env() -> dict[str, str]:
    default_lock_path = str((RUNTIME_DIR / "bot.lock"))
    child_env = dict(os.environ)
    child_env.update(
        {
            "TELEGRAM_BOT_TOKEN": _env_or_default("TELEGRAM_BOT_TOKEN", ""),
            "ALLOWED_TELEGRAM_USER_IDS": _env_or_default("ALLOWED_TELEGRAM_USER_IDS", ""),
            "DEFAULT_CWD": _env_or_default("DEFAULT_CWD", str(REPO_ROOT)),
            "DEFAULT_PROVIDER": _env_or_default("DEFAULT_PROVIDER", "codex"),
            "CODEX_BIN": _env_or_default("CODEX_BIN", "codex"),
            "CODEX_SESSION_ROOT": _env_or_default("CODEX_SESSION_ROOT", str(Path("~/.codex/sessions").expanduser())),
            "CODEX_SANDBOX_MODE": _env_or_default("CODEX_SANDBOX_MODE", ""),
            "CODEX_APPROVAL_POLICY": _env_or_default("CODEX_APPROVAL_POLICY", ""),
            "CODEX_DANGEROUS_BYPASS": _env_or_default("CODEX_DANGEROUS_BYPASS", "0"),
            "CLAUDE_BIN": _env_or_default("CLAUDE_BIN", "claude"),
            "CLAUDE_SESSION_ROOT": _env_or_default(
                "CLAUDE_SESSION_ROOT", str(Path("~/.claude/projects").expanduser())
            ),
            "CLAUDE_MODEL": _env_or_default("CLAUDE_MODEL", ""),
            "CLAUDE_PERMISSION_MODE": _env_or_default("CLAUDE_PERMISSION_MODE", "default"),
            "STATE_PATH": str(STATE_PATH),
            "TG_STREAM_ENABLED": _resolve_stream_enabled(),
            "TG_STREAM_EDIT_INTERVAL_MS": _env_or_default("TG_STREAM_EDIT_INTERVAL_MS", "700"),
            "TG_STREAM_MIN_DELTA_CHARS": _env_or_default("TG_STREAM_MIN_DELTA_CHARS", "8"),
            "TG_THINKING_STATUS_INTERVAL_MS": _env_or_default("TG_THINKING_STATUS_INTERVAL_MS", "900"),
            "TG_HTTP_MAX_RETRIES": _env_or_default("TG_HTTP_MAX_RETRIES", "2"),
            "TG_HTTP_RETRY_BASE_MS": _env_or_default("TG_HTTP_RETRY_BASE_MS", "300"),
            "TG_HTTP_RETRY_MAX_MS": _env_or_default("TG_HTTP_RETRY_MAX_MS", "3000"),
            "TG_INSTANCE_LOCK_PATH": _env_or_default("TG_INSTANCE_LOCK_PATH", default_lock_path),
            "TG_STREAM_RETRY_COOLDOWN_MS": _env_or_default("TG_STREAM_RETRY_COOLDOWN_MS", "15000"),
            "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS": _env_or_default(
                "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS",
                "2",
            ),
            "TG_STREAM_PREVIEW_FAILFAST": _env_or_default("TG_STREAM_PREVIEW_FAILFAST", "1"),
        }
    )

    legacy_stream = _env_value(os.environ, "TELEGRAM_ENABLE_DRAFT_STREAM")
    if legacy_stream is not None:
        child_env["TELEGRAM_ENABLE_DRAFT_STREAM"] = legacy_stream

    return child_env


def _resolve_instance_lock_path(environ: MutableMapping[str, str]) -> Path:
    raw = _env_value(environ, "TG_INSTANCE_LOCK_PATH")
    if raw:
        return Path(raw).expanduser()
    return RUNTIME_DIR / "bot.lock"


def _probe_instance_lock(environ: MutableMapping[str, str]) -> tuple[bool, str]:
    token = _env_value(environ, "TELEGRAM_BOT_TOKEN")
    if not token:
        return True, ""

    lock = BotInstanceLock(_resolve_instance_lock_path(environ), token)
    acquired, payload = lock.acquire()
    if acquired:
        lock.release()
        return True, ""

    owner_pid = payload.get("pid")
    owner_started = payload.get("started_at")
    owner_cmdline = str(payload.get("cmdline", "") or "")
    msg = (
        f"path={lock.path} owner_pid={owner_pid} owner_started_at={owner_started} "
        f"owner_cmdline={owner_cmdline[:220]!r}"
    )
    return False, msg


def _tail_last_lines(path: Path, lines: int = 10) -> list[str]:
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return content[-lines:]


def _wait_until_stopped(pid: int, timeout_sec: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.2)
    return not _pid_exists(pid)


def _ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def start() -> int:
    _ensure_runtime_dir()

    validate_tg_config()
    validate_shared_config()

    if not has_tg_config():
        print("[error] 未配置 TELEGRAM_BOT_TOKEN")
        print("请配置环境变量：")
        print("  TELEGRAM_BOT_TOKEN=123456:xxxx")
        print("  ALLOWED_TELEGRAM_USER_IDS=123456789  # 可选，推荐")
        return 1

    if not BOT_ENTRY.is_file():
        raise CliError(f"启动入口不存在: {BOT_ENTRY}")

    child_env = _build_child_env()
    lock_ok, lock_msg = _probe_instance_lock(child_env)
    if not lock_ok:
        print(f"[error] 检测到同 token 实例已运行，拒绝启动: {lock_msg}")
        print("[hint] 请先执行 uv run stop，或手动停止占用进程后重试。")
        return 1

    running, pid = tg_is_running()
    if running and pid is not None:
        print(f"[info] Telegram 已运行，PID={pid}")
        return 0

    print("[info] 启动 Telegram 服务...")
    with LOG_FILE.open("ab") as log_fp:
        proc = subprocess.Popen(
            [sys.executable, str(BOT_ENTRY)],
            cwd=str(REPO_ROOT),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    _write_pid_file(proc.pid)
    time.sleep(1)

    if proc.poll() is None:
        print(f"[ok] Telegram 已启动，PID={proc.pid}")
        print(f"[ok] Telegram 日志: {LOG_FILE}")
        return 0

    _remove_pid_file()
    print("[error] Telegram 启动失败，最近日志：")
    for line in _tail_last_lines(LOG_FILE, lines=50):
        print(line)
    return 1


def stop() -> int:
    running, pid = tg_is_running()
    if not running or pid is None:
        print("[info] Telegram 未运行")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise CliError(f"无权限停止进程 PID={pid}: {exc}") from exc

    if not _wait_until_stopped(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise CliError(f"无权限强制停止进程 PID={pid}: {exc}") from exc
        _wait_until_stopped(pid)

    _remove_pid_file()
    print(f"[ok] Telegram 已停止，PID={pid}")
    return 0


def status() -> int:
    running, pid = tg_is_running()
    if running and pid is not None:
        print(f"[ok] Telegram 运行中，PID={pid}")
    else:
        print("[info] Telegram 未运行")
    return 0


def logs() -> int:
    _ensure_runtime_dir()
    LOG_FILE.touch(exist_ok=True)

    for line in _tail_last_lines(LOG_FILE, lines=10):
        print(line)

    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as log_fp:
            log_fp.seek(0, os.SEEK_END)
            while True:
                line = log_fp.readline()
                if line:
                    print(line, end="")
                    continue
                time.sleep(0.2)
    except KeyboardInterrupt:
        return 0


def restart() -> int:
    rc = stop()
    if rc != 0:
        return rc
    return start()


def _bootstrap() -> None:
    load_dotenv()
    normalize_proxy_env(os.environ)


def _run(command) -> int:
    try:
        _bootstrap()
        return int(command())
    except CliError as exc:
        print(f"[error] {exc}")
        return 1


def entry_start() -> int:
    return _run(start)


def entry_stop() -> int:
    return _run(stop)


def entry_restart() -> int:
    return _run(restart)


def entry_status() -> int:
    return _run(status)


def entry_logs() -> int:
    return _run(logs)
