# tg-codex

Language: English | [简体中文](README.zh-CN.md)

`tg-codex` lets you run and continue local `codex` sessions from Telegram.

## Features

- List local session history with titles
- Switch to an existing session and continue asking
- Create new sessions and control working directory
- View recent messages in a session (`/history`)
- Run Telegram bot to control Codex CLI remotely

## Requirements

- Python 3.9+
- Local `codex` installed and already logged in
- Telegram: `TELEGRAM_BOT_TOKEN`

## Quick Start

### 1) Configure environment variables

```bash
# Telegram (required)
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # optional, recommended

# Shared (optional)
export DEFAULT_CWD="/path/to/your/project"
export CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"
export CODEX_SANDBOX_MODE=""                         # optional: used only when CODEX_DANGEROUS_BYPASS=1
export CODEX_APPROVAL_POLICY=""                      # optional: used only when CODEX_DANGEROUS_BYPASS=1
export CODEX_DANGEROUS_BYPASS=0                      # 0/1/2 (see permission section below)
```

### 2) Start services

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

## Setup Guide

See [SETUP_TELEGRAM.md](SETUP_TELEGRAM.md) for detailed step-by-step setup instructions.

## Permission Switches & Risks

Permission behavior is controlled by `CODEX_DANGEROUS_BYPASS`:

- `0` (default): no extra permission flags (least privilege)
- `1`: enable permission flags
  - `CODEX_SANDBOX_MODE` defaults to `danger-full-access` (override allowed)
  - `CODEX_APPROVAL_POLICY` defaults to `never` (override allowed)
- `2`: append `--dangerously-bypass-approvals-and-sandbox`

Notes:
- `CODEX_SANDBOX_MODE` / `CODEX_APPROVAL_POLICY` are applied only when `CODEX_DANGEROUS_BYPASS=1`
- `CODEX_DANGEROUS_BYPASS=2` takes full bypass path

Risk notes:

- It may execute arbitrary commands and modify/delete local files
- It may read and exfiltrate sensitive data (keys, configs, source code)
- Enable only in controlled environments and switch back to `0` afterward

## Commands

- `/help`
- `/sessions [N]`: list recent `N` sessions (title + index)
- `/use <index|session_id>`: switch active session
- `/history [index|session_id] [N]`: show latest `N` messages (default 10, max 50)
- `/new [cwd]`: enter new-session mode; next normal message creates a new session
- `/status`: show current active session
- `/ask <text>`: ask in the current session
- Send normal text directly: continue current session, or create one if in new-session mode

Tips:

- After `/sessions`, send an index directly (for example `1`) to switch

## Additional Scripts

- `tg_codex_bot.py`: Telegram service entry
- `run.sh`: Process management script

## Known Limitations

- New sessions are mainly visible in terminal/CLI session history
- Codex Desktop may need restart before newly continued sessions become visible
- Replies are returned after each request finishes (no streaming push yet)