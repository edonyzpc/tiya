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

`uv run start|stop|restart|status|logs` auto-loads `.env` from project root. You can either export vars in shell or put them in `.env`.

Example `.env`:

```bash
TELEGRAM_BOT_TOKEN="your bot token"
ALLOWED_TELEGRAM_USER_IDS="123456789"
ALLOWED_CWD_ROOTS="/path/to/your/project"
DEFAULT_CWD="/path/to/your/project"
DEFAULT_PROVIDER="codex"
CODEX_BIN="codex"
CLAUDE_BIN="claude"
TG_STREAM_ENABLED=1
TG_FORMATTING_ENABLED=1
TG_FORMATTING_MODE="html"
TG_FORMATTING_STYLE="strong"
```

Environment variables:

```bash
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # optional, recommended

# Provider
export DEFAULT_PROVIDER=codex                         # codex or claude
export ALLOWED_CWD_ROOTS="/path/to/allowed-a,/path/to/allowed-b"  # optional

# Streaming
export TG_STREAM_ENABLED=1
export TG_STREAM_EDIT_INTERVAL_MS=700
export TG_STREAM_MIN_DELTA_CHARS=8
export TG_THINKING_STATUS_INTERVAL_MS=900
export TG_STREAM_RETRY_COOLDOWN_MS=15000
export TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS=2
export TG_STREAM_PREVIEW_FAILFAST=1

# Message formatting / rendering
export TG_FORMATTING_ENABLED=1
export TG_FORMATTING_STYLE="strong"                  # light | medium | strong
export TG_FORMATTING_MODE="html"                     # html | plain
export TG_LINK_PREVIEW_POLICY="auto"                 # auto | off
export TG_FORMATTING_FAIL_OPEN=1

# HTTP retry
export TG_HTTP_MAX_RETRIES=2
export TG_HTTP_RETRY_BASE_MS=300
export TG_HTTP_RETRY_MAX_MS=3000

# Runtime home (optional)
export TIYA_HOME="$HOME/.local/state/tiya"
# For repo-local runtime files during development:
# export TIYA_HOME="$(pwd)/.runtime"

# Optional explicit lock path override
# export TG_INSTANCE_LOCK_PATH="/custom/path/bot.lock"

# Optional proxy (use when VPN / network policy requires it)
export TG_PROXY_URL="http://127.0.0.1:7897"
# or:
export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"

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
uv run start
```

Common commands:

```bash
uv run stop
uv run status
uv run logs
uv run restart
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

- `tiya.py`: startup entry
- `src/runtime_paths.py`: token-scoped runtime path resolution
- `src/cli.py`: service manager (`start|stop|restart|status|logs`)
- `src/app.py`: app composition & polling startup
- `src/config.py`: env parsing
- `src/telegram/router.py`: command/callback routing
- `src/telegram/streaming.py`: streaming orchestrator
- `src/telegram/client.py`: Telegram API wrapper with retries
- `src/services/codex_runner.py`: async codex subprocess runner
- `src/services/claude_runner.py`: async claude subprocess runner
- `src/services/session_store.py`: provider-aware session/history reader
- `src/services/state_store.py`: provider-aware JSON state persistence
- `tests/`: pytest suite

## Testing

```bash
uv run pytest
```

## Notes

- Legacy env `TELEGRAM_ENABLE_DRAFT_STREAM` is still honored when `TG_STREAM_ENABLED` is unset.
- `TG_FORMATTING_FINAL_ONLY` and `TG_FORMATTING_BACKEND` are deprecated and ignored.
- Polling mode only by design in current architecture.
- `run.sh` has been removed. Use `uv run <command>` only.
- Start with `uv run start` only. Do not run extra polling processes (for example `python -m tg_codex`) with the same bot token.
- Runtime files are stored under `TIYA_HOME/instances/<token_hash>/`.
- `CODEX_DANGEROUS_BYPASS` should be used only for trusted users and trusted hosts.

## Troubleshooting Slow First Token

If Telegram shows `思考中...` for a long time on simple prompts, the bottleneck is usually the model subprocess network path, not Telegram send latency.

- Ensure optional proxy vars are valid when VPN is enabled (`TG_PROXY_URL` or `HTTPS_PROXY`).
- Verify in the same shell:
  - `codex exec --json --skip-git-repo-check "who are you?"`
  - `claude -p --verbose --output-format stream-json "who are you?"`
- `uv run start` normalizes proxy env names (uppercase/lowercase) to avoid common proxy propagation issues.

## Troubleshooting Stream Stuck Halfway

- `tiya` now enforces a per-token instance lock and stores PID/log/state per token.
- If startup fails with "instance lock rejected", stop old process first:
  - `uv run stop`
  - `ps -ef | rg "python -m src|tiya.py"`
- During Telegram rate limits, preview stream can auto-degrade to `typing + final message`; final answer delivery is still guaranteed.
