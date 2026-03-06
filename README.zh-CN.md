# tiya

语言: [English](README.md) | 简体中文

`tiya` 用于在 Telegram 中继续本地 `codex` 与 `claude` 会话。

## 特性

- 基于 `aiogram`（异步）
- 仅支持长轮询（不含 webhook）
- 通过 `/provider codex|claude` 运行时切换 provider
- 两个 provider 都支持会话列表/切换/历史
- 私聊流式回退链：
  - `sendMessageDraft`
  - `editMessageText`
  - `typing + final sendMessage`
- 使用 `uv` 管理依赖（`pyproject.toml + uv.lock`）

## 环境要求

- Python 3.10+
- `uv`
- 本地已安装并登录 `codex` 和/或 `claude` CLI
- Telegram Bot Token

## 快速开始

### 1) 安装依赖

```bash
uv sync --group dev
```

### 2) 配置环境变量

`uv run start|stop|restart|status|logs` 会自动加载项目根目录 `.env`。你可以在 shell 中 `export`，也可以写到 `.env`。

`.env` 示例：

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

环境变量列表：

```bash
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # 可选，推荐

# Provider
export DEFAULT_PROVIDER=codex                         # codex 或 claude
export ALLOWED_CWD_ROOTS="/path/to/allowed-a,/path/to/allowed-b"  # 可选

# 流式参数
export TG_STREAM_ENABLED=1
export TG_STREAM_EDIT_INTERVAL_MS=700
export TG_STREAM_MIN_DELTA_CHARS=8
export TG_THINKING_STATUS_INTERVAL_MS=900
export TG_STREAM_RETRY_COOLDOWN_MS=15000
export TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS=2
export TG_STREAM_PREVIEW_FAILFAST=1

# 消息格式化 / 渲染
export TG_FORMATTING_ENABLED=1
export TG_FORMATTING_STYLE="strong"                  # light | medium | strong
export TG_FORMATTING_MODE="html"                     # html | plain
export TG_LINK_PREVIEW_POLICY="auto"                 # auto | off
export TG_FORMATTING_FAIL_OPEN=1

# 网络重试
export TG_HTTP_MAX_RETRIES=2
export TG_HTTP_RETRY_BASE_MS=300
export TG_HTTP_RETRY_MAX_MS=3000

# 运行时目录（可选）
# Linux 默认值：~/.local/state/tiya
# macOS 默认值：~/Library/Application Support/tiya
export TIYA_HOME="$HOME/.local/state/tiya"
# 开发态如需将运行时文件放回仓库，可显式设置：
# export TIYA_HOME="$(pwd)/.runtime"

# 显式锁路径覆盖（可选）
# export TG_INSTANCE_LOCK_PATH="/custom/path/bot.lock"

# 代理（可选，仅在 VPN/网络策略需要时配置）
export TG_PROXY_URL="http://127.0.0.1:7897"
# 或：
export HTTPS_PROXY="http://127.0.0.1:7897"
export HTTP_PROXY="http://127.0.0.1:7897"

# Codex
export DEFAULT_CWD="/path/to/your/project"
# 如果 `codex` 已在 PATH 中，可不显式设置。
# macOS 还会自动探测：
#   /Applications/Codex.app/Contents/Resources/codex
#   ~/Applications/Codex.app/Contents/Resources/codex
export CODEX_BIN="codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"
export CODEX_SANDBOX_MODE=""
export CODEX_APPROVAL_POLICY=""
export CODEX_DANGEROUS_BYPASS=0

# Claude
# 如果 `claude` 已在 PATH 中，可不显式设置。
# macOS 还会自动探测：
#   /opt/homebrew/bin/claude
#   /usr/local/bin/claude
#   ~/.local/bin/claude
export CLAUDE_BIN="claude"
export CLAUDE_SESSION_ROOT="$HOME/.claude/projects"
export CLAUDE_MODEL=""                                # 可选
export CLAUDE_PERMISSION_MODE="default"
```

### 3) 启动

```bash
uv run start
```

常用命令：

```bash
uv run stop
uv run status
uv run logs
uv run restart
```

## 支持命令

- `/help`
- `/provider [codex|claude]`
- `/sessions [N]`
- `/use <index|session_id>`
- `/history [index|session_id] [N]`
- `/new [cwd]`
- `/status`
- `/ask <text>`
- 直接发送文本即可对话

## 目录结构

- `tiya.py`：启动入口
- `src/runtime_paths.py`：按 token 分隔的运行时路径解析
- `src/cli.py`：服务管理命令（`start|stop|restart|status|logs`）
- `src/app.py`：应用装配与轮询启动
- `src/config.py`：环境变量解析
- `src/telegram/router.py`：命令与回调路由
- `src/telegram/streaming.py`：流式编排
- `src/telegram/client.py`：Telegram API 封装（含重试）
- `src/services/codex_runner.py`：异步 Codex 子进程执行
- `src/services/claude_runner.py`：异步 Claude 子进程执行
- `src/services/session_store.py`：按 provider 的会话与历史读取
- `src/services/state_store.py`：按 provider 的 JSON 状态持久化
- `tests/`：pytest 测试集

## 测试

```bash
uv run pytest
```

## 备注

- 当 `TG_STREAM_ENABLED` 未设置时，仍兼容旧变量 `TELEGRAM_ENABLE_DRAFT_STREAM`。
- `TG_FORMATTING_FINAL_ONLY` 与 `TG_FORMATTING_BACKEND` 已废弃，设置后会被忽略。
- 当前架构按设计仅支持长轮询模式。
- `run.sh` 已移除，仅保留 `uv run <command>`。
- 仅使用 `uv run start` 启动。不要再额外运行 `python -m tg_codex` 等同 token 轮询进程。
- 运行时文件会存放到 `TIYA_HOME/instances/<token_hash>/`。
- macOS 支持当前与 Linux 的 CLI 用法对齐；本版本不包含 `launchd` 集成。
- `CODEX_DANGEROUS_BYPASS` 仅应在受控用户、受控主机环境下使用。

## 简单 Prompt 很慢时的排查

如果 Telegram 中长时间停留在 `思考中...`，通常瓶颈不在 Telegram 发消息，而在模型子进程的网络链路。

- 如果开启了 VPN，检查可选代理变量是否可用（`TG_PROXY_URL` 或 `HTTPS_PROXY`）。
- 在同一 shell 里直接执行：
  - `codex exec --json --skip-git-repo-check "你是谁？"`
  - `claude -p --verbose --output-format stream-json "你是谁？"`
- `uv run start` 已做代理变量名标准化（大小写），避免代理变量传递异常导致长时间无首 token。

## “半句卡住”排查

- `tiya` 已对同 token 启用实例锁，并按 token 分隔 PID / 日志 / 状态文件。
- 可先执行：
  - `uv run stop`
  - `ps -ef | rg "python -m src|tiya.py"`
- 遇到 Telegram 限流时，流式预览会自动降级为 `typing + 最终消息`，最终完整答案仍会发送。
