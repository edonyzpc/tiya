from __future__ import annotations

from pathlib import Path

from src.secret_store import FileSecretBackend, SecretStore


def test_file_secret_store_round_trip(tmp_path: Path):
    store = SecretStore(FileSecretBackend(tmp_path / "secrets.json"), tmp_path / "metadata.json")

    initial = store.get_status("telegram_token")
    assert initial.present is False
    assert initial.available is True

    written = store.set("telegram_token", "123456:abcdefghijklmnopqrstuvwxyz12345")
    assert written.present is True
    assert isinstance(written.updated_at, int)
    assert store.get("telegram_token") == "123456:abcdefghijklmnopqrstuvwxyz12345"

    cleared = store.clear("telegram_token")
    assert cleared.present is False
    assert store.get("telegram_token") is None
