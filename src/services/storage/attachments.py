from __future__ import annotations
import base64
import hashlib
import mimetypes
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...domain.models import AgentProvider
from .runtime import StorageRuntime, StorageSession

_IMAGE_MIME_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


def _now_ts() -> int:
    import time

    return int(time.time())


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_file_name(file_name: Optional[str], fallback: str = "attachment.bin") -> str:
    candidate = Path(file_name or "").name.strip()
    return candidate or fallback


def mime_type_for_name(file_name: Optional[str]) -> Optional[str]:
    if not file_name:
        return None
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or None


def extension_for_mime(mime_type: Optional[str], fallback: str = ".bin") -> str:
    normalized = (mime_type or "").strip().lower()
    if not normalized:
        return fallback
    if normalized in _IMAGE_MIME_EXTENSION_OVERRIDES:
        return _IMAGE_MIME_EXTENSION_OVERRIDES[normalized]
    guessed = mimetypes.guess_extension(normalized)
    if isinstance(guessed, str) and guessed:
        return guessed
    return fallback


@dataclass(frozen=True)
class AttachmentSeed:
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]
    path: Optional[Path] = None
    data: Optional[bytes] = None

    def is_materializable(self) -> bool:
        if self.data is not None:
            return True
        if self.path is None:
            return False
        return self.path.is_file()


class AttachmentStorage:
    def __init__(self, runtime: StorageRuntime, attachments_root: Path):
        self.runtime = runtime
        self.attachments_root = attachments_root.expanduser()

    async def store_file(
        self,
        path: Path,
        *,
        file_name: str,
        mime_type: Optional[str],
        file_size: Optional[int],
        source_kind: str = "telegram",
    ) -> int:
        seed = await self.prepare_seed(
            AttachmentSeed(
                path=path.expanduser(),
                file_name=file_name,
                mime_type=mime_type,
                file_size=file_size,
            )
        )

        async def _store(db: StorageSession) -> int:
            if seed.data is None:
                raise FileNotFoundError(str(path))
            return await self._store_bytes_nontx(
                db,
                seed.data,
                file_name=seed.file_name,
                mime_type=seed.mime_type,
                file_size=seed.file_size,
                source_kind=source_kind,
            )

        return await self.runtime.write(_store)

    async def prepare_seed(self, seed: AttachmentSeed) -> AttachmentSeed:
        if seed.data is not None:
            return seed
        if seed.path is None or not seed.path.is_file():
            return seed
        data = seed.path.read_bytes()
        file_size = seed.file_size if isinstance(seed.file_size, int) else len(data)
        return AttachmentSeed(
            file_name=seed.file_name,
            mime_type=seed.mime_type,
            file_size=file_size,
            data=data,
        )

    async def store_seed(self, db: StorageSession, seed: AttachmentSeed, source_kind: str) -> Optional[int]:
        if seed.data is not None:
            return await self._store_bytes_nontx(
                db,
                seed.data,
                file_name=seed.file_name,
                mime_type=seed.mime_type,
                file_size=seed.file_size,
                source_kind=source_kind,
            )
        if seed.path is not None and seed.path.is_file():
            return await self._store_bytes_nontx(
                db,
                seed.path.read_bytes(),
                file_name=seed.file_name,
                mime_type=seed.mime_type,
                file_size=seed.file_size,
                source_kind=source_kind,
            )
        return None

    async def materialize_ref(
        self,
        attachment_ref_id: int,
        *,
        user_id: int,
        provider: AgentProvider,
        file_name: Optional[str],
    ) -> Path:
        async def _read(db: StorageSession) -> tuple[str, bytes]:
            return await self.read_ref_payload_from_db(db, attachment_ref_id)

        original_file_name, data = await self.runtime.read(_read)
        return self.materialize_payload(
            attachment_ref_id,
            original_file_name=original_file_name,
            data=data,
            user_id=user_id,
            provider=provider,
            file_name=file_name,
        )

    async def materialize_ref_from_db(
        self,
        db: StorageSession,
        attachment_ref_id: int,
        *,
        user_id: int,
        provider: AgentProvider,
        file_name: Optional[str],
    ) -> Path:
        original_file_name, data = await self.read_ref_payload_from_db(db, attachment_ref_id)
        return self.materialize_payload(
            attachment_ref_id,
            original_file_name=original_file_name,
            data=data,
            user_id=user_id,
            provider=provider,
            file_name=file_name,
        )

    async def read_ref_payload_from_db(self, db: StorageSession, attachment_ref_id: int) -> tuple[str, bytes]:
        row = await db.fetch_one(
            """
            SELECT ref.original_file_name, blob.data
            FROM attachment_refs ref
            JOIN attachment_blobs blob ON blob.sha256=ref.blob_sha256
            WHERE ref.id=?
            """,
            (attachment_ref_id,),
            op_name="materialize_attachment_ref",
        )
        if row is None:
            raise FileNotFoundError(f"attachment ref not found: {attachment_ref_id}")
        return str(row["original_file_name"]), bytes(row["data"])

    def materialize_payload(
        self,
        attachment_ref_id: int,
        *,
        original_file_name: str,
        data: bytes,
        user_id: int,
        provider: AgentProvider,
        file_name: Optional[str],
    ) -> Path:
        return self._materialize_row(
            attachment_ref_id,
            original_file_name=original_file_name,
            data=data,
            user_id=user_id,
            provider=provider,
            file_name=file_name,
        )

    async def gc_unreferenced(self, db: StorageSession) -> None:
        await db.execute(
            """
            DELETE FROM attachment_refs
            WHERE NOT EXISTS (
                SELECT 1 FROM provider_state WHERE pending_attachment_ref_id=attachment_refs.id
            )
              AND NOT EXISTS (
                SELECT 1 FROM run_attachments WHERE attachment_ref_id=attachment_refs.id
            )
              AND NOT EXISTS (
                SELECT 1 FROM session_message_attachments WHERE attachment_ref_id=attachment_refs.id
            )
            """,
            op_name="gc_attachment_refs",
        )
        await db.execute(
            """
            DELETE FROM attachment_blobs
            WHERE NOT EXISTS (
                SELECT 1 FROM attachment_refs WHERE blob_sha256=attachment_blobs.sha256
            )
            """,
            op_name="gc_attachment_blobs",
        )

    async def _store_bytes_nontx(
        self,
        db: StorageSession,
        data: bytes,
        *,
        file_name: str,
        mime_type: Optional[str],
        file_size: Optional[int],
        source_kind: str,
    ) -> int:
        sha256 = _sha256_hex(data)
        now = _now_ts()
        await db.execute(
            """
            INSERT INTO attachment_blobs (sha256, data, size_bytes, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (sha256, data, len(data), now),
            op_name="store_attachment_blob",
        )
        return await db.execute_insert(
            """
            INSERT INTO attachment_refs (blob_sha256, created_at, original_file_name, mime_type, file_size, source_kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sha256, now, safe_file_name(file_name), mime_type, file_size, source_kind),
            op_name="store_attachment_ref",
        )

    def _materialize_row(
        self,
        attachment_ref_id: int,
        *,
        original_file_name: str,
        data: bytes,
        user_id: int,
        provider: AgentProvider,
        file_name: Optional[str],
    ) -> Path:
        materialized_dir = self.attachments_root / f"user-{user_id}" / f"provider-{provider}" / f"pending-{attachment_ref_id}"
        materialized_dir.mkdir(parents=True, exist_ok=True)
        path = materialized_dir / safe_file_name(file_name or original_file_name)
        if path.exists():
            return path
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path


def attachment_seed_from_local_path(raw_path: str) -> Optional[AttachmentSeed]:
    candidate = Path(raw_path.strip()).expanduser()
    if not raw_path.strip():
        return None
    file_name = safe_file_name(candidate.name, "attachment.bin")
    mime_type = mime_type_for_name(file_name)
    file_size = None
    if candidate.is_file():
        try:
            file_size = int(candidate.stat().st_size)
        except OSError:
            file_size = None
    return AttachmentSeed(
        path=candidate,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )


def parse_data_url(data_url: str) -> Optional[tuple[Optional[str], bytes]]:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        return None
    header, sep, payload = data_url.partition(",")
    if not sep:
        return None
    meta = header[5:]
    mime_type: Optional[str] = None
    is_base64 = False
    if meta:
        parts = [part for part in meta.split(";") if part]
        if parts:
            if "/" in parts[0]:
                mime_type = parts[0].strip().lower() or None
                parts = parts[1:]
            is_base64 = any(part.strip().lower() == "base64" for part in parts)
    try:
        if is_base64:
            data = base64.b64decode(payload.encode("ascii"), validate=True)
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return None
    return mime_type, data


def attachment_seed_from_data_url(data_url: str, *, fallback_name: str) -> Optional[AttachmentSeed]:
    parsed = parse_data_url(data_url)
    if parsed is None:
        return None
    mime_type, data = parsed
    return AttachmentSeed(
        data=data,
        file_name=safe_file_name(fallback_name, "attachment.bin"),
        mime_type=mime_type,
        file_size=len(data),
    )
