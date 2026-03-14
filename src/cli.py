from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import asyncio
import json
from pathlib import Path
from typing import Callable, MutableMapping, Optional

from .config import load_config
from .supervisor_client import (
    RpcResponseError,
    SupervisorClientError,
    SupervisorSubscription,
    SupervisorUnavailableError,
    call_rpc,
    shutdown_supervisor,
    supervisor_pid,
)
from .instance_lock import BotInstanceLock
from .process_utils import pid_exists, read_process_snapshot
from .provider_defaults import (
    default_claude_session_root,
    default_codex_session_root,
    resolve_claude_bin,
    resolve_codex_bin,
)
from .runtime_paths import RuntimePaths, list_runtime_instances, resolve_runtime_home
from .services.storage import StorageConfig, StorageManager
from .services.storage.schema import SCHEMA_VERSION
from .worker_state import read_state


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_MODULE = "src"
READY_MARKER = "tiya service ready"

TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")
USER_IDS_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")
MODULE_CMD_RE = re.compile(r"(^|\s)-m\s+src(\s|$)")
PACKAGED_WORKER_CMD_RE = re.compile(r"(^|[\s/\\\\])tiya-worker(?:\.exe)?(\s|$)")

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


def load_dotenv(*, verbose: bool = True) -> None:
    env_file = _get_env_file()
    if not env_file.is_file():
        if verbose:
            print("[info] 未找到 .env，继续使用当前 shell 环境变量")
        return

    if verbose:
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

    codex_bin = resolve_codex_bin(_env_value(os.environ, "CODEX_BIN"))
    claude_bin = resolve_claude_bin(_env_value(os.environ, "CLAUDE_BIN"))
    _validate_runner_bin("codex", codex_bin, required=default_provider == "codex")
    _validate_runner_bin("claude", claude_bin, required=default_provider == "claude")


def _require_runtime_paths() -> RuntimePaths:
    token = _env_value(os.environ, "TELEGRAM_BOT_TOKEN")
    if not token:
        raise CliError("未配置 TELEGRAM_BOT_TOKEN，无法定位实例目录")
    return RuntimePaths.for_token(token, os.environ)


def _resolve_existing_runtime_paths() -> Optional[RuntimePaths]:
    token = _env_value(os.environ, "TELEGRAM_BOT_TOKEN")
    if token:
        return RuntimePaths.for_token(token, os.environ)

    instances = [paths for paths in list_runtime_instances(os.environ) if paths.instance_dir.exists()]
    if not instances:
        return None
    if len(instances) > 1:
        raise CliError("检测到多个实例目录，请设置 TELEGRAM_BOT_TOKEN 或 TIYA_HOME 后再执行")
    return instances[0]


def _read_pid_file(paths: RuntimePaths) -> Optional[int]:
    if not paths.pid_file.is_file():
        return None
    raw = paths.pid_file.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid_file(paths: RuntimePaths, pid: int) -> None:
    paths.instance_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text(str(pid), encoding="utf-8")


def _remove_pid_file(paths: RuntimePaths) -> None:
    if paths.pid_file.exists():
        paths.pid_file.unlink()


def _pid_exists(pid: int) -> bool:
    return pid_exists(pid)


def _is_zombie(pid: int) -> bool:
    snapshot = read_process_snapshot(pid)
    if snapshot is None:
        return False
    return snapshot.is_zombie


def _read_cmdline(pid: int) -> str:
    snapshot = read_process_snapshot(pid)
    if snapshot is None:
        return ""
    return snapshot.cmdline


def _cmdline_matches(cmdline: str) -> bool:
    if not cmdline:
        return False
    if MODULE_CMD_RE.search(cmdline):
        return True
    if PACKAGED_WORKER_CMD_RE.search(cmdline):
        return True
    return "tiya.py" in cmdline


def _is_pid_running(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    snapshot = read_process_snapshot(pid)
    if snapshot is None:
        return False
    if snapshot.is_zombie:
        return False
    return _cmdline_matches(snapshot.cmdline)


def _read_lock_owner_pid(paths: RuntimePaths) -> Optional[int]:
    token = _env_value(os.environ, "TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    lock = BotInstanceLock(paths.lock_base, token)
    owner = lock.read_owner()
    pid = owner.get("pid")
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.isdigit():
        return int(pid)
    return None


def tg_is_running(paths: RuntimePaths) -> tuple[bool, Optional[int]]:
    pid = _read_pid_file(paths)
    if _is_pid_running(pid):
        return True, pid
    _remove_pid_file(paths)

    owner_pid = _read_lock_owner_pid(paths)
    if _is_pid_running(owner_pid):
        assert owner_pid is not None
        _write_pid_file(paths, owner_pid)
        return True, owner_pid
    return False, None


def _resolve_stream_enabled() -> str:
    explicit = _env_value(os.environ, "TG_STREAM_ENABLED")
    if explicit is not None:
        return explicit
    legacy = _env_value(os.environ, "TELEGRAM_ENABLE_DRAFT_STREAM")
    if legacy is not None:
        return legacy
    return "1"


def _build_child_env(paths: RuntimePaths) -> dict[str, str]:
    resolved_codex_bin = resolve_codex_bin(_env_value(os.environ, "CODEX_BIN"))
    resolved_claude_bin = resolve_claude_bin(_env_value(os.environ, "CLAUDE_BIN"))
    child_env = dict(os.environ)
    child_env.update(
        {
            "TELEGRAM_BOT_TOKEN": _env_or_default("TELEGRAM_BOT_TOKEN", ""),
            "ALLOWED_TELEGRAM_USER_IDS": _env_or_default("ALLOWED_TELEGRAM_USER_IDS", ""),
            "ALLOWED_CWD_ROOTS": _env_or_default("ALLOWED_CWD_ROOTS", ""),
            "DEFAULT_CWD": _env_or_default("DEFAULT_CWD", str(REPO_ROOT)),
            "DEFAULT_PROVIDER": _env_or_default("DEFAULT_PROVIDER", "codex"),
            "CODEX_BIN": resolved_codex_bin,
            "CODEX_SESSION_ROOT": _env_or_default("CODEX_SESSION_ROOT", str(default_codex_session_root())),
            "CODEX_SANDBOX_MODE": _env_or_default("CODEX_SANDBOX_MODE", ""),
            "CODEX_APPROVAL_POLICY": _env_or_default("CODEX_APPROVAL_POLICY", ""),
            "CODEX_DANGEROUS_BYPASS": _env_or_default("CODEX_DANGEROUS_BYPASS", "0"),
            "CLAUDE_BIN": resolved_claude_bin,
            "CLAUDE_SESSION_ROOT": _env_or_default("CLAUDE_SESSION_ROOT", str(default_claude_session_root())),
            "CLAUDE_MODEL": _env_or_default("CLAUDE_MODEL", ""),
            "CLAUDE_PERMISSION_MODE": _env_or_default("CLAUDE_PERMISSION_MODE", "default"),
            "STORAGE_PATH": str(paths.db_file),
            "STATE_PATH": str(paths.state_file),
            "TG_STREAM_ENABLED": _resolve_stream_enabled(),
            "TG_STREAM_EDIT_INTERVAL_MS": _env_or_default("TG_STREAM_EDIT_INTERVAL_MS", "700"),
            "TG_STREAM_MIN_DELTA_CHARS": _env_or_default("TG_STREAM_MIN_DELTA_CHARS", "8"),
            "TG_THINKING_STATUS_INTERVAL_MS": _env_or_default("TG_THINKING_STATUS_INTERVAL_MS", "900"),
            "TG_HTTP_MAX_RETRIES": _env_or_default("TG_HTTP_MAX_RETRIES", "2"),
            "TG_HTTP_RETRY_BASE_MS": _env_or_default("TG_HTTP_RETRY_BASE_MS", "300"),
            "TG_HTTP_RETRY_MAX_MS": _env_or_default("TG_HTTP_RETRY_MAX_MS", "3000"),
            "TG_INSTANCE_LOCK_PATH": str(paths.lock_base),
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


def _probe_instance_lock(environ: MutableMapping[str, str], paths: RuntimePaths) -> tuple[bool, str]:
    token = _env_value(environ, "TELEGRAM_BOT_TOKEN")
    if not token:
        return True, ""

    lock = BotInstanceLock(paths.lock_base, token)
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


def _unsupported_storage_schema_message(db_path: Path) -> Optional[str]:
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise CliError(f"无法检查 storage schema: {db_path} ({exc})") from exc

    version = int(row[0] if row else 0)
    if version in (0, SCHEMA_VERSION):
        return None
    return (
        f"storage schema {version} is not supported by this build "
        f"(expected {SCHEMA_VERSION}, db={db_path}); run `uv run storage rebuild`"
    )


def _wait_until_stopped(pid: int, timeout_sec: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.2)
    return not _pid_exists(pid)


def _wait_for_ready(proc: subprocess.Popen[bytes], paths: RuntimePaths, timeout_sec: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        worker_state = read_state(paths.worker_state_file)
        phase = worker_state.get("phase")
        if phase == "running":
            return True
        if phase == "crashed":
            return False
        if proc.poll() is not None:
            return phase == "running"
        if paths.log_file.is_file():
            lines = _tail_last_lines(paths.log_file, lines=40)
            if any(READY_MARKER in line for line in lines):
                return True
        time.sleep(0.2)
    return False


def start() -> int:
    validate_tg_config()
    validate_shared_config()

    if not has_tg_config():
        print("[error] 未配置 TELEGRAM_BOT_TOKEN")
        print("请配置环境变量：")
        print("  TELEGRAM_BOT_TOKEN=123456:xxxx")
        print("  ALLOWED_TELEGRAM_USER_IDS=123456789  # 可选，推荐")
        return 1

    paths = _require_runtime_paths()
    paths.instance_dir.mkdir(parents=True, exist_ok=True)
    child_env = _build_child_env(paths)
    lock_ok, lock_msg = _probe_instance_lock(child_env, paths)
    if not lock_ok:
        print(f"[error] 检测到同 token 实例已运行，拒绝启动: {lock_msg}")
        print("[hint] 请先执行 uv run stop，或手动停止占用进程后重试。")
        return 1

    running, pid = tg_is_running(paths)
    if running and pid is not None:
        print(f"[info] Telegram 已运行，PID={pid}")
        return 0

    schema_error = _unsupported_storage_schema_message(paths.db_file)
    if schema_error is not None:
        print(f"[error] {schema_error}")
        return 1

    print("[info] 启动 Telegram 服务...")
    worker_executable = _env_value(os.environ, "TIYA_WORKER_EXECUTABLE")
    cmd = [worker_executable] if worker_executable else [sys.executable, "-m", BOT_MODULE]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    _write_pid_file(paths, proc.pid)
    if _wait_for_ready(proc, paths):
        print(f"[ok] Telegram 已启动，PID={proc.pid}")
        print(f"[ok] Telegram 日志: {paths.log_file}")
        return 0

    if proc.poll() is not None:
        _remove_pid_file(paths)
    print("[error] Telegram 启动失败，最近日志：")
    for line in _tail_last_lines(paths.log_file, lines=50):
        print(line)
    return 1


def stop() -> int:
    paths = _resolve_existing_runtime_paths()
    if paths is None:
        print("[info] Telegram 未运行")
        return 0

    running, pid = tg_is_running(paths)
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

    _remove_pid_file(paths)
    print(f"[ok] Telegram 已停止，PID={pid}")
    return 0


def status() -> int:
    paths = _resolve_existing_runtime_paths()
    if paths is None:
        print("[info] Telegram 未运行")
        return 0

    running, pid = tg_is_running(paths)
    if running and pid is not None:
        print(f"[ok] Telegram 运行中，PID={pid}")
        print(f"[ok] 运行目录: {paths.instance_dir}")
    else:
        print("[info] Telegram 未运行")
    return 0


def logs() -> int:
    paths = _resolve_existing_runtime_paths()
    if paths is None:
        print("[info] 尚无日志")
        return 0

    paths.instance_dir.mkdir(parents=True, exist_ok=True)
    paths.log_file.touch(exist_ok=True)

    for line in _tail_last_lines(paths.log_file, lines=10):
        print(line)

    try:
        with paths.log_file.open("r", encoding="utf-8", errors="replace") as log_fp:
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


def _resolve_storage_db_path() -> Path:
    token = _env_value(os.environ, "TELEGRAM_BOT_TOKEN")
    if token:
        return RuntimePaths.for_token(token, os.environ).db_file
    instances = list_runtime_instances(os.environ)
    if instances:
        return instances[0].db_file
    return resolve_runtime_home(os.environ) / "storage" / "tiya.db"


def storage() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in {"backup", "stats", "vacuum", "rebuild"}:
        print("[error] 用法: uv run storage <backup DEST|stats|vacuum|rebuild>")
        return 1

    command = args[0]

    async def _run_storage() -> int:
        if command == "rebuild":
            for paths in list_runtime_instances(os.environ):
                running, pid = tg_is_running(paths)
                if running:
                    print(f"[error] 检测到 tiya 实例仍在运行: {paths.instance_dir} (pid={pid})")
                    return 1
            config = load_config()
            runtime_paths = RuntimePaths.for_token(config.telegram_token, os.environ)
            rebuilt_path, backup_path = await StorageManager.rebuild_database(
                db_path=config.storage_path,
                instance_id=runtime_paths.instance_name,
                default_provider=config.default_provider,
                attachments_root=runtime_paths.attachments_dir,
                session_roots={
                    "codex": config.codex_session_root,
                    "claude": config.claude_session_root,
                },
                config_snapshot={
                    "storage_path": str(config.storage_path),
                    "default_provider": config.default_provider,
                    "codex_session_root": str(config.codex_session_root),
                    "claude_session_root": str(config.claude_session_root),
                },
            )
            print(f"[ok] 已重建 storage 数据库: {rebuilt_path}")
            if backup_path is not None:
                print(f"[ok] 已保留旧库备份: {backup_path}")
            return 0

        db_path = _resolve_storage_db_path()
        runtime_root = db_path.parent.parent
        manager = await StorageManager.open(
            StorageConfig(
                db_path=db_path,
                instance_id="cli",
                attachments_root=runtime_root / "instances" / "_storage-cli" / "attachments",
                maintenance_mode=True,
            )
        )
        try:
            if command == "backup":
                if len(args) < 2:
                    print("[error] 缺少备份目标路径")
                    return 1
                destination = Path(args[1]).expanduser()
                await manager.maintenance.backup(destination)
                print(f"[ok] 已备份到: {destination}")
                return 0
            if command == "stats":
                stats = await manager.maintenance.stats()
                print(json.dumps(stats, ensure_ascii=False, indent=2))
                return 0
            await manager.maintenance.vacuum()
            print(f"[ok] 已完成 VACUUM: {db_path}")
            return 0
        finally:
            await manager.close()

    return int(asyncio.run(_run_storage()))


def _bootstrap(*, verbose: bool = True) -> None:
    load_dotenv(verbose=verbose)
    normalize_proxy_env(os.environ)


def _run(command: Callable[[], int], *, verbose: bool = True) -> int:
    try:
        _bootstrap(verbose=verbose)
        return int(command())
    except CliError as exc:
        print(f"[error] {exc}")
        return 1


def entry_start() -> int:
    return main(["start"])


def entry_stop() -> int:
    return main(["stop"])


def entry_restart() -> int:
    return main(["restart"])


def entry_status() -> int:
    return main(["status"])


def entry_logs() -> int:
    return main(["logs"])


def entry_storage() -> int:
    return _run(storage)


def _write_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _read_json_input() -> object:
    raw = sys.stdin.read()
    if not raw.strip():
        raise CliError("stdin is empty; expected a JSON payload")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"invalid JSON payload: {exc}") from exc


def _read_secret_input(explicit: Optional[str]) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    raw = sys.stdin.read().strip()
    if not raw:
        raise CliError("missing secret value")
    return raw


def _status_prefix(phase: str) -> str:
    if phase == "running":
        return "[ok]"
    if phase in {"crashed", "schema_mismatch", "misconfigured"}:
        return "[error]"
    return "[info]"


def _print_service_status(status_payload: dict[str, object]) -> None:
    phase = str(status_payload.get("phase") or "unknown")
    prefix = _status_prefix(phase)
    print(f"{prefix} tiya service phase={phase}")

    desktop_pid_value = status_payload.get("desktopPid")
    supervisor_pid_value = status_payload.get("supervisorPid")
    worker_pid_value = status_payload.get("workerPid")
    launch_id_value = status_payload.get("launchId")
    worker_started_at_value = status_payload.get("workerStartedAt")
    if isinstance(desktop_pid_value, int):
        print(f"[info] desktop pid={desktop_pid_value}")
    if isinstance(supervisor_pid_value, int):
        print(f"[info] supervisor pid={supervisor_pid_value}")
    if isinstance(worker_pid_value, int):
        print(f"[info] worker pid={worker_pid_value}")
    if isinstance(launch_id_value, str) and launch_id_value:
        print(f"[info] launch id={launch_id_value}")
    if isinstance(worker_started_at_value, int):
        print(f"[info] worker started at={worker_started_at_value}")

    runtime_paths = status_payload.get("runtimePaths")
    if isinstance(runtime_paths, dict):
        log_path = runtime_paths.get("logPath") or status_payload.get("logPath")
        env_path = runtime_paths.get("envPath")
        socket_path = runtime_paths.get("socketPath")
        if env_path:
            print(f"[info] env path={env_path}")
        if socket_path:
            print(f"[info] socket path={socket_path}")
        if log_path:
            print(f"[info] log path={log_path}")

    for issue in status_payload.get("blockingIssues", []):
        if isinstance(issue, dict):
            print(f"[error] {issue.get('message')}")
    for warning in status_payload.get("warnings", []):
        print(f"[warn] {warning}")


def _rpc_call(method: str, params: Optional[dict[str, object]] = None) -> dict[str, object]:
    result = call_rpc(method, params=params, environ=os.environ)
    return result


def _cmd_start(_args: argparse.Namespace) -> int:
    _bootstrap()
    result = _rpc_call("service.start")
    output = str(result.get("output") or "").strip()
    if output:
        print(output)
    status_payload = result.get("status")
    if isinstance(status_payload, dict):
        _print_service_status(status_payload)
    return 0 if result.get("started") else 1


def _cmd_stop(_args: argparse.Namespace) -> int:
    _bootstrap()
    result = _rpc_call("service.stop")
    output = str(result.get("output") or "").strip()
    if output:
        print(output)
    status_payload = result.get("status")
    if isinstance(status_payload, dict):
        _print_service_status(status_payload)
    return 0 if result.get("stopped", True) else 1


def _cmd_restart(_args: argparse.Namespace) -> int:
    _bootstrap()
    result = _rpc_call("service.restart")
    output = str(result.get("output") or "").strip()
    if output:
        print(output)
    status_payload = result.get("status")
    if isinstance(status_payload, dict):
        _print_service_status(status_payload)
    return 0 if result.get("restarted") else 1


def _cmd_status(_args: argparse.Namespace) -> int:
    _bootstrap()
    status_payload = _rpc_call("service.status")
    _print_service_status(status_payload)
    return 0


def _cmd_logs(_args: argparse.Namespace) -> int:
    _bootstrap()
    try:
        with SupervisorSubscription("service.subscribe", environ=os.environ) as subscription:
            initial = subscription.initial_result or {}
            status_payload = initial.get("status") if isinstance(initial, dict) else None
            if not isinstance(status_payload, dict):
                status_payload = _rpc_call("service.status")
            log_path = Path(str(status_payload.get("logPath") or "")).expanduser()
            if log_path.is_file():
                for line in _tail_last_lines(log_path, lines=10):
                    print(line)
            for message in subscription.iter_messages():
                if message.get("type") != "event" or message.get("event") != "log_appended":
                    continue
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    continue
                for line in payload.get("lines", []):
                    print(line)
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_supervisor_start(_args: argparse.Namespace) -> int:
    print("[error] standalone supervisor boot is no longer supported; launch tiya desktop instead")
    return 1


def _cmd_supervisor_status(_args: argparse.Namespace) -> int:
    _bootstrap()
    try:
        _print_service_status(_rpc_call("service.status"))
        return 0
    except SupervisorUnavailableError:
        pid = supervisor_pid(os.environ)
        if pid is None:
            print("[info] tiya supervisor 未运行")
        else:
            print(f"[warn] tiya supervisor pid 文件存在但 socket 不可用，PID={pid}")
        return 0


def _cmd_supervisor_stop(_args: argparse.Namespace) -> int:
    _bootstrap()
    stopped = shutdown_supervisor(environ=os.environ)
    if stopped:
        print("[ok] tiya supervisor 已停止")
    else:
        print("[info] tiya supervisor 未运行")
    return 0


def _cmd_storage(args: argparse.Namespace) -> int:
    saved_argv = list(sys.argv)
    try:
        sys.argv = ["storage", *args.storage_args]
        return _run(storage)
    finally:
        sys.argv = saved_argv


def _ctl_service(args: argparse.Namespace) -> int:
    _bootstrap(verbose=False)
    if args.service_command == "subscribe":
        with SupervisorSubscription("service.subscribe", environ=os.environ) as subscription:
            _write_json(subscription.initial_result or {})
            for message in subscription.iter_messages():
                _write_json(message)
        return 0

    result = _rpc_call(f"service.{args.service_command}")
    _write_json(result)
    return 0


def _ctl_config(args: argparse.Namespace) -> int:
    _bootstrap(verbose=False)
    if args.config_command == "get":
        _write_json(_rpc_call("config.get"))
        return 0
    if args.config_command == "validate":
        _write_json(_rpc_call("config.validate", {"payload": _read_json_input()}))
        return 0
    if args.config_command == "set":
        _write_json(_rpc_call("config.set", {"payload": _read_json_input()}))
        return 0
    if args.config_command == "set-secret":
        secret_value = _read_secret_input(args.value)
        _write_json(_rpc_call("config.setSecret", {"value": secret_value}))
        return 0
    _write_json(_rpc_call("config.clearSecret"))
    return 0


def _ctl_sessions(args: argparse.Namespace) -> int:
    _bootstrap(verbose=False)
    if args.sessions_command == "list":
        params: dict[str, object] = {"provider": args.provider, "limit": args.limit}
        if args.telegram_user_id is not None:
            params["telegramUserId"] = args.telegram_user_id
        _write_json(_rpc_call("sessions.list", params))
        return 0
    _write_json(
        _rpc_call(
            "sessions.history",
            {
                "provider": args.provider,
                "sessionId": args.session_id,
                "limit": args.limit,
            },
        )
    )
    return 0


def _ctl_diagnostics(args: argparse.Namespace) -> int:
    _bootstrap(verbose=False)
    if args.diagnostics_command == "report":
        _write_json(_rpc_call("diagnostics.report"))
        return 0
    params: dict[str, object] = {}
    if args.destination is not None:
        params["destinationPath"] = args.destination
    _write_json(_rpc_call("diagnostics.export", params))
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    _bootstrap()
    return _ctl_diagnostics(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tiya")
    subparsers = parser.add_subparsers(dest="command")

    parser_start = subparsers.add_parser("start")
    parser_start.set_defaults(func=_cmd_start)

    parser_stop = subparsers.add_parser("stop")
    parser_stop.set_defaults(func=_cmd_stop)

    parser_restart = subparsers.add_parser("restart")
    parser_restart.set_defaults(func=_cmd_restart)

    parser_status = subparsers.add_parser("status")
    parser_status.set_defaults(func=_cmd_status)

    parser_logs = subparsers.add_parser("logs")
    parser_logs.set_defaults(func=_cmd_logs)

    parser_diagnostics = subparsers.add_parser("diagnostics")
    diagnostics_subparsers = parser_diagnostics.add_subparsers(dest="diagnostics_command")
    diagnostics_report = diagnostics_subparsers.add_parser("report")
    diagnostics_report.set_defaults(func=_cmd_diagnostics)
    diagnostics_export = diagnostics_subparsers.add_parser("export")
    diagnostics_export.add_argument("destination", nargs="?")
    diagnostics_export.set_defaults(func=_cmd_diagnostics)

    parser_storage = subparsers.add_parser("storage")
    parser_storage.add_argument("storage_args", nargs=argparse.REMAINDER)
    parser_storage.set_defaults(func=_cmd_storage)

    parser_supervisor = subparsers.add_parser("supervisor", help="internal compatibility commands")
    supervisor_subparsers = parser_supervisor.add_subparsers(dest="supervisor_command")
    supervisor_start = supervisor_subparsers.add_parser("start", help="unsupported direct boot; launch desktop instead")
    supervisor_start.set_defaults(func=_cmd_supervisor_start)
    supervisor_status = supervisor_subparsers.add_parser("status", help="inspect the attached desktop-owned supervisor")
    supervisor_status.set_defaults(func=_cmd_supervisor_status)
    supervisor_stop = supervisor_subparsers.add_parser("stop", help="stop the attached desktop-owned supervisor")
    supervisor_stop.set_defaults(func=_cmd_supervisor_stop)

    parser_ctl = subparsers.add_parser("ctl")
    ctl_subparsers = parser_ctl.add_subparsers(dest="ctl_group")

    ctl_service = ctl_subparsers.add_parser("service")
    ctl_service_subparsers = ctl_service.add_subparsers(dest="service_command")
    for command_name in ("status", "start", "stop", "restart", "subscribe"):
        ctl_service_command = ctl_service_subparsers.add_parser(command_name)
        ctl_service_command.set_defaults(func=_ctl_service)

    ctl_config = ctl_subparsers.add_parser("config")
    ctl_config_subparsers = ctl_config.add_subparsers(dest="config_command")
    ctl_config_get = ctl_config_subparsers.add_parser("get")
    ctl_config_get.set_defaults(func=_ctl_config)
    ctl_config_validate = ctl_config_subparsers.add_parser("validate")
    ctl_config_validate.set_defaults(func=_ctl_config)
    ctl_config_set = ctl_config_subparsers.add_parser("set")
    ctl_config_set.set_defaults(func=_ctl_config)
    ctl_config_set_secret = ctl_config_subparsers.add_parser("set-secret")
    ctl_config_set_secret.add_argument("value", nargs="?")
    ctl_config_set_secret.set_defaults(func=_ctl_config)
    ctl_config_clear_secret = ctl_config_subparsers.add_parser("clear-secret")
    ctl_config_clear_secret.set_defaults(func=_ctl_config)

    ctl_sessions = ctl_subparsers.add_parser("sessions")
    ctl_sessions_subparsers = ctl_sessions.add_subparsers(dest="sessions_command")
    ctl_sessions_list = ctl_sessions_subparsers.add_parser("list")
    ctl_sessions_list.add_argument("--provider", default="codex")
    ctl_sessions_list.add_argument("--limit", type=int, default=20)
    ctl_sessions_list.add_argument("--telegram-user-id", type=int)
    ctl_sessions_list.set_defaults(func=_ctl_sessions)
    ctl_sessions_history = ctl_sessions_subparsers.add_parser("history")
    ctl_sessions_history.add_argument("session_id")
    ctl_sessions_history.add_argument("--provider", default="codex")
    ctl_sessions_history.add_argument("--limit", type=int, default=20)
    ctl_sessions_history.set_defaults(func=_ctl_sessions)

    ctl_diagnostics = ctl_subparsers.add_parser("diagnostics")
    ctl_diagnostics_subparsers = ctl_diagnostics.add_subparsers(dest="diagnostics_command")
    ctl_diagnostics_report = ctl_diagnostics_subparsers.add_parser("report")
    ctl_diagnostics_report.set_defaults(func=_ctl_diagnostics)
    ctl_diagnostics_export = ctl_diagnostics_subparsers.add_parser("export")
    ctl_diagnostics_export.add_argument("destination", nargs="?")
    ctl_diagnostics_export.set_defaults(func=_ctl_diagnostics)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return int(args.func(args))
    except RpcResponseError as exc:
        if args.command == "ctl":
            _write_json({"error": {"code": exc.code, "message": exc.message}})
        else:
            print(f"[error] {exc.message}")
        return 1
    except SupervisorUnavailableError as exc:
        if args.command == "ctl":
            _write_json({"error": {"code": "supervisor_unavailable", "message": str(exc)}})
        else:
            print(f"[error] {exc}")
        return 1
    except (SupervisorClientError, CliError) as exc:
        if args.command == "ctl":
            _write_json({"error": {"code": "cli_error", "message": str(exc)}})
        else:
            print(f"[error] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
