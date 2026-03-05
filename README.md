# tiya

Language: English | [简体中文](README.zh-CN.md)

`tiya` lets you continue local `codex` and `claude` sessions from Telegram.

## Highlights

- Built on `aiogram` (async)
- Polling mode only (no webhook)
- Runtime provider switch via `/provider codex|claude`
- Session list/switch/history for both providers
- Private-chat streaming with fallback chain:
  - `sendMessageDraft`
  - `editMessageText`
  - `typing + final sendMessage`
- `uv`-managed project (`pyproject.toml + uv.lock`)

## Requirements

- Python 3.10+
- `uv`
- Local `codex` and/or `claude` CLI installed and logged in
- Telegram bot token

## Quick Start

### 1) Install dependencies

```bash
uv sync --group dev
```

### 2) Configure environment

`run.sh` auto-loads `.env` from project root. You can either export vars in shell or put them in `.env`.

Example `.env`:

```bash
TELEGRAM_BOT_TOKEN="your bot token"
ALLOWED_TELEGRAM_USER_IDS="123456789"
DEFAULT_CWD="/path/to/your/project"
DEFAULT_PROVIDER="codex"
CODEX_BIN="codex"
CLAUDE_BIN="claude"
TG_STREAM_ENABLED=1
```

Environment variables:

```bash
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # optional, recommended

# Provider
export DEFAULT_PROVIDER=codex                         # codex or claude

# Streaming
export TG_STREAM_ENABLED=1
export TG_STREAM_EDIT_INTERVAL_MS=700
export TG_STREAM_MIN_DELTA_CHARS=8
export TG_THINKING_STATUS_INTERVAL_MS=900

# HTTP retry
export TG_HTTP_MAX_RETRIES=2
export TG_HTTP_RETRY_BASE_MS=300
export TG_HTTP_RETRY_MAX_MS=3000
export TG_PROXY_URL="http://127.0.0.1:7897"         # optional; falls back to HTTPS_PROXY/http_proxy

# Codex
export DEFAULT_CWD="/path/to/your/project"
export CODEX_BIN="codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"
export CODEX_SANDBOX_MODE=""
export CODEX_APPROVAL_POLICY=""
export CODEX_DANGEROUS_BYPASS=0

# Claude
export CLAUDE_BIN="claude"
export CLAUDE_SESSION_ROOT="$HOME/.claude/projects"
export CLAUDE_MODEL=""                                # optional
export CLAUDE_PERMISSION_MODE="default"
```

### 3) Run

```bash
./run.sh start
```

Common commands:

```bash
./run.sh stop
./run.sh status
./run.sh logs
./run.sh restart
```

## Telegram Commands

- `/help`
- `/provider [codex|claude]`
- `/sessions [N]`
- `/use <index|session_id>`
- `/history [index|session_id] [N]`
- `/new [cwd]`
- `/status`
- `/ask <text>`
- Send normal text directly to continue chat

## Project Structure

- `tg_codex_bot.py`: thin compatibility entry
- `src/tg_codex/app.py`: app composition & polling startup
- `src/tg_codex/config.py`: env parsing
- `src/tg_codex/telegram/router.py`: command/callback routing
- `src/tg_codex/telegram/streaming.py`: streaming orchestrator
- `src/tg_codex/telegram/client.py`: Telegram API wrapper with retries
- `src/tg_codex/services/codex_runner.py`: async codex subprocess runner
- `src/tg_codex/services/claude_runner.py`: async claude subprocess runner
- `src/tg_codex/services/session_store.py`: provider-aware session/history reader
- `src/tg_codex/services/state_store.py`: provider-aware JSON state persistence
- `tests/`: pytest suite

## Testing

```bash
uv run pytest
```

## Notes

- Legacy env `TELEGRAM_ENABLE_DRAFT_STREAM` is still honored when `TG_STREAM_ENABLED` is unset.
- Polling mode only by design in current architecture.

## Troubleshooting Slow First Token

If Telegram shows `思考中...` for a long time on simple prompts, the bottleneck is usually the model subprocess network path, not Telegram send latency.

- Ensure proxy vars are valid (`TG_PROXY_URL` or `HTTPS_PROXY`).
- Verify in the same shell:
  - `codex exec --json --skip-git-repo-check "who are you?"`
  - `claude -p --verbose --output-format stream-json "who are you?"`
- `run.sh` normalizes proxy env names (uppercase/lowercase) to avoid common proxy propagation issues.
