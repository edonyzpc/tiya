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

`run.sh` 启动时会自动加载项目根目录 `.env`。你可以在 shell 中 `export`，也可以写到 `.env`。

`.env` 示例：

```bash
TELEGRAM_BOT_TOKEN="your bot token"
ALLOWED_TELEGRAM_USER_IDS="123456789"
DEFAULT_CWD="/path/to/your/project"
DEFAULT_PROVIDER="codex"
CODEX_BIN="codex"
CLAUDE_BIN="claude"
TG_STREAM_ENABLED=1
```

环境变量列表：

```bash
export TELEGRAM_BOT_TOKEN="your bot token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # 可选，推荐

# Provider
export DEFAULT_PROVIDER=codex                         # codex 或 claude

# 流式参数
export TG_STREAM_ENABLED=1
export TG_STREAM_EDIT_INTERVAL_MS=700
export TG_STREAM_MIN_DELTA_CHARS=8
export TG_THINKING_STATUS_INTERVAL_MS=900

# 网络重试
export TG_HTTP_MAX_RETRIES=2
export TG_HTTP_RETRY_BASE_MS=300
export TG_HTTP_RETRY_MAX_MS=3000
export TG_PROXY_URL="http://127.0.0.1:7897"         # 可选；未设置时回退到 HTTPS_PROXY/http_proxy

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
export CLAUDE_MODEL=""                                # 可选
export CLAUDE_PERMISSION_MODE="default"
```

### 3) 启动

```bash
./run.sh start
```

常用命令：

```bash
./run.sh stop
./run.sh status
./run.sh logs
./run.sh restart
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

- `tg_codex_bot.py`：兼容入口
- `src/tg_codex/app.py`：应用装配与轮询启动
- `src/tg_codex/config.py`：环境变量解析
- `src/tg_codex/telegram/router.py`：命令与回调路由
- `src/tg_codex/telegram/streaming.py`：流式编排
- `src/tg_codex/telegram/client.py`：Telegram API 封装（含重试）
- `src/tg_codex/services/codex_runner.py`：异步 Codex 子进程执行
- `src/tg_codex/services/claude_runner.py`：异步 Claude 子进程执行
- `src/tg_codex/services/session_store.py`：按 provider 的会话与历史读取
- `src/tg_codex/services/state_store.py`：按 provider 的 JSON 状态持久化
- `tests/`：pytest 测试集

## 测试

```bash
uv run pytest
```

## 备注

- 当 `TG_STREAM_ENABLED` 未设置时，仍兼容旧变量 `TELEGRAM_ENABLE_DRAFT_STREAM`。
- 当前架构按设计仅支持长轮询模式。

## 简单 Prompt 很慢时的排查

如果 Telegram 中长时间停留在 `思考中...`，通常瓶颈不在 Telegram 发消息，而在模型子进程的网络链路。

- 检查代理变量是否可用（`TG_PROXY_URL` 或 `HTTPS_PROXY`）。
- 在同一 shell 里直接执行：
  - `codex exec --json --skip-git-repo-check "你是谁？"`
  - `claude -p --verbose --output-format stream-json "你是谁？"`
- `run.sh` 已做代理变量名标准化（大小写），避免代理变量传递异常导致长时间无首 token。
