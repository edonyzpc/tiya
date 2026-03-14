from __future__ import annotations

import json
import os
import signal
import socket
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .runtime_paths import resolve_supervisor_paths


class SupervisorClientError(RuntimeError):
    pass


class SupervisorUnavailableError(SupervisorClientError):
    pass


class RpcResponseError(SupervisorClientError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _supervisor_socket_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    return resolve_supervisor_paths(environ).socket_file


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _open_socket(environ: Optional[Mapping[str, str]] = None, *, timeout_sec: float = 2.0) -> socket.socket:
    path = _supervisor_socket_path(environ)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_sec)
    try:
        client.connect(str(path))
    except FileNotFoundError as exc:
        client.close()
        raise SupervisorUnavailableError(f"desktop-owned supervisor is unavailable: {path}") from exc
    except OSError as exc:
        client.close()
        raise SupervisorUnavailableError(f"failed to connect to the desktop-owned supervisor: {exc}") from exc
    return client


def _send_request(
    sock: socket.socket,
    method: str,
    params: Optional[dict[str, object]] = None,
    *,
    request_id: str = "cli",
) -> None:
    payload = {
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


def _read_message(sock: socket.socket) -> dict[str, Any]:
    fp = sock.makefile("r", encoding="utf-8")
    try:
        raw = fp.readline()
    finally:
        fp.close()
    if not raw:
        raise SupervisorClientError("the desktop-owned supervisor closed the connection unexpectedly")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SupervisorClientError("the desktop-owned supervisor returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SupervisorClientError("the desktop-owned supervisor returned an invalid response payload")
    return payload


def call_rpc(
    method: str,
    params: Optional[dict[str, object]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    timeout_sec: float = 5.0,
) -> dict[str, Any]:
    sock = _open_socket(environ, timeout_sec=timeout_sec)
    try:
        _send_request(sock, method, params)
        response = _read_message(sock)
    finally:
        sock.close()

    if not response.get("ok"):
        error = response.get("error")
        if isinstance(error, dict):
            raise RpcResponseError(str(error.get("code") or "rpc_error"), str(error.get("message") or "rpc failed"))
        raise SupervisorClientError("the desktop-owned supervisor returned an unknown RPC failure")

    result = response.get("result")
    if not isinstance(result, dict):
        raise SupervisorClientError("the desktop-owned supervisor returned a non-object result")
    return result


def supervisor_pid(environ: Optional[Mapping[str, str]] = None) -> Optional[int]:
    pid_file = resolve_supervisor_paths(environ).pid_file
    if not pid_file.is_file():
        return None
    raw = pid_file.read_text(encoding="utf-8").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    if not _pid_running(pid):
        return None
    return pid


def spawn_supervisor(
    *,
    environ: Optional[Mapping[str, str]] = None,
    timeout_sec: float = 8.0,
) -> int:
    raise SupervisorUnavailableError("standalone supervisor boot is no longer supported; launch tiya desktop instead")


def ensure_supervisor_running(
    *,
    environ: Optional[Mapping[str, str]] = None,
    timeout_sec: float = 8.0,
) -> int:
    raise SupervisorUnavailableError("standalone supervisor boot is no longer supported; launch tiya desktop instead")


def shutdown_supervisor(*, environ: Optional[Mapping[str, str]] = None) -> bool:
    try:
        call_rpc("supervisor.shutdown", environ=environ, timeout_sec=2.0)
        return True
    except SupervisorUnavailableError:
        pid = supervisor_pid(environ)
        if pid is None:
            return False
        os.kill(pid, signal.SIGTERM)
        return True


class SupervisorSubscription:
    def __init__(
        self,
        method: str,
        params: Optional[dict[str, object]] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        timeout_sec: float = 5.0,
    ):
        self.method = method
        self.params = params or {}
        self.environ = environ
        self.timeout_sec = timeout_sec
        self._socket: Optional[socket.socket] = None
        self._fp = None
        self.initial_result: Optional[dict[str, Any]] = None

    def __enter__(self) -> "SupervisorSubscription":
        self._socket = _open_socket(self.environ, timeout_sec=self.timeout_sec)
        self._socket.settimeout(None)
        self._fp = self._socket.makefile("r", encoding="utf-8")
        _send_request(self._socket, self.method, self.params)
        raw = self._fp.readline()
        if not raw:
            raise SupervisorClientError("the desktop-owned supervisor closed the subscription unexpectedly")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise SupervisorClientError("the desktop-owned supervisor returned an invalid subscription response")
        if not payload.get("ok"):
            error = payload.get("error")
            if isinstance(error, dict):
                raise RpcResponseError(str(error.get("code") or "rpc_error"), str(error.get("message") or "rpc failed"))
            raise SupervisorClientError("the desktop-owned supervisor returned an invalid subscription failure")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SupervisorClientError("the desktop-owned supervisor returned a non-object subscription result")
        self.initial_result = result
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fp is not None:
            self._fp.close()
        if self._socket is not None:
            self._socket.close()

    def iter_messages(self) -> Iterator[dict[str, Any]]:
        if self._fp is None:
            raise SupervisorClientError("subscription has not been opened")
        while True:
            raw = self._fp.readline()
            if not raw:
                break
            payload = json.loads(raw)
            if isinstance(payload, dict):
                yield payload
