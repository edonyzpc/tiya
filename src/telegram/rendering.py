import html
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

from ..domain.models import FormattingBackend, FormattingMode, FormattingStyle, LinkPreviewPolicy


CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
FENCED_CODE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,256})\]\((https?://[^\s)]+)\)")
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
PRE_BLOCK_RE = re.compile(r'^(<pre><code(?: class="language-[^"]+")?>)(.*?)(</code></pre>)$', re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
LIST_RE = re.compile(r"^\s*(?:[-*+]|(\d+)\.)\s+(.+)$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
KEY_VALUE_RE = re.compile(r"^([A-Za-z0-9_\-\u4e00-\u9fff（）() /\[\]]{1,64}):\s*(.+)$")
CODE_PLACEHOLDER_RE = re.compile(r"^@@CODE_BLOCK_(\d+)@@$")
HTML_TOKEN_RE = re.compile(r"(<[^>]+>)")
HTML_START_TAG_RE = re.compile(r"<([a-zA-Z0-9]+)(?:\s+[^>]*)?>")
HTML_END_TAG_RE = re.compile(r"</([a-zA-Z0-9]+)>")
HTML_SELF_CLOSING_TAG_RE = re.compile(r"<([a-zA-Z0-9]+)(?:\s+[^>]*)?/>")


class RenderProfile(str, Enum):
    ASSISTANT_FINAL = "assistant_final"
    COMMAND_HELP = "command_help"
    COMMAND_STATUS = "command_status"
    HISTORY = "history"
    SESSIONS = "sessions"
    GENERIC = "generic"


@dataclass(frozen=True)
class RenderedChunk:
    text: str
    parse_mode: Optional[str]
    disable_web_page_preview: Optional[bool]
    fallback_text: str


@dataclass(frozen=True)
class RenderResult:
    chunks: list[RenderedChunk]
    render_mode: str
    parse_errors: int


class RendererBackend(Protocol):
    def render_blocks(self, text: str, profile: RenderProfile) -> tuple[list[str], int]:
        ...


class BuiltinRendererBackend:
    def __init__(self, style: FormattingStyle):
        self.style = style

    def render_blocks(self, text: str, profile: RenderProfile) -> tuple[list[str], int]:
        parse_errors = 0
        if text.count("```") % 2 == 1:
            parse_errors += 1

        source, code_blocks = self._extract_code_blocks(text)
        lines = source.split("\n")
        blocks: list[str] = []
        paragraph: list[str] = []
        quote_lines: list[str] = []
        list_lines: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            value = "\n".join(paragraph).strip()
            paragraph.clear()
            if value:
                blocks.append(self._render_paragraph(value, profile))

        def flush_quote() -> None:
            if not quote_lines:
                return
            rendered = "\n".join(self._render_inline(line, profile) for line in quote_lines if line.strip())
            quote_lines.clear()
            if rendered:
                blocks.append(f"<blockquote>{rendered}</blockquote>")

        def flush_list() -> None:
            if not list_lines:
                return
            rendered_items = [f"• {self._render_inline(item, profile)}" for item in list_lines if item.strip()]
            list_lines.clear()
            if rendered_items:
                blocks.append("\n".join(rendered_items))

        for line in lines:
            raw = line.rstrip()
            code_match = CODE_PLACEHOLDER_RE.fullmatch(raw.strip())
            if code_match:
                flush_paragraph()
                flush_quote()
                flush_list()
                idx = int(code_match.group(1))
                lang, code = code_blocks[idx]
                blocks.append(self._render_code_block(lang, code))
                continue

            if not raw.strip():
                flush_paragraph()
                flush_quote()
                flush_list()
                continue

            heading_match = HEADING_RE.match(raw)
            if heading_match:
                flush_paragraph()
                flush_quote()
                flush_list()
                level = len(heading_match.group(1))
                text_value = heading_match.group(2).strip()
                blocks.append(self._render_heading(level, text_value))
                continue

            quote_match = QUOTE_RE.match(raw)
            if quote_match:
                flush_paragraph()
                flush_list()
                quote_lines.append(quote_match.group(1).strip())
                continue

            list_match = LIST_RE.match(raw)
            if list_match:
                flush_paragraph()
                flush_quote()
                list_lines.append(list_match.group(2).strip())
                continue

            paragraph.append(raw)

        flush_paragraph()
        flush_quote()
        flush_list()
        return blocks, parse_errors

    def _render_heading(self, level: int, value: str) -> str:
        title = self._render_inline(value, RenderProfile.GENERIC)
        if self.style == "strong" and level <= 2:
            return f"<b>{title}</b>\n<code>────────────</code>"
        if level <= 3:
            return f"<b>{title}</b>"
        return title

    def _render_code_block(self, lang: str, code: str) -> str:
        safe_lang = re.sub(r"[^a-zA-Z0-9_+-]", "", (lang or "").strip())
        attr = f' class="language-{safe_lang}"' if safe_lang else ""
        safe_code = html.escape(code or "", quote=False)
        return f"<pre><code{attr}>{safe_code}</code></pre>"

    def _render_paragraph(self, paragraph: str, profile: RenderProfile) -> str:
        lines = paragraph.split("\n")
        rendered_lines: list[str] = []
        for line in lines:
            rendered_lines.append(self._render_profiled_line(line, profile))
        return "\n".join(item for item in rendered_lines if item)

    def _render_profiled_line(self, line: str, profile: RenderProfile) -> str:
        raw = (line or "").strip()
        if not raw:
            return ""

        if self.style == "strong" and profile in (
            RenderProfile.COMMAND_STATUS,
            RenderProfile.HISTORY,
            RenderProfile.SESSIONS,
        ):
            key_value = KEY_VALUE_RE.match(raw)
            if key_value:
                key = html.escape(key_value.group(1).strip(), quote=False)
                value_raw = key_value.group(2).strip()
                value_rendered = self._render_inline(value_raw, profile)
                if "<code>" not in value_rendered and self._should_wrap_code_value(value_raw):
                    value_rendered = f"<code>{html.escape(value_raw, quote=False)}</code>"
                return f"<b>{key}:</b> {value_rendered}"

        return self._render_inline(raw, profile)

    @staticmethod
    def _should_wrap_code_value(value: str) -> bool:
        if not value or len(value) > 120:
            return False
        return any(ch in value for ch in ("/", "\\", "_", "-", ".", ":", "="))

    @staticmethod
    def _extract_code_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
        blocks: list[tuple[str, str]] = []

        def _replace(match: re.Match[str]) -> str:
            lang = (match.group(1) or "").strip()
            body = (match.group(2) or "").strip("\n")
            idx = len(blocks)
            blocks.append((lang, body))
            return f"\n@@CODE_BLOCK_{idx}@@\n"

        replaced = FENCED_CODE_RE.sub(_replace, text)
        return replaced, blocks

    def _render_inline(self, text: str, profile: RenderProfile) -> str:
        working = text
        code_tokens: list[str] = []
        link_tokens: list[tuple[str, str]] = []

        def _replace_inline_code(match: re.Match[str]) -> str:
            token = f"@@INLINE_CODE_{len(code_tokens)}@@"
            code_tokens.append(match.group(1))
            return token

        def _replace_markdown_link(match: re.Match[str]) -> str:
            token = f"@@INLINE_LINK_{len(link_tokens)}@@"
            link_tokens.append((match.group(1), match.group(2)))
            return token

        def _replace_auto_link(match: re.Match[str]) -> str:
            token = f"@@INLINE_LINK_{len(link_tokens)}@@"
            url = match.group(1)
            link_tokens.append((url, url))
            return token

        working = INLINE_CODE_RE.sub(_replace_inline_code, working)
        working = MARKDOWN_LINK_RE.sub(_replace_markdown_link, working)
        working = AUTOLINK_RE.sub(_replace_auto_link, working)
        escaped = html.escape(working, quote=False)

        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", escaped)
        escaped = re.sub(r"~~(.+?)~~", r"<s>\1</s>", escaped)
        escaped = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", escaped)

        for idx, (label, url) in enumerate(link_tokens):
            token = f"@@INLINE_LINK_{idx}@@"
            safe_label = html.escape(label, quote=False)
            safe_url = html.escape(url, quote=True)
            escaped = escaped.replace(token, f'<a href="{safe_url}">{safe_label}</a>')

        for idx, code in enumerate(code_tokens):
            token = f"@@INLINE_CODE_{idx}@@"
            safe_code = html.escape(code, quote=False)
            escaped = escaped.replace(token, f"<code>{safe_code}</code>")

        if profile == RenderProfile.COMMAND_HELP and escaped.startswith("/"):
            escaped = f"<code>{escaped}</code>"
        return escaped


class TelegramMessageRenderer:
    def __init__(
        self,
        enabled: bool,
        final_only: bool,
        style: FormattingStyle,
        mode: FormattingMode,
        link_preview_policy: LinkPreviewPolicy,
        fail_open: bool,
        backend: FormattingBackend = "builtin",
        max_chunk_chars: int = 3500,
    ):
        self.enabled = bool(enabled)
        self.final_only = bool(final_only)
        self.style = style
        self.mode = mode
        self.link_preview_policy = link_preview_policy
        self.fail_open = bool(fail_open)
        self.backend = "builtin" if backend != "builtin" else backend
        self.max_chunk_chars = max(100, min(3900, int(max_chunk_chars)))
        self._builtin_backend = BuiltinRendererBackend(style=style)

    def render_text(self, text: str, profile: RenderProfile) -> RenderResult:
        cleaned = self._sanitize_text(text)
        if not cleaned.strip():
            cleaned = "Codex 没有返回可展示内容。"

        if not self.enabled:
            return self._render_plain(cleaned, render_mode="disabled", parse_errors=0)
        if self.mode == "plain":
            return self._render_plain(cleaned, render_mode="plain", parse_errors=0)

        backend = self._resolve_backend()

        try:
            blocks, parse_errors = backend.render_blocks(cleaned, profile)
            html_chunks = self._chunk_blocks(blocks)
            if not html_chunks:
                html_chunks = [html.escape(cleaned, quote=False)]
            disable_preview = True if self.link_preview_policy == "off" else None
            chunks = [
                RenderedChunk(
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=disable_preview,
                    fallback_text=self._html_to_plain_text(chunk),
                )
                for chunk in html_chunks
            ]
            return RenderResult(
                chunks=chunks,
                render_mode="html",
                parse_errors=parse_errors,
            )
        except Exception:
            if not self.fail_open:
                raise
            return self._render_plain(cleaned, render_mode="plain_fallback", parse_errors=1)

    def _resolve_backend(self) -> RendererBackend:
        # Keep an extension point for future backends.
        return self._builtin_backend

    def _render_plain(self, text: str, render_mode: str, parse_errors: int) -> RenderResult:
        chunks = []
        disable_preview = True if self.link_preview_policy == "off" else None
        for chunk in self._split_plain_chunks(text):
            chunks.append(
                RenderedChunk(
                    text=chunk,
                    parse_mode=None,
                    disable_web_page_preview=disable_preview,
                    fallback_text=chunk,
                )
            )
        return RenderResult(chunks=chunks, render_mode=render_mode, parse_errors=parse_errors)

    def _split_plain_chunks(self, text: str) -> list[str]:
        if len(text) <= self.max_chunk_chars:
            return [text]
        chunks: list[str] = []
        start = 0
        total = len(text)
        while start < total:
            end = min(start + self.max_chunk_chars, total)
            if end < total:
                split_at = text.rfind("\n", start, end)
                if split_at > start:
                    end = split_at + 1
            chunks.append(text[start:end].strip("\n"))
            start = end
        return [chunk for chunk in chunks if chunk]

    def _chunk_blocks(self, blocks: list[str]) -> list[str]:
        if not blocks:
            return []
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        def flush_current() -> None:
            nonlocal current_len
            if not current:
                return
            chunks.append("\n\n".join(current))
            current.clear()
            current_len = 0

        for block in blocks:
            block = (block or "").strip()
            if not block:
                continue
            if len(block) > self.max_chunk_chars:
                flush_current()
                chunks.extend(self._split_large_block(block))
                continue

            sep = 2 if current else 0
            if current_len + sep + len(block) > self.max_chunk_chars:
                flush_current()
            if current:
                current_len += 2 + len(block)
            else:
                current_len = len(block)
            current.append(block)

        flush_current()
        return chunks

    def _split_large_block(self, block: str) -> list[str]:
        pre_match = PRE_BLOCK_RE.match(block)
        if pre_match:
            prefix, content, suffix = pre_match.group(1), pre_match.group(2), pre_match.group(3)
            room = self.max_chunk_chars - len(prefix) - len(suffix)
            if room > 200:
                parts = self._split_text_by_newline(content, room)
                return [f"{prefix}{part}{suffix}" for part in parts]
        return self._split_html_block(block)

    def _split_html_block(self, block: str) -> list[str]:
        if len(block) <= self.max_chunk_chars:
            return [block]

        tokens = [token for token in HTML_TOKEN_RE.split(block) if token]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        open_tags: list[tuple[str, str]] = []

        def closing_tags() -> list[str]:
            return [f"</{name}>" for _, name in reversed(open_tags)]

        def closing_length() -> int:
            return sum(len(tag) for tag in closing_tags())

        def ensure_reopened() -> None:
            nonlocal current_len
            if current or not open_tags:
                return
            for start_tag, _ in open_tags:
                current.append(start_tag)
                current_len += len(start_tag)

        def flush() -> None:
            nonlocal current_len
            if not current:
                return
            chunks.append("".join(current + closing_tags()))
            current.clear()
            current_len = 0

        for token in tokens:
            if token.startswith("<") and token.endswith(">"):
                ensure_reopened()
                if current and current_len + len(token) + closing_length() > self.max_chunk_chars:
                    flush()
                    ensure_reopened()
                current.append(token)
                current_len += len(token)
                self._update_tag_stack(open_tags, token)
                continue

            remaining = token
            while remaining:
                ensure_reopened()
                room = self.max_chunk_chars - current_len - closing_length()
                if room <= 0 and current:
                    flush()
                    ensure_reopened()
                    room = self.max_chunk_chars - current_len - closing_length()
                if room <= 0:
                    break
                if len(remaining) <= room:
                    current.append(remaining)
                    current_len += len(remaining)
                    remaining = ""
                    continue

                split_at = remaining.rfind("\n", 0, room + 1)
                if split_at <= 0:
                    split_at = remaining.rfind(" ", 0, room + 1)
                if split_at <= 0:
                    split_at = room
                part = remaining[:split_at]
                current.append(part)
                current_len += len(part)
                flush()
                remaining = remaining[split_at:].lstrip("\n ")

        if current:
            chunks.append("".join(current + closing_tags()))
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _update_tag_stack(open_tags: list[tuple[str, str]], token: str) -> None:
        if HTML_SELF_CLOSING_TAG_RE.fullmatch(token):
            return
        end_match = HTML_END_TAG_RE.fullmatch(token)
        if end_match:
            name = end_match.group(1)
            for idx in range(len(open_tags) - 1, -1, -1):
                if open_tags[idx][1] == name:
                    del open_tags[idx:]
                    break
            return
        start_match = HTML_START_TAG_RE.fullmatch(token)
        if start_match:
            open_tags.append((token, start_match.group(1)))

    @staticmethod
    def _split_text_by_newline(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        parts: list[str] = []
        start = 0
        total = len(text)
        while start < total:
            end = min(start + limit, total)
            if end < total:
                split_at = text.rfind("\n", start, end)
                if split_at > start:
                    end = split_at + 1
            if end <= start:
                end = min(start + limit, total)
            parts.append(text[start:end])
            start = end
        return [part for part in parts if part]

    @staticmethod
    def _sanitize_text(text: str) -> str:
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = CONTROL_CHAR_RE.sub("", normalized)
        return normalized.strip()

    @staticmethod
    def _html_to_plain_text(text: str) -> str:
        plain = text.replace("<br>", "\n").replace("<br/>", "\n")
        plain = plain.replace("<blockquote>", "").replace("</blockquote>", "\n")
        plain = re.sub(r"<[^>]+>", "", plain)
        plain = html.unescape(plain)
        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
        return plain or "Codex 没有返回可展示内容。"
