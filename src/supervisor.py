from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from . import cli as legacy_cli
from .envfile import read_env_file, write_env_file
from .instance_lock import BotInstanceLock, ExclusiveFileLock
from .logging_utils import configure_logging, get_logger, log
from .managed_config import (
    ConfigPaths,
    build_worker_env,
    load_config_snapshot,
    normalize_snapshot,
    persist_snapshot,
    resolve_config_paths,
    validate_snapshot,
)
from .process_utils import pid_exists, read_process_snapshot
from .runtime_paths import RuntimePaths, resolve_supervisor_paths
from .secret_store import TELEGRAM_TOKEN_SECRET, SecretStore, SecretStoreError, build_secret_store
from .services.storage import StorageConfig, StorageManager
from .worker_state import read_state, write_state


TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")
ABSOLUTE_PATH_RE = re.compile(r"(^|[\s='\"(\[])(/[^ \t\r\n'\"\]\),;]+)")


class RpcError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _read_pid_from_owner(owner: dict[str, Any]) -> Optional[int]:
    pid = owner.get("pid")
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str) and pid.isdigit():
        return int(pid)
    return None


def _sanitize_text(value: str) -> str:
    sanitized = TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    sanitized = re.sub(r"://([^/\s:@]+):([^@\s/]+)@", "://[REDACTED]:[REDACTED]@", sanitized)
    return sanitized


def _redacted_path_placeholder(label: str, value: str, *, keep_basename: bool) -> str:
    placeholder = f"[REDACTED_{label}]"
    if not keep_basename:
        return placeholder
    name = Path(value).name
    return f"{placeholder}/{name}" if name else placeholder


def _redact_paths_in_text(value: str, replacements: dict[str, str]) -> str:
    sanitized = _sanitize_text(value)
    for raw, redacted in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        if raw:
            sanitized = sanitized.replace(raw, redacted)

    def replace_match(match: re.Match[str]) -> str:
        prefix = match.group(1)
        path_value = match.group(2)
        return f"{prefix}{_redacted_path_placeholder('PATH', path_value, keep_basename=True)}"

    return ABSOLUTE_PATH_RE.sub(replace_match, sanitized)


class TiyaSupervisor:
    def __init__(self) -> None:
        self.base_environ = dict(os.environ)
        self.config_paths: ConfigPaths = resolve_config_paths(self.base_environ)
        self.supervisor_paths = resolve_supervisor_paths(self.base_environ)
        self.secret_store: SecretStore = build_secret_store(
            metadata_path=self.config_paths.secret_metadata_file,
            file_path=self.config_paths.secret_file,
        )
        self._server: Optional[asyncio.AbstractServer] = None
        self._stop_event = asyncio.Event()
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._watch_task: Optional[asyncio.Task[None]] = None
        self._worker_wait_task: Optional[asyncio.Task[None]] = None
        self._last_log_path: Optional[Path] = None
        self._last_log_offset = 0
        self._last_status_signature: Optional[str] = None
        self._last_phase: Optional[str] = None
        self._last_worker_pid: Optional[int] = None
        self._token_cache_loaded = False
        self._token_cache: Optional[str] = None
        self._worker_proc: Optional[subprocess.Popen[bytes]] = None
        self._stopping_worker = False
        self._lock = ExclusiveFileLock(self.supervisor_paths.lock_file)
        self._status_cache: Optional[dict[str, object]] = None
        self._status_cache_signature: Optional[str] = None
        self._status_cache_at = 0.0
        self._runner_health_cache_key: Optional[str] = None
        self._runner_health_cache_value: Optional[dict[str, dict[str, object]]] = None
        self._runner_health_cache_at = 0.0
        self._schema_status_cache_key: Optional[str] = None
        self._schema_status_cache_value: Optional[dict[str, object]] = None
        self._schema_status_cache_at = 0.0
        self._recent_activity_cache_key: Optional[str] = None
        self._recent_activity_cache_value: Optional[list[dict[str, object]]] = None
        self._recent_activity_cache_at = 0.0
        self._recent_errors_cache_key: Optional[tuple[str, int, int]] = None
        self._recent_errors_cache_value: list[str] = []
        self._diagnostics_cache_key: Optional[str] = None
        self._diagnostics_cache_value: Optional[dict[str, object]] = None
        self._diagnostics_cache_at = 0.0
        self._session_root_refresh_at: dict[tuple[str, str], float] = {}

    @staticmethod
    def _cache_fresh(cached_at: float, ttl_sec: float) -> bool:
        return cached_at > 0 and (time.monotonic() - cached_at) < ttl_sec

    def _invalidate_runtime_caches(
        self,
        *,
        status: bool = True,
        diagnostics: bool = True,
        checks: bool = False,
        recent_activity: bool = False,
    ) -> None:
        if status:
            self._status_cache = None
            self._status_cache_signature = None
            self._status_cache_at = 0.0
        if diagnostics:
            self._diagnostics_cache_key = None
            self._diagnostics_cache_value = None
            self._diagnostics_cache_at = 0.0
        if checks:
            self._runner_health_cache_key = None
            self._runner_health_cache_value = None
            self._runner_health_cache_at = 0.0
            self._schema_status_cache_key = None
            self._schema_status_cache_value = None
            self._schema_status_cache_at = 0.0
        if recent_activity:
            self._recent_activity_cache_key = None
            self._recent_activity_cache_value = None
            self._recent_activity_cache_at = 0.0

    @staticmethod
    def _status_signature(status: dict[str, object]) -> str:
        return json.dumps(status, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _status_cache_ttl(phase: str) -> float:
        return 0.5 if phase in {"running", "starting", "stopping"} else 2.0

    def _load_token(self, *, refresh: bool = False) -> Optional[str]:
        if refresh or not self._token_cache_loaded:
            self._token_cache = self.secret_store.get(TELEGRAM_TOKEN_SECRET)
            self._token_cache_loaded = True
        return self._token_cache

    def _secret_status(self) -> dict[str, object]:
        status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        return {
            "backend": status.backend,
            "available": status.available,
            "telegramToken": {
                "present": status.present,
                "updatedAt": status.updated_at,
            },
        }

    def _snapshot(self) -> dict[str, object]:
        return load_config_snapshot(
            paths=self.config_paths,
            secret_status=self.secret_store.get_status(TELEGRAM_TOKEN_SECRET),
        )

    def _current_env_values(self) -> dict[str, str]:
        snapshot = self._snapshot()
        return normalize_snapshot(snapshot)

    def _secret_backend_unavailable_message(self, backend_name: str) -> str:
        if backend_name == "secret-service":
            return (
                "Linux secret backend is unavailable. Install secret-tool "
                "(Debian/Ubuntu: apt install libsecret-tools) and relaunch the desktop."
            )
        if backend_name == "keychain":
            return "macOS Keychain CLI is unavailable. Ensure the system security tool is present and relaunch the desktop."
        return "Secret backend is unavailable on this host."

    def _redact_diagnostics_report(self, report: dict[str, object]) -> dict[str, object]:
        redacted = json.loads(json.dumps(report))
        path_replacements: dict[str, str] = {}

        def replace_path(field: str, label: str, *, keep_basename: bool) -> None:
            value = redacted.get(field)
            if not isinstance(value, str) or not value:
                return
            replacement = _redacted_path_placeholder(label, value, keep_basename=keep_basename)
            path_replacements[value] = replacement
            redacted[field] = replacement

        replace_path("envPath", "ENV_PATH", keep_basename=True)
        replace_path("runtimeRoot", "RUNTIME_ROOT", keep_basename=False)
        replace_path("socketPath", "SOCKET_PATH", keep_basename=True)
        replace_path("storagePath", "STORAGE_PATH", keep_basename=True)
        replace_path("logPath", "LOG_PATH", keep_basename=True)

        lock_status = redacted.get("lockStatus")
        if isinstance(lock_status, dict):
            path_value = lock_status.get("path")
            if isinstance(path_value, str) and path_value:
                replacement = _redacted_path_placeholder("LOCK_PATH", path_value, keep_basename=True)
                path_replacements[path_value] = replacement
                lock_status["path"] = replacement
            message = lock_status.get("message")
            if isinstance(message, str):
                lock_status["message"] = _redact_paths_in_text(message, path_replacements)

        session_roots = redacted.get("sessionRoots")
        if isinstance(session_roots, list):
            for item in session_roots:
                if not isinstance(item, dict):
                    continue
                path_value = item.get("path")
                if not isinstance(path_value, str) or not path_value:
                    continue
                replacement = _redacted_path_placeholder("SESSION_ROOT", path_value, keep_basename=False)
                path_replacements[path_value] = replacement
                item["path"] = replacement

        recent_errors = redacted.get("recentErrors")
        if isinstance(recent_errors, list):
            redacted["recentErrors"] = [
                _redact_paths_in_text(item, path_replacements) if isinstance(item, str) else item for item in recent_errors
            ]

        recommended_actions = redacted.get("recommendedActions")
        if isinstance(recommended_actions, list):
            redacted["recommendedActions"] = [
                _redact_paths_in_text(item, path_replacements) if isinstance(item, str) else item for item in recommended_actions
            ]

        return redacted

    def _migrate_legacy_secret(self) -> None:
        secret_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        if secret_status.present or not secret_status.available:
            return

        current_values = read_env_file(self.config_paths.env_file)
        legacy_token = str(current_values.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not legacy_token:
            return
        if not TOKEN_RE.fullmatch(legacy_token):
            log("[warn] skipping legacy TELEGRAM_BOT_TOKEN migration because the value is invalid")
            return

        try:
            self.secret_store.set(TELEGRAM_TOKEN_SECRET, legacy_token)
        except SecretStoreError as exc:
            log(f"[warn] failed to migrate TELEGRAM_BOT_TOKEN into secret store: {exc}")
            return

        current_values.pop("TELEGRAM_BOT_TOKEN", None)
        write_env_file(self.config_paths.env_file, current_values)
        self._token_cache = legacy_token
        self._token_cache_loaded = True
        log("[info] migrated legacy TELEGRAM_BOT_TOKEN from env file into the configured secret store")

    def _runtime_paths(self, token: Optional[str]) -> Optional[RuntimePaths]:
        if not token:
            return None
        return RuntimePaths.for_token(token, self.base_environ)

    def _desktop_pid(self) -> Optional[int]:
        raw = str(self.base_environ.get("TIYA_DESKTOP_PID") or "").strip()
        if raw.isdigit():
            return int(raw)
        parent = os.getppid()
        return parent if parent > 1 else None

    def _desktop_alive(self) -> bool:
        desktop_pid = self._desktop_pid()
        if desktop_pid is None:
            return True
        return pid_exists(desktop_pid)

    def _current_worker_pid(self) -> Optional[int]:
        if self._worker_proc is None:
            return None
        if self._worker_proc.poll() is not None:
            self._worker_proc = None
            return None
        return int(self._worker_proc.pid)

    def _worker_process_status(self, _paths: RuntimePaths, _token: str) -> tuple[bool, Optional[int]]:
        worker_pid = self._current_worker_pid()
        return (worker_pid is not None, worker_pid)

    def _build_worker_overlay(self, *, env_values: dict[str, str], token: str, runtime_paths: RuntimePaths) -> dict[str, str]:
        overlay = build_worker_env(
            base_environ=self.base_environ,
            token=token,
            env_values=env_values,
            runtime_paths=runtime_paths,
        )
        overlay["ENV_FILE"] = str(self.config_paths.env_file)
        overlay["TIYA_HOME"] = str(self.supervisor_paths.root)
        overlay["PYTHONUNBUFFERED"] = "1"
        worker_executable = self.base_environ.get("TIYA_WORKER_EXECUTABLE")
        if worker_executable:
            overlay["TIYA_WORKER_EXECUTABLE"] = worker_executable
        return overlay

    def _worker_command(self) -> list[str]:
        worker_executable = str(self.base_environ.get("TIYA_WORKER_EXECUTABLE") or "").strip()
        if worker_executable:
            return [worker_executable]
        return [sys.executable, "-m", legacy_cli.BOT_MODULE]

    def _foreign_worker_status(self, token: Optional[str], paths: Optional[RuntimePaths], env_values: dict[str, str]) -> dict[str, object]:
        if not token or paths is None:
            return {"conflict": False}

        owner = BotInstanceLock(paths.lock_base, token).read_owner()
        owner_pid = _read_pid_from_owner(owner)
        if owner_pid is None or not pid_exists(owner_pid):
            return {
                "conflict": False,
                "path": str(BotInstanceLock(paths.lock_base, token).path),
            }

        current_pid = self._current_worker_pid()
        if current_pid is not None and owner_pid == current_pid:
            return {
                "conflict": False,
                "path": str(BotInstanceLock(paths.lock_base, token).path),
            }

        owner_started = owner.get("started_at")
        snapshot = read_process_snapshot(owner_pid)
        owner_cmdline = snapshot.cmdline if snapshot is not None else str(owner.get("cmdline") or "")
        message = (
            f"path={BotInstanceLock(paths.lock_base, token).path} "
            f"owner_pid={owner_pid} owner_started_at={owner_started} owner_cmdline={owner_cmdline[:220]!r}"
        )
        return {
            "conflict": True,
            "message": message,
            "path": str(BotInstanceLock(paths.lock_base, token).path),
        }

    def _schema_status(self, paths: Optional[RuntimePaths]) -> dict[str, object]:
        cache_key = str(paths.db_file) if paths is not None else ""
        if (
            self._schema_status_cache_value is not None
            and self._schema_status_cache_key == cache_key
            and self._cache_fresh(self._schema_status_cache_at, 30.0)
        ):
            return self._schema_status_cache_value
        if paths is None:
            status = {"status": "missing", "message": "token is not configured yet"}
            self._schema_status_cache_key = cache_key
            self._schema_status_cache_value = status
            self._schema_status_cache_at = time.monotonic()
            return status
        message = legacy_cli._unsupported_storage_schema_message(paths.db_file)
        if message is None:
            status = {"status": "ok", "message": ""}
        else:
            status = {"status": "mismatch", "message": message}
        self._schema_status_cache_key = cache_key
        self._schema_status_cache_value = status
        self._schema_status_cache_at = time.monotonic()
        return status

    def _lock_status(self, token: Optional[str], paths: Optional[RuntimePaths], env_values: dict[str, str]) -> dict[str, object]:
        return self._foreign_worker_status(token, paths, env_values)

    def _runner_health(self, env_values: dict[str, str]) -> dict[str, dict[str, object]]:
        cache_key = json.dumps(
            {
                "CODEX_BIN": env_values.get("CODEX_BIN") or "",
                "CLAUDE_BIN": env_values.get("CLAUDE_BIN") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if (
            self._runner_health_cache_value is not None
            and self._runner_health_cache_key == cache_key
            and self._cache_fresh(self._runner_health_cache_at, 30.0)
        ):
            return self._runner_health_cache_value
        codex_bin = legacy_cli.resolve_codex_bin(env_values.get("CODEX_BIN") or None)
        claude_bin = legacy_cli.resolve_claude_bin(env_values.get("CLAUDE_BIN") or None)
        health = {
            "codex": {
                "bin": codex_bin,
                "available": legacy_cli._is_runner_available(codex_bin),
            },
            "claude": {
                "bin": claude_bin,
                "available": legacy_cli._is_runner_available(claude_bin),
            },
        }
        self._runner_health_cache_key = cache_key
        self._runner_health_cache_value = health
        self._runner_health_cache_at = time.monotonic()
        return health

    def _read_supervisor_state(self) -> dict[str, object]:
        return read_state(self.supervisor_paths.state_file)

    def _write_supervisor_state(self, **updates: object) -> dict[str, object]:
        state = self._read_supervisor_state()
        state.update(updates)
        state["updatedAt"] = int(time.time())
        write_state(self.supervisor_paths.state_file, state)
        return state

    def _recent_activity(self, paths: Optional[RuntimePaths]) -> list[dict[str, object]]:
        if paths is None or not paths.db_file.is_file():
            return []
        cache_key = str(paths.db_file)
        if (
            self._recent_activity_cache_value is not None
            and self._recent_activity_cache_key == cache_key
            and self._cache_fresh(self._recent_activity_cache_at, 5.0)
        ):
            return self._recent_activity_cache_value
        items: list[dict[str, object]] = []
        try:
            with sqlite3.connect(str(paths.db_file)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        state.telegram_user_id,
                        state.provider,
                        state.active_session_id,
                        state.active_cwd,
                        state.active_run_id,
                        state.pending_interaction_id,
                        state.pending_image_created_at,
                        run_row.started_at AS active_run_started_at,
                        interaction.created_at AS interaction_created_at
                    FROM provider_state state
                    LEFT JOIN runs run_row ON run_row.run_id=state.active_run_id
                    LEFT JOIN interactions interaction ON interaction.interaction_id=state.pending_interaction_id
                    WHERE state.instance_id=?
                    ORDER BY state.telegram_user_id ASC, state.provider ASC
                    """,
                    (paths.instance_name,),
                ).fetchall()
                cache_rows = conn.execute(
                    """
                    SELECT telegram_user_id, provider, position, session_id
                    FROM session_pick_cache
                    WHERE instance_id=?
                    ORDER BY telegram_user_id ASC, provider ASC, position ASC
                    """,
                    (paths.instance_name,),
                ).fetchall()
        except sqlite3.Error:
            return []

        cache_map: dict[tuple[int, str], list[str]] = {}
        for row in cache_rows:
            key = (int(row["telegram_user_id"]), str(row["provider"]))
            cache_map.setdefault(key, []).append(str(row["session_id"]))

        for row in rows:
            user_id = int(row["telegram_user_id"])
            provider = str(row["provider"])
            items.append(
                {
                    "telegramUserId": user_id,
                    "provider": provider,
                    "activeSessionId": row["active_session_id"] if isinstance(row["active_session_id"], str) else None,
                    "activeCwd": row["active_cwd"] if isinstance(row["active_cwd"], str) else None,
                    "activeRunId": row["active_run_id"] if isinstance(row["active_run_id"], str) else None,
                    "pendingInteraction": bool(row["pending_interaction_id"]),
                    "lastSessionIds": cache_map.get((user_id, provider), []),
                    "updatedAt": max(
                        int(row["active_run_started_at"] or 0),
                        int(row["interaction_created_at"] or 0),
                        int(row["pending_image_created_at"] or 0),
                    ),
                }
            )
        self._recent_activity_cache_key = cache_key
        self._recent_activity_cache_value = items
        self._recent_activity_cache_at = time.monotonic()
        return items

    def _service_phase(
        self,
        *,
        secret_present: bool,
        validation_errors: list[str],
        schema_status: dict[str, object],
        lock_status: dict[str, object],
        running: bool,
        worker_state: dict[str, object],
        supervisor_state: dict[str, object],
    ) -> str:
        if not secret_present:
            return "unconfigured"
        if validation_errors:
            return "misconfigured"
        if schema_status.get("status") == "mismatch":
            return "schema_mismatch"
        if self._stopping_worker and running:
            return "stopping"
        if running:
            if worker_state.get("phase") == "running":
                return "running"
            return "starting"
        if worker_state.get("phase") == "crashed" or supervisor_state.get("phase") == "crashed":
            return "crashed"
        return "stopped"

    async def service_status(self, *, force_refresh: bool = False) -> dict[str, object]:
        if self._status_cache is not None and not force_refresh:
            cached_phase = str(self._status_cache.get("phase") or "")
            if self._cache_fresh(self._status_cache_at, self._status_cache_ttl(cached_phase)):
                return self._status_cache

        snapshot = self._snapshot()
        secret_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        env_values = normalize_snapshot(snapshot)
        validation = validate_snapshot(snapshot, secret_present=secret_status.present)
        token = self._load_token()
        runtime_paths = self._runtime_paths(token)
        worker_state = read_state(runtime_paths.worker_state_file) if runtime_paths is not None else {}
        running, worker_pid = (False, None)
        if runtime_paths is not None and token:
            running, worker_pid = self._worker_process_status(runtime_paths, token)
        schema_status = self._schema_status(runtime_paths)
        lock_status = self._lock_status(token, runtime_paths, env_values)
        supervisor_state = self._read_supervisor_state()
        runner_health = self._runner_health(env_values)
        launch_id = supervisor_state.get("launchId")
        worker_started_at = supervisor_state.get("workerStartedAt")

        warnings = list(validation["warnings"])

        blocking_issues = [{"code": "config_invalid", "message": item} for item in validation["errors"]]
        if not secret_status.available:
            blocking_issues.append(
                {
                    "code": "secret_backend_unavailable",
                    "message": self._secret_backend_unavailable_message(str(secret_status.backend)),
                }
            )
        elif not secret_status.present:
            blocking_issues.append({"code": "missing_secret", "message": "Telegram bot token secret is not configured"})
        if schema_status.get("status") == "mismatch":
            blocking_issues.append({"code": "schema_mismatch", "message": str(schema_status["message"])})
        if lock_status.get("conflict") and not running:
            blocking_issues.append({"code": "foreign_worker_process", "message": str(lock_status.get("message") or "")})

        phase = self._service_phase(
            secret_present=secret_status.present,
            validation_errors=list(validation["errors"]),
            schema_status=schema_status,
            lock_status=lock_status,
            running=running,
            worker_state=worker_state,
            supervisor_state=supervisor_state,
        )

        runtime_payload: dict[str, object] = {
            "runtimeRoot": str(self.supervisor_paths.root),
            "supervisorDir": str(self.supervisor_paths.supervisor_dir),
            "socketPath": str(self.supervisor_paths.socket_file),
            "envPath": str(self.config_paths.env_file),
            "supervisorLogPath": str(self.supervisor_paths.log_file),
        }
        if runtime_paths is not None:
            runtime_payload.update(
                {
                    "storagePath": str(runtime_paths.db_file),
                    "logPath": str(runtime_paths.log_file),
                    "instanceDir": str(runtime_paths.instance_dir),
                    "workerStatePath": str(runtime_paths.worker_state_file),
                    "pidPath": str(runtime_paths.pid_file),
                }
            )

        status = {
            "phase": phase,
            "desktopPid": self._desktop_pid(),
            "supervisorPid": os.getpid(),
            "workerPid": worker_pid,
            "launchId": str(launch_id) if isinstance(launch_id, str) and launch_id else None,
            "workerStartedAt": int(worker_started_at) if isinstance(worker_started_at, int) else None,
            "readyAt": worker_state.get("readyAt"),
            "runtimePaths": runtime_payload,
            "schemaStatus": schema_status,
            "runnerHealth": runner_health,
            "blockingIssues": blocking_issues,
            "warnings": warnings,
            "recentActivity": self._recent_activity(runtime_paths),
            "logPath": runtime_payload.get("logPath") or runtime_payload["supervisorLogPath"],
            "secrets": snapshot["secrets"],
        }
        self._write_supervisor_state(phase=phase, supervisorPid=os.getpid(), workerPid=worker_pid, readyAt=worker_state.get("readyAt"))
        self._status_cache = status
        self._status_cache_signature = self._status_signature(status)
        self._status_cache_at = time.monotonic()
        return status

    async def _emit(self, event_name: str, payload: dict[str, object]) -> None:
        dead: list[asyncio.StreamWriter] = []
        for writer in self._subscribers:
            if writer.is_closing():
                dead.append(writer)
                continue
            try:
                writer.write(_json_line({"type": "event", "event": event_name, "payload": payload}))
                await writer.drain()
            except Exception:
                dead.append(writer)
        for writer in dead:
            self._subscribers.discard(writer)

    async def _emit_status(self, status: Optional[dict[str, object]] = None) -> None:
        next_status = status or await self.service_status(force_refresh=True)
        signature = self._status_signature(next_status)
        if signature == self._last_status_signature:
            return

        phase = str(next_status["phase"])
        worker_pid = next_status["workerPid"] if isinstance(next_status["workerPid"], int) else None
        if phase != self._last_phase:
            await self._emit("service_phase_changed", next_status)
        if worker_pid is not None and self._last_worker_pid != worker_pid:
            await self._emit("worker_started", next_status)
        if self._last_worker_pid is not None and worker_pid is None:
            if phase == "crashed":
                await self._emit("worker_crashed", next_status)
            else:
                await self._emit("worker_stopped", next_status)

        self._last_status_signature = signature
        self._last_phase = phase
        self._last_worker_pid = worker_pid
        await self._emit("health_updated", next_status)

    def _spawn_worker(
        self,
        *,
        env_values: dict[str, str],
        token: str,
        launch_id: str,
        worker_started_at: int,
    ) -> tuple[subprocess.Popen[bytes], RuntimePaths]:
        runtime_paths = RuntimePaths.for_token(token, self.base_environ)
        runtime_paths.instance_dir.mkdir(parents=True, exist_ok=True)
        overlay = self._build_worker_overlay(env_values=env_values, token=token, runtime_paths=runtime_paths)
        proc = subprocess.Popen(
            self._worker_command(),
            cwd=str(legacy_cli.REPO_ROOT),
            env=overlay,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )
        legacy_cli._write_pid_file(runtime_paths, proc.pid)
        self._worker_proc = proc
        self._stopping_worker = False
        self._write_supervisor_state(
            phase="starting",
            supervisorPid=os.getpid(),
            workerPid=proc.pid,
            launchId=launch_id,
            workerStartedAt=worker_started_at,
        )
        if self._worker_wait_task is not None:
            self._worker_wait_task.cancel()
        self._worker_wait_task = asyncio.create_task(self._wait_for_worker_exit(proc, runtime_paths))
        return proc, runtime_paths

    async def _wait_for_worker_exit(self, proc: subprocess.Popen[bytes], runtime_paths: RuntimePaths) -> None:
        rc = await asyncio.to_thread(proc.wait)
        if self._worker_proc is proc:
            self._worker_proc = None
        legacy_cli._remove_pid_file(runtime_paths)
        phase = "stopped" if self._stopping_worker else "crashed"
        self._write_supervisor_state(phase=phase, supervisorPid=os.getpid(), workerPid=None, lastReturnCode=rc)
        self._invalidate_runtime_caches(recent_activity=True, diagnostics=True)
        status = await self.service_status(force_refresh=True)
        await self._emit_status(status)

    async def _stop_owned_worker(self, *, token: Optional[str], timeout_sec: float = 10.0) -> bool:
        runtime_paths = self._runtime_paths(token)
        proc = self._worker_proc
        if proc is None or proc.poll() is not None:
            self._worker_proc = None
            if runtime_paths is not None:
                legacy_cli._remove_pid_file(runtime_paths)
            return True

        self._stopping_worker = True
        self._write_supervisor_state(phase="stopping", supervisorPid=os.getpid(), workerPid=proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.2)

        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.to_thread(proc.wait)

        self._worker_proc = None
        self._stopping_worker = False
        if runtime_paths is not None:
            legacy_cli._remove_pid_file(runtime_paths)
        return proc.poll() is not None

    async def start_service(self) -> dict[str, object]:
        snapshot = self._snapshot()
        secret_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        validation = validate_snapshot(snapshot, secret_present=secret_status.present)
        env_values = normalize_snapshot(snapshot)
        token = self._load_token()
        if not validation["ok"] or not token:
            status = await self.service_status(force_refresh=True)
            return {"status": status, "output": "", "started": False}

        owned_worker_pid = self._current_worker_pid()
        if owned_worker_pid is not None:
            status = await self.service_status(force_refresh=True)
            await self._emit_status(status)
            return {"status": status, "output": "", "started": True}

        status = await self.service_status(force_refresh=True)
        if status["blockingIssues"]:
            return {"status": status, "output": "", "started": False}

        launch_id = uuid.uuid4().hex
        worker_started_at = int(time.time())
        proc, runtime_paths = self._spawn_worker(
            env_values=env_values,
            token=token,
            launch_id=launch_id,
            worker_started_at=worker_started_at,
        )
        self._write_supervisor_state(
            phase="starting",
            supervisorPid=os.getpid(),
            workerPid=proc.pid,
            launchId=launch_id,
            workerStartedAt=worker_started_at,
            lastOutput="",
        )
        self._invalidate_runtime_caches(recent_activity=True, diagnostics=True)
        status = await self.service_status(force_refresh=True)
        await self._emit_status(status)
        output = f"[info] supervisor started worker\n[ok] Worker PID={proc.pid}\n[ok] Worker log: {runtime_paths.log_file}"
        return {"status": status, "output": output, "started": True}

    async def stop_service(self) -> dict[str, object]:
        snapshot = self._snapshot()
        env_values = normalize_snapshot(snapshot)
        token = self._load_token()
        if not token:
            status = await self.service_status(force_refresh=True)
            return {"status": status, "output": "", "stopped": True}

        lock_status = self._lock_status(token, self._runtime_paths(token), env_values)
        if self._current_worker_pid() is None:
            status = await self.service_status(force_refresh=True)
            return {"status": status, "output": "", "stopped": not bool(lock_status.get("conflict"))}

        stopped = await self._stop_owned_worker(token=token)
        self._write_supervisor_state(
            phase="stopped" if stopped else "crashed",
            supervisorPid=os.getpid(),
            workerPid=None,
            lastOutput="",
        )
        self._invalidate_runtime_caches(recent_activity=True, diagnostics=True)
        status = await self.service_status(force_refresh=True)
        await self._emit_status(status)
        return {"status": status, "output": "", "stopped": stopped}

    async def restart_service(self) -> dict[str, object]:
        stop_result = await self.stop_service()
        if not stop_result.get("stopped", True):
            return {"status": stop_result["status"], "output": stop_result.get("output", ""), "restarted": False}
        start_result = await self.start_service()
        return {
            "status": start_result["status"],
            "output": start_result.get("output", ""),
            "restarted": bool(start_result.get("started")),
        }

    async def config_get(self) -> dict[str, object]:
        return self._snapshot()

    async def config_validate(self, payload: object) -> dict[str, object]:
        secret_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        return validate_snapshot(payload, secret_present=secret_status.present)

    async def config_set(self, payload: object) -> dict[str, object]:
        validation = await self.config_validate(payload)
        if not validation["ok"]:
            raise RpcError("config_invalid", "configuration is invalid")
        persisted = persist_snapshot(paths=self.config_paths, payload=payload)
        self._invalidate_runtime_caches(checks=True, recent_activity=True)
        await self._emit("config_changed", {"envPath": str(self.config_paths.env_file)})
        status = await self.service_status(force_refresh=True)
        await self._emit_status(status)
        return {
            "env": persisted["env"],
            "secrets": (await self.config_get())["secrets"],
        }

    async def config_set_secret(self, params: dict[str, object]) -> dict[str, object]:
        value = str(params.get("value") or "").strip()
        if not value or not TOKEN_RE.fullmatch(value):
            raise RpcError("secret_invalid", "Telegram bot token is invalid")
        current_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        if not current_status.available:
            raise RpcError(
                "secret_backend_unavailable",
                self._secret_backend_unavailable_message(str(current_status.backend)),
            )
        try:
            status = self.secret_store.set(TELEGRAM_TOKEN_SECRET, value)
        except SecretStoreError as exc:
            raise RpcError("secret_store_error", _sanitize_text(str(exc) or "failed to store Telegram bot token")) from exc
        self._token_cache = value
        self._token_cache_loaded = True
        self._invalidate_runtime_caches(checks=True, recent_activity=True)
        await self._emit("config_changed", {"secret": "telegramToken"})
        status_payload = await self.service_status(force_refresh=True)
        await self._emit_status(status_payload)
        return {
            "present": status.present,
            "updatedAt": status.updated_at,
            "backend": status.backend,
            "available": status.available,
        }

    async def config_clear_secret(self) -> dict[str, object]:
        current_status = self.secret_store.get_status(TELEGRAM_TOKEN_SECRET)
        if not current_status.available:
            raise RpcError(
                "secret_backend_unavailable",
                self._secret_backend_unavailable_message(str(current_status.backend)),
            )
        try:
            status = self.secret_store.clear(TELEGRAM_TOKEN_SECRET)
        except SecretStoreError as exc:
            raise RpcError("secret_store_error", _sanitize_text(str(exc) or "failed to clear Telegram bot token")) from exc
        self._token_cache = None
        self._token_cache_loaded = True
        self._invalidate_runtime_caches(checks=True, recent_activity=True)
        await self._emit("config_changed", {"secret": "telegramToken"})
        status_payload = await self.service_status(force_refresh=True)
        await self._emit_status(status_payload)
        return {
            "present": status.present,
            "updatedAt": status.updated_at,
            "backend": status.backend,
            "available": status.available,
        }

    async def _open_storage(self) -> tuple[StorageManager, RuntimePaths, dict[str, str]]:
        snapshot = self._snapshot()
        env_values = normalize_snapshot(snapshot)
        token = self._load_token()
        if not token:
            raise RpcError("missing_secret", "Telegram bot token secret is not configured")
        runtime_paths = RuntimePaths.for_token(token, self.base_environ)
        manager = await StorageManager.open(
            StorageConfig(
                db_path=runtime_paths.db_file,
                instance_id=runtime_paths.instance_name,
                default_provider=(env_values.get("DEFAULT_PROVIDER") or "codex"),
                attachments_root=runtime_paths.attachments_dir,
                session_roots={
                    "codex": Path(env_values.get("CODEX_SESSION_ROOT") or runtime_paths.root / "codex"),
                    "claude": Path(env_values.get("CLAUDE_SESSION_ROOT") or runtime_paths.root / "claude"),
                },
                config_snapshot={"default_provider": env_values.get("DEFAULT_PROVIDER") or "codex"},
            )
        )
        return manager, runtime_paths, env_values

    def _read_recent_errors(self, log_path: Path) -> list[str]:
        try:
            stat = log_path.stat()
            cache_key = (str(log_path), int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            cache_key = (str(log_path), 0, 0)
        if self._recent_errors_cache_key == cache_key:
            return self._recent_errors_cache_value
        if log_path.is_file():
            recent_errors = [_sanitize_text(line) for line in legacy_cli._tail_last_lines(log_path, lines=20)]
        else:
            recent_errors = []
        self._recent_errors_cache_key = cache_key
        self._recent_errors_cache_value = recent_errors
        return recent_errors

    def _session_root_refresh_due(self, provider: str, root: Path) -> bool:
        cache_key = (provider, str(root.expanduser().resolve()))
        last_refreshed_at = self._session_root_refresh_at.get(cache_key, 0.0)
        return (time.monotonic() - last_refreshed_at) >= 2.0

    def _mark_session_root_refreshed(self, provider: str, root: Path) -> None:
        cache_key = (provider, str(root.expanduser().resolve()))
        self._session_root_refresh_at[cache_key] = time.monotonic()

    async def sessions_list(self, params: dict[str, object]) -> dict[str, object]:
        provider = str(params.get("provider") or "codex")
        limit = max(1, int(params.get("limit") or 20))
        user_id = params.get("telegramUserId")
        manager, _runtime_paths, env_values = await self._open_storage()
        try:
            root = Path(
                env_values.get("CODEX_SESSION_ROOT") if provider == "codex" else env_values.get("CLAUDE_SESSION_ROOT") or ""
            ).expanduser()
            if self._session_root_refresh_due(provider, root):
                await manager.sessions.refresh_session_root(provider, root)  # type: ignore[arg-type]
                self._mark_session_root_refreshed(provider, root)
            sessions = await manager.sessions.list_recent_sessions(provider, root, limit)  # type: ignore[arg-type]
            active_session_id: Optional[str] = None
            if isinstance(user_id, int):
                active_session_id, _cwd = await manager.state.get_active(user_id, provider)  # type: ignore[arg-type]
            return {
                "provider": provider,
                "items": [
                    {
                        "provider": provider,
                        "sessionId": session.session_id,
                        "title": session.title,
                        "cwd": session.cwd,
                        "timestamp": session.timestamp,
                        "sourcePath": session.file_path,
                        "isActiveForUser": session.session_id == active_session_id,
                    }
                    for session in sessions
                ],
            }
        finally:
            await manager.close()

    async def sessions_history(self, params: dict[str, object]) -> dict[str, object]:
        provider = str(params.get("provider") or "codex")
        session_id = str(params.get("sessionId") or "").strip()
        limit = max(1, int(params.get("limit") or 20))
        if not session_id:
            raise RpcError("session_id_required", "sessionId is required")
        manager, _runtime_paths, env_values = await self._open_storage()
        try:
            root = Path(
                env_values.get("CODEX_SESSION_ROOT") if provider == "codex" else env_values.get("CLAUDE_SESSION_ROOT") or ""
            ).expanduser()
            await manager.sessions.refresh_session(provider, root, session_id)  # type: ignore[arg-type]
            meta, messages = await manager.sessions.get_session_history(provider, root, session_id, limit)  # type: ignore[arg-type]
            return {
                "provider": provider,
                "meta": (
                    {
                        "sessionId": meta.session_id,
                        "title": meta.title,
                        "cwd": meta.cwd,
                        "timestamp": meta.timestamp,
                        "sourcePath": meta.file_path,
                    }
                    if meta is not None
                    else None
                ),
                "messages": [{"role": role, "content": content} for role, content in messages],
            }
        finally:
            await manager.close()

    async def diagnostics_report(self) -> dict[str, object]:
        status = await self.service_status()
        runtime_paths = status["runtimePaths"]
        log_path = Path(str(status["logPath"]))
        recent_errors = self._read_recent_errors(log_path)
        session_roots = []
        env_values = self._current_env_values()
        for key in ("CODEX_SESSION_ROOT", "CLAUDE_SESSION_ROOT"):
            path = Path(env_values.get(key) or "").expanduser()
            session_roots.append(
                {
                    "key": key,
                    "path": str(path),
                    "exists": path.exists(),
                    "readable": os.access(path, os.R_OK) if path.exists() else False,
                }
            )
        diagnostics_key = json.dumps(
            {
                "status": self._status_signature(status),
                "log": list(self._recent_errors_cache_key or (str(log_path), 0, 0)),
                "sessionRoots": session_roots,
                "secretStoreStatus": self._secret_status(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if (
            self._diagnostics_cache_value is not None
            and self._diagnostics_cache_key == diagnostics_key
            and self._cache_fresh(self._diagnostics_cache_at, 2.0)
        ):
            return self._diagnostics_cache_value
        report = {
            "envPath": runtime_paths.get("envPath"),
            "runtimeRoot": runtime_paths.get("runtimeRoot"),
            "socketPath": runtime_paths.get("socketPath"),
            "storagePath": runtime_paths.get("storagePath"),
            "logPath": status["logPath"],
            "launchId": status.get("launchId"),
            "workerStartedAt": status.get("workerStartedAt"),
            "lockStatus": self._lock_status(self._load_token(), self._runtime_paths(self._load_token()), env_values),
            "schemaStatus": status["schemaStatus"],
            "runnerHealth": status["runnerHealth"],
            "sessionRoots": session_roots,
            "secretStoreStatus": json.loads(json.dumps(self._secret_status())),
            "recentErrors": recent_errors,
            "recommendedActions": [issue["message"] for issue in status["blockingIssues"]] or ["No blocking issues detected."],
        }
        redacted = self._redact_diagnostics_report(report)
        self._diagnostics_cache_key = diagnostics_key
        self._diagnostics_cache_value = redacted
        self._diagnostics_cache_at = time.monotonic()
        return redacted

    async def diagnostics_export(self, params: dict[str, object]) -> dict[str, object]:
        destination = params.get("destinationPath")
        if isinstance(destination, str) and destination.strip():
            archive_path = Path(destination).expanduser()
        else:
            timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
            archive_path = self.supervisor_paths.supervisor_dir / f"diagnostics-{timestamp}.zip"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        report = await self.diagnostics_report()
        log_excerpt = "\n".join(report["recentErrors"])
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doctor.json", json.dumps(report, ensure_ascii=False, indent=2))
            zf.writestr("logs.txt", log_excerpt)
        return {"path": str(archive_path)}

    async def subscribe(self, writer: asyncio.StreamWriter) -> dict[str, object]:
        self._subscribers.add(writer)
        return {"subscribed": True, "status": await self.service_status()}

    async def dispatch(self, method: str, params: dict[str, object], writer: asyncio.StreamWriter) -> dict[str, object]:
        if method == "supervisor.shutdown":
            self._stop_event.set()
            return {"stopping": True}
        if method == "service.status":
            return await self.service_status()
        if method == "service.start":
            return await self.start_service()
        if method == "service.stop":
            return await self.stop_service()
        if method == "service.restart":
            return await self.restart_service()
        if method == "service.subscribe":
            return await self.subscribe(writer)
        if method == "config.get":
            return await self.config_get()
        if method == "config.validate":
            return await self.config_validate(params.get("payload"))
        if method == "config.set":
            return await self.config_set(params.get("payload"))
        if method == "config.setSecret":
            return await self.config_set_secret(params)
        if method == "config.clearSecret":
            return await self.config_clear_secret()
        if method == "sessions.list":
            return await self.sessions_list(params)
        if method == "sessions.history":
            return await self.sessions_history(params)
        if method == "diagnostics.report":
            return await self.diagnostics_report()
        if method == "diagnostics.export":
            return await self.diagnostics_export(params)
        raise RpcError("method_not_found", f"unknown method: {method}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    writer.write(_json_line({"id": None, "ok": False, "error": {"code": "invalid_json", "message": "invalid JSON"}}))
                    await writer.drain()
                    continue
                request_id = payload.get("id")
                method = payload.get("method")
                params = payload.get("params")
                if not isinstance(method, str):
                    writer.write(
                        _json_line(
                            {"id": request_id, "ok": False, "error": {"code": "invalid_method", "message": "method must be a string"}}
                        )
                    )
                    await writer.drain()
                    continue
                if not isinstance(params, dict):
                    params = {}
                try:
                    result = await self.dispatch(method, params, writer)
                    writer.write(_json_line({"id": request_id, "ok": True, "result": result}))
                except RpcError as exc:
                    writer.write(_json_line({"id": request_id, "ok": False, "error": {"code": exc.code, "message": exc.message}}))
                except Exception as exc:
                    writer.write(
                        _json_line(
                            {
                                "id": request_id,
                                "ok": False,
                                "error": {"code": "internal_error", "message": _sanitize_text(str(exc))},
                            }
                        )
                    )
                await writer.drain()
        finally:
            self._subscribers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _watch_state(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._desktop_alive():
                    log("[info] desktop owner is gone; stopping supervisor")
                    self._stop_event.set()
                    continue
                status = await self.service_status()
                log_path = Path(str(status["logPath"]))
                if self._last_log_path != log_path:
                    self._last_log_path = log_path
                    self._last_log_offset = log_path.stat().st_size if log_path.exists() else 0
                if log_path.exists():
                    size = log_path.stat().st_size
                    if size < self._last_log_offset:
                        self._last_log_offset = 0
                    if size > self._last_log_offset:
                        with log_path.open("r", encoding="utf-8", errors="replace") as fp:
                            fp.seek(self._last_log_offset)
                            chunk = fp.read()
                            self._last_log_offset = fp.tell()
                        lines = [_sanitize_text(line) for line in chunk.splitlines() if line.strip()]
                        if lines:
                            await self._emit("log_appended", {"path": str(log_path), "lines": lines})
                await self._emit_status(status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"[warn] supervisor watch loop failed: {exc!r}")
            await asyncio.sleep(0.5)

    async def serve(self) -> int:
        self.supervisor_paths.root.mkdir(parents=True, exist_ok=True)
        self.supervisor_paths.supervisor_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.supervisor_paths.root, 0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.supervisor_paths.supervisor_dir, 0o700)
        self._migrate_legacy_secret()

        acquired, owner = self._lock.acquire({"role": "tiya-supervisor"})
        if not acquired:
            owner_pid = owner.get("pid")
            log(f"[warn] tiya-supervisor already running (owner_pid={owner_pid})")
            return 1

        if self.supervisor_paths.socket_file.exists():
            self.supervisor_paths.socket_file.unlink()

        self.supervisor_paths.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self._write_supervisor_state(phase="stopped", supervisorPid=os.getpid(), desktopPid=self._desktop_pid())

        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.supervisor_paths.socket_file))
        with contextlib.suppress(OSError):
            os.chmod(self.supervisor_paths.socket_file, 0o600)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop_event.set)

        self._watch_task = asyncio.create_task(self._watch_state())
        log(f"[info] tiya-supervisor listening on {self.supervisor_paths.socket_file}")
        try:
            async with self._server:
                await self._stop_event.wait()
        finally:
            self._stopping_worker = True
            await self._stop_owned_worker(token=self._load_token())
            if self._watch_task is not None:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
            if self._worker_wait_task is not None:
                self._worker_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker_wait_task
            self._subscribers.clear()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self.supervisor_paths.socket_file.unlink()
            with contextlib.suppress(FileNotFoundError):
                self.supervisor_paths.pid_file.unlink()
            self._lock.release()
            self._write_supervisor_state(phase="stopped", supervisorPid=os.getpid(), workerPid=None)
        return 0


def main() -> int:
    supervisor_paths = resolve_supervisor_paths()
    configure_logging(supervisor_paths.log_file)
    try:
        return asyncio.run(TiyaSupervisor().serve())
    except KeyboardInterrupt:
        return 0
    except Exception:
        get_logger().exception("fatal supervisor error")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
