# Telegram Setup Guide

This guide explains how to run `tiya` with the current `aiogram + uv` architecture.

## Requirements

- Python 3.10+
- `uv`
- Local `codex` and/or `claude` CLI installed and logged in
- A Telegram bot token from `@BotFather`

## 1) Install dependencies

```bash
uv sync --group dev
```

## 2) Configure env vars

`uv run start|stop|restart|status|logs` automatically loads `.env` from the project root on startup.

```bash
export TELEGRAM_BOT_TOKEN="123456:xxxx"
export ALLOWED_TELEGRAM_USER_IDS="123456789"  # optional but recommended
export ALLOWED_CWD_ROOTS="/path/to/project"   # optional

export DEFAULT_PROVIDER="codex"                # codex or claude
export DEFAULT_CWD="/path/to/project"

# Optional if `codex` is already on PATH.
# macOS auto-discovery also checks:
#   /Applications/Codex.app/Contents/Resources/codex
#   ~/Applications/Codex.app/Contents/Resources/codex
export CODEX_BIN="codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"

# Optional if `claude` is already on PATH.
# macOS auto-discovery also checks:
#   /opt/homebrew/bin/claude
#   /usr/local/bin/claude
#   ~/.local/bin/claude
export CLAUDE_BIN="claude"
export CLAUDE_SESSION_ROOT="$HOME/.claude/projects"
export CLAUDE_MODEL=""                         # optional
export CLAUDE_PERMISSION_MODE="default"

export TG_STREAM_ENABLED=1
export TG_STREAM_EDIT_INTERVAL_MS=700
export TG_STREAM_MIN_DELTA_CHARS=8
export TG_THINKING_STATUS_INTERVAL_MS=900
export TG_STREAM_RETRY_COOLDOWN_MS=15000
export TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS=2
export TG_STREAM_PREVIEW_FAILFAST=1

export TG_FORMATTING_ENABLED=1
export TG_FORMATTING_STYLE="strong"         # light | medium | strong
export TG_FORMATTING_MODE="html"            # html | plain
export TG_LINK_PREVIEW_POLICY="auto"        # auto | off
export TG_FORMATTING_FAIL_OPEN=1

export TG_HTTP_MAX_RETRIES=2
export TG_HTTP_RETRY_BASE_MS=300
export TG_HTTP_RETRY_MAX_MS=3000
# Linux default: ~/.local/state/tiya
# macOS default: ~/Library/Application Support/tiya
export TIYA_HOME="$HOME/.local/state/tiya"
# For repo-local runtime files during development:
# export TIYA_HOME="$(pwd)/.runtime"

# Optional proxy (only if VPN / network policy needs it)
export TG_PROXY_URL="http://127.0.0.1:7897"
# or:
export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"
```

## 3) Start service

```bash
uv run start
```

## 4) Verify

```bash
uv run status
uv run logs
```

If logs show `tiya service ready`, the bot is running.

## 5) Switch provider in Telegram

Use:

```text
/provider
/provider claude
/provider codex
```

## 6) Stop / restart

```bash
uv run stop
uv run restart
```

## Notes

- Polling mode only.
- Legacy variable `TELEGRAM_ENABLE_DRAFT_STREAM` is still supported as fallback.
- `TG_FORMATTING_FINAL_ONLY` and `TG_FORMATTING_BACKEND` are deprecated and ignored.
- `run.sh` has been removed. Use `uv run <command>`.
- Use `uv run start` as the only startup entry. Avoid extra polling process for the same token (for example `python -m tg_codex`).
- macOS support follows the same CLI lifecycle as Linux. `launchd` integration is not included in this release.
