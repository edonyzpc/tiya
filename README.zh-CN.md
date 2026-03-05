# tg-codex

语言: [English](README.md) | 简体中文

`tg-codex` 让你可以通过 Telegram 来运行和继续本地的 `codex` 会话。

## 功能特性

- 列出本地会话历史（带标题）
- 切换到已有会话并继续提问
- 创建新会话并控制工作目录
- 查看会话中的最近消息 (`/history`)
- 运行 Telegram Bot 远程控制 Codex CLI

## 环境要求

- Python 3.9+
- 本地已安装并登录 [Codex](https://codex.bot/)
- Telegram: `TELEGRAM_BOT_TOKEN`

## 快速开始

### 1) 配置环境变量

```bash
# Telegram（必需）
export TELEGRAM_BOT_TOKEN="你的Bot Token"
export ALLOWED_TELEGRAM_USER_IDS="123456789"         # 可选，推荐配置

# 共享配置（可选）
export DEFAULT_CWD="/path/to/your/project"
export CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"
export CODEX_SANDBOX_MODE=""                         # 可选，仅在 CODEX_DANGEROUS_BYPASS=1 时使用
export CODEX_APPROVAL_POLICY=""                      # 可选，仅在 CODEX_DANGEROUS_BYPASS=1 时使用
export CODEX_DANGEROUS_BYPASS=0                      # 0/1/2（见权限部分）
```

### 2) 启动服务

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

## 配置指南

详细分步配置说明请查看 [SETUP_TELEGRAM.md](SETUP_TELEGRAM.md)。

## 权限开关与风险

权限行为由 `CODEX_DANGEROUS_BYPASS` 控制：

- `0`（默认）：无额外权限标志（最低权限）
- `1`：启用权限标志
  - `CODEX_SANDBOX_MODE` 默认为 `danger-full-access`（可覆盖）
  - `CODEX_APPROVAL_POLICY` 默认为 `never`（可覆盖）
- `2`：追加 `--dangerously-bypass-approvals-and-sandbox`

注意：
- `CODEX_SANDBOX_MODE` / `CODEX_APPROVAL_POLICY` 仅在 `CODEX_DANGEROUS_BYPASS=1` 时生效
- `CODEX_DANGEROUS_BYPASS=2` 完全绕过安全限制

风险提示：

- 可能会执行任意命令并修改/删除本地文件
- 可能会读取并外泄敏感数据（密钥、配置、源代码）
- 仅在受控环境中启用，之后请切回 `0`

## 命令列表

- `/help`
- `/sessions [N]`：列出最近 N 个会话（标题 + 编号）
- `/use <编号|session_id>`：切换到指定会话
- `/history [编号|session_id] [N]`：显示最近 N 条消息（默认 10，最大 50）
- `/new [cwd]`：进入新会话模式；下一条普通消息将创建新会话
- `/status`：显示当前活动会话
- `/ask <文本>`：在当前会话中提问
- 直接发送文本：继续当前会话，或在新会话模式下创建会话

使用技巧：

- 执行 `/sessions` 后，直接发送编号（例如 `1`）即可切换

## 附加脚本

- `tg_codex_bot.py`：Telegram 服务入口
- `run.sh`：进程管理脚本

## 已知限制

- 新会话主要在终端/CLI 会话历史中可见
- Codex Desktop 可能需要重启才能看到新继续的会话
- 回复在每个请求完成后返回（暂不支持流式推送）