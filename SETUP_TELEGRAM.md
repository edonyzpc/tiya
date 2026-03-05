# Telegram Bot 配置指南

本指南将一步一步教你如何配置 Telegram Bot 来远程控制本地 Codex CLI 会话。

---

## 环境要求

- Python 3.9+
- 本地已安装并登录 [Codex](https://codex.bot/)
- Telegram 账号

---

## 第一步：创建 Telegram Bot

1. 打开 Telegram，搜索 `@BotFather` 并开始对话

2. 发送命令 `/newbot` 创建新机器人

3. 按照提示输入机器人名称（例如：`My Codex Bot`）和用户名（必须以 `bot` 结尾，例如：`my_codex_bot`）

4. 创建成功后，BotFather 会给你一个 **Bot Token**，格式类似：
   ```
   123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```
   > ⚠️ 请妥善保存这个 Token，不要泄露给他人

---

## 第二步：获取你的 Telegram 用户 ID

1. 搜索 `@userinfobot` 并开始对话

2. 发送任意消息，机器人会返回你的 **User ID**（一串数字）

3. 记录下来，后面配置需要用到

---

## 第三步：配置环境变量

在终端中设置以下环境变量：

```bash
# 必需：第一步获取的 Bot Token
export TELEGRAM_BOT_TOKEN="你的BotToken"

# 可选但推荐：你的用户ID，限制只有你能使用机器人
export ALLOWED_TELEGRAM_USER_IDS="你的UserID"

# 可选：Codex 二进制文件路径（根据你的系统调整）
export CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"

# Linux 用户可能需要：
# export CODEX_BIN="/usr/local/bin/codex"

# 或 macOS 用户：
# export CODEX_BIN="$HOME/.codex/bin/codex"

# 可选：会话存储路径
export CODEX_SESSION_ROOT="$HOME/.codex/sessions"

# 可选：工作目录
export DEFAULT_CWD="$HOME"

# 可选：权限级别（0=默认，1=沙箱模式，2=完全绕过）
export CODEX_DANGEROUS_BYPASS=0
```

**永久保存配置（推荐）：**

将以上内容添加到 `~/.bashrc` 或 `~/.zshrc`：

```bash
echo 'export TELEGRAM_BOT_TOKEN="你的BotToken"' >> ~/.bashrc
echo 'export ALLOWED_TELEGRAM_USER_IDS="你的UserID"' >> ~/.bashrc
source ~/.bashrc
```

---

## 第四步：启动 Bot

进入项目目录并启动：

```bash
cd /path/to/codex-tg
./run.sh start
```

查看运行状态：

```bash
./run.sh status
```

查看日志：

```bash
./run.sh logs
```

停止 Bot：

```bash
./run.sh stop
```

---

## 第五步：开始使用

1. 在 Telegram 中打开你创建的机器人

2. 点击 **Start** 或发送 `/start`

3. 发送 `/help` 查看所有可用命令

### 常用命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看帮助 |
| `/sessions` | 列出最近会话 |
| `/use <编号>` | 切换到指定会话 |
| `/history` | 查看当前会话历史 |
| `/new` | 创建新会话 |
| `/status` | 查看当前会话状态 |
| `/ask <问题>` | 直接提问 |

### 使用示例

```
# 查看最近会话
/sessions

# 切换到第 1 个会话
/use 1

# 在当前会话中提问
帮我解释一下这段代码的作用

# 或使用 /ask
/ask 帮我写一个快速排序函数
```

---

## 故障排除

### Bot 没有响应

1. 检查 Bot 是否正在运行：
   ```bash
   ./run.sh status
   ```

2. 查看日志：
   ```bash
   ./run.sh logs
   ```

3. 确认 `TELEGRAM_BOT_TOKEN` 正确配置

### 无法执行命令

1. 确认你的 User ID 在 `ALLOWED_TELEGRAM_USER_IDS` 中
2. 确认 Codex 已正确安装并登录

### Codex 命令执行失败

1. 确认 Codex 二进制路径正确：
   ```bash
   which codex
   # 或者
   echo $CODEX_BIN
   ```

2. 手动测试 Codex：
   ```bash
   $CODEX_BIN --version
   ```

---

## 安全建议

- **始终设置** `ALLOWED_TELEGRAM_USER_IDS`，防止他人使用你的机器人
- 生产环境建议保持 `CODEX_DANGEROUS_BYPASS=0`
- 定期检查Bot日志，确认没有异常访问