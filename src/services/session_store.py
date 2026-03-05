import json
from pathlib import Path
from typing import Optional, Protocol
from uuid import UUID

from domain.models import SessionMeta


class SessionStoreProtocol(Protocol):
    def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        ...

    def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        ...

    def get_history(self, session_id: str, limit: int = 10) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        ...


class CodexSessionStore:
    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        if not self.root.exists():
            return []
        files = sorted(self.root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        sessions: list[SessionMeta] = []
        for path in files:
            meta = self._parse_session_meta(path)
            if not meta:
                continue
            sessions.append(meta)
            if len(sessions) >= limit:
                break
        return sessions

    def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        if not self.root.exists():
            return None
        for path in self.root.rglob("*.jsonl"):
            meta = self._parse_session_meta(path)
            if meta and meta.session_id == session_id:
                return meta
        return None

    def mark_as_desktop_session(self, session_id: str) -> bool:
        meta = self.find_by_id(session_id)
        if not meta:
            return False
        path = Path(meta.file_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return False
            first = json.loads(lines[0])
            if first.get("type") != "session_meta":
                return False
            payload = first.get("payload") or {}
            changed = False
            if payload.get("source") != "vscode":
                payload["source"] = "vscode"
                changed = True
            if payload.get("originator") != "Codex Desktop":
                payload["originator"] = "Codex Desktop"
                changed = True
            if not changed:
                return True
            first["payload"] = payload
            lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        except Exception:
            return False

    def get_history(self, session_id: str, limit: int = 10) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        meta = self.find_by_id(session_id)
        if not meta:
            return None, []
        path = Path(meta.file_path)
        messages: list[tuple[str, str]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    msg_type = payload.get("type")
                    if msg_type not in ("user_message", "agent_message"):
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    role = "user" if msg_type == "user_message" else "assistant"
                    messages.append((role, message))
        except Exception:
            return meta, []
        if limit > 0:
            messages = messages[-limit:]
        return meta, messages

    @classmethod
    def _parse_session_meta(cls, path: Path) -> Optional[SessionMeta]:
        try:
            with path.open("r", encoding="utf-8") as f:
                first_line = f.readline()
            parsed = json.loads(first_line)
            payload = parsed.get("payload") or {}
            if parsed.get("type") != "session_meta":
                return None
            session_id = payload.get("id")
            if not session_id:
                return None
            title = cls._extract_title(path)
            return SessionMeta(
                session_id=session_id,
                timestamp=payload.get("timestamp", "unknown"),
                cwd=payload.get("cwd", "unknown"),
                file_path=str(path),
                title=title or f"session {session_id[:8]}",
            )
        except Exception:
            return None

    @classmethod
    def _extract_title(cls, path: Path) -> Optional[str]:
        try:
            with path.open("r", encoding="utf-8") as f:
                for _ in range(240):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") != "event_msg":
                        continue
                    payload = evt.get("payload") or {}
                    if payload.get("type") != "user_message":
                        continue
                    message = (payload.get("message") or "").strip()
                    if not message:
                        continue
                    return cls._compact_title(message)
        except Exception:
            return None
        return None

    @staticmethod
    def _compact_title(text: str, limit: int = 46) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"

    @staticmethod
    def compact_message(text: str, limit: int = 320) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"


class ClaudeSessionStore:
    def __init__(self, root: Path):
        self.root = root.expanduser()

    def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        sessions: list[SessionMeta] = []
        for path in self._iter_session_files():
            meta = self._parse_session_meta(path)
            if not meta:
                continue
            sessions.append(meta)
            if len(sessions) >= limit:
                break
        return sessions

    def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        if not self.root.exists():
            return None
        candidate = None
        if self._is_uuid(session_id):
            for path in self._iter_session_files():
                if path.stem == session_id:
                    candidate = path
                    break
        if candidate:
            return self._parse_session_meta(candidate)

        for path in self._iter_session_files():
            meta = self._parse_session_meta(path)
            if meta and meta.session_id == session_id:
                return meta
        return None

    def get_history(self, session_id: str, limit: int = 10) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        meta = self.find_by_id(session_id)
        if not meta:
            return None, []
        path = Path(meta.file_path)
        messages: list[tuple[str, str]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    evt_type = evt.get("type")
                    if evt_type == "user":
                        if evt.get("isMeta"):
                            continue
                        text = self._extract_user_text(evt.get("message"))
                        if text:
                            messages.append(("user", text))
                        continue
                    if evt_type == "assistant":
                        text = self._extract_assistant_text(evt.get("message"))
                        if text:
                            messages.append(("assistant", text))
        except Exception:
            return meta, []

        if limit > 0:
            messages = messages[-limit:]
        return meta, messages

    def _iter_session_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        files = [
            path
            for path in self.root.rglob("*.jsonl")
            if "subagents" not in path.parts and path.is_file()
        ]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def _parse_session_meta(self, path: Path) -> Optional[SessionMeta]:
        session_id = path.stem
        if not self._is_uuid(session_id):
            return None

        cwd = "unknown"
        timestamp = "unknown"
        title: Optional[str] = None
        seen_session_id: Optional[str] = None

        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    line_session_id = evt.get("sessionId")
                    if isinstance(line_session_id, str) and line_session_id:
                        if seen_session_id is None:
                            seen_session_id = line_session_id
                        elif seen_session_id != line_session_id:
                            return None

                    if timestamp == "unknown":
                        evt_timestamp = evt.get("timestamp")
                        if isinstance(evt_timestamp, str) and evt_timestamp:
                            timestamp = evt_timestamp

                    if cwd == "unknown":
                        evt_cwd = evt.get("cwd")
                        if isinstance(evt_cwd, str) and evt_cwd:
                            cwd = evt_cwd

                    if title is None and evt.get("type") == "user" and not evt.get("isMeta"):
                        user_text = self._extract_user_text(evt.get("message"))
                        if user_text:
                            title = CodexSessionStore._compact_title(user_text)
        except Exception:
            return None

        if seen_session_id and seen_session_id != session_id:
            return None

        return SessionMeta(
            session_id=session_id,
            timestamp=timestamp,
            cwd=cwd,
            file_path=str(path),
            title=title or f"session {session_id[:8]}",
        )

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            UUID(value)
            return True
        except Exception:
            return False

    @staticmethod
    def _extract_user_text(message: object) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _extract_assistant_text(message: object) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def compact_message(text: str, limit: int = 320) -> str:
        return CodexSessionStore.compact_message(text, limit=limit)


# Backward-compatible alias for existing imports.
SessionStore = CodexSessionStore
