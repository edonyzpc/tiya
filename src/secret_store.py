from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


TELEGRAM_TOKEN_SECRET = "telegram_token"


class SecretStoreError(RuntimeError):
    pass


class SecretBackendError(SecretStoreError):
    pass


@dataclass(frozen=True)
class SecretStatus:
    present: bool
    updated_at: Optional[int]
    backend: str
    available: bool


class SecretBackend:
    name = "unavailable"

    def is_available(self) -> bool:
        return False

    def get(self, name: str) -> Optional[str]:
        return None

    def set(self, name: str, value: str) -> None:
        raise SecretBackendError("secret backend is unavailable")

    def clear(self, name: str) -> None:
        raise SecretBackendError("secret backend is unavailable")


class FileSecretBackend(SecretBackend):
    name = "file"

    def __init__(self, path: Path):
        self.path = path.expanduser()

    def is_available(self) -> bool:
        return True

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def _write(self, payload: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, name: str) -> Optional[str]:
        return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        payload = self._read()
        payload[name] = value
        self._write(payload)

    def clear(self, name: str) -> None:
        payload = self._read()
        if name in payload:
            payload.pop(name, None)
            self._write(payload)


class KeychainSecretBackend(SecretBackend):
    name = "keychain"

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.account_name = os.getenv("USER", "tiya")

    def is_available(self) -> bool:
        return shutil.which("security") is not None

    def get(self, name: str) -> Optional[str]:
        if not self.is_available():
            return None
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                self.account_name,
                "-s",
                f"{self.service_name}.{name}",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set(self, name: str, value: str) -> None:
        if not self.is_available():
            raise SecretBackendError("macOS security CLI is unavailable")
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                self.account_name,
                "-s",
                f"{self.service_name}.{name}",
                "-w",
                value,
                "-U",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SecretBackendError(result.stderr.strip() or "failed to store secret in macOS keychain")

    def clear(self, name: str) -> None:
        if not self.is_available():
            raise SecretBackendError("macOS security CLI is unavailable")
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-a",
                self.account_name,
                "-s",
                f"{self.service_name}.{name}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )


class SecretToolBackend(SecretBackend):
    name = "secret-service"

    def __init__(self, service_name: str):
        self.service_name = service_name

    def is_available(self) -> bool:
        return shutil.which("secret-tool") is not None

    def _attrs(self, name: str) -> list[str]:
        return ["service", self.service_name, "secret", name]

    def get(self, name: str) -> Optional[str]:
        if not self.is_available():
            return None
        result = subprocess.run(
            ["secret-tool", "lookup", *self._attrs(name)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def set(self, name: str, value: str) -> None:
        if not self.is_available():
            raise SecretBackendError("secret-tool is unavailable")
        result = subprocess.run(
            ["secret-tool", "store", "--label", f"{self.service_name} {name}", *self._attrs(name)],
            input=value,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SecretBackendError(result.stderr.strip() or "failed to store secret in Secret Service")

    def clear(self, name: str) -> None:
        if not self.is_available():
            raise SecretBackendError("secret-tool is unavailable")
        subprocess.run(
            ["secret-tool", "clear", *self._attrs(name)],
            capture_output=True,
            text=True,
            check=False,
        )


class SecretStore:
    def __init__(self, backend: SecretBackend, metadata_path: Path):
        self.backend = backend
        self.metadata_path = metadata_path.expanduser()

    def _read_metadata(self) -> dict[str, dict[str, int]]:
        if not self.metadata_path.is_file():
            return {}
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        normalized: dict[str, dict[str, int]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            updated_at = value.get("updated_at")
            if isinstance(updated_at, int):
                normalized[str(key)] = {"updated_at": updated_at}
        return normalized

    def _write_metadata(self, payload: dict[str, dict[str, int]]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.metadata_path.with_name(f".{self.metadata_path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.metadata_path)
        try:
            os.chmod(self.metadata_path, 0o600)
        except OSError:
            pass

    def get_status(self, name: str) -> SecretStatus:
        metadata = self._read_metadata()
        present = self.get(name) is not None
        updated_at = metadata.get(name, {}).get("updated_at")
        return SecretStatus(
            present=present,
            updated_at=updated_at if isinstance(updated_at, int) else None,
            backend=self.backend.name,
            available=self.backend.is_available(),
        )

    def get(self, name: str) -> Optional[str]:
        return self.backend.get(name)

    def set(self, name: str, value: str) -> SecretStatus:
        self.backend.set(name, value)
        metadata = self._read_metadata()
        metadata[name] = {"updated_at": int(time.time())}
        self._write_metadata(metadata)
        return self.get_status(name)

    def clear(self, name: str) -> SecretStatus:
        self.backend.clear(name)
        metadata = self._read_metadata()
        metadata.pop(name, None)
        self._write_metadata(metadata)
        return self.get_status(name)


def build_secret_store(*, metadata_path: Path, file_path: Path) -> SecretStore:
    service_name = "tiya.desktop"
    backend_override = (os.getenv("TIYA_SECRET_STORE_BACKEND") or "").strip().lower()
    system_name = platform.system()

    if backend_override == "file":
        backend: SecretBackend = FileSecretBackend(file_path)
    elif system_name == "Darwin":
        backend = KeychainSecretBackend(service_name)
    elif system_name == "Linux":
        backend = SecretToolBackend(service_name)
    else:
        backend = SecretBackend()

    return SecretStore(backend, metadata_path)
