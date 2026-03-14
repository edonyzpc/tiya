export type FieldKind = "text" | "textarea" | "select" | "number" | "directory";

export interface FieldOption {
  label: string;
  value: string;
}

export interface FieldDefinition {
  key: string;
  label: string;
  hint: string;
  kind: FieldKind;
  placeholder?: string;
  options?: FieldOption[];
}

export const basicFields: FieldDefinition[] = [
  {
    key: "ALLOWED_TELEGRAM_USER_IDS",
    label: "Allowed Telegram Users",
    hint: "Comma-separated operator IDs allowed to control the bot.",
    kind: "text",
    placeholder: "123456789,987654321"
  },
  {
    key: "DEFAULT_PROVIDER",
    label: "Default Provider",
    hint: "Which local runner new conversations should default to.",
    kind: "select",
    options: [
      { label: "Codex", value: "codex" },
      { label: "Claude", value: "claude" }
    ]
  },
  {
    key: "DEFAULT_CWD",
    label: "Default Working Directory",
    hint: "The fallback project directory when a Telegram prompt does not specify cwd.",
    kind: "directory",
    placeholder: "/path/to/project"
  },
  {
    key: "ALLOWED_CWD_ROOTS",
    label: "Allowed Working Roots",
    hint: "Optional allowlist for cwd roots, comma-separated.",
    kind: "textarea",
    placeholder: "/workspace/a,/workspace/b"
  },
  {
    key: "CODEX_BIN",
    label: "Codex Binary",
    hint: "Path or command name for the Codex CLI.",
    kind: "text",
    placeholder: "codex"
  },
  {
    key: "CLAUDE_BIN",
    label: "Claude Binary",
    hint: "Path or command name for the Claude CLI.",
    kind: "text",
    placeholder: "claude"
  },
  {
    key: "CODEX_SESSION_ROOT",
    label: "Codex Session Root",
    hint: "Filesystem root for Codex session archives.",
    kind: "directory",
    placeholder: "~/.codex/sessions"
  },
  {
    key: "CLAUDE_SESSION_ROOT",
    label: "Claude Session Root",
    hint: "Filesystem root for Claude session archives.",
    kind: "directory",
    placeholder: "~/.claude/projects"
  },
  {
    key: "TG_PROXY_URL",
    label: "Telegram Proxy",
    hint: "Primary proxy URL used by the bot worker when network policy requires it.",
    kind: "text",
    placeholder: "http://127.0.0.1:7897"
  }
];

export const advancedFields: FieldDefinition[] = [
  {
    key: "TIYA_DESKTOP_GPU_MODE",
    label: "Desktop GPU Mode",
    hint: "Controls Electron hardware acceleration. Relaunch desktop after changing this value.",
    kind: "select",
    options: [
      { label: "Disabled", value: "disabled" },
      { label: "Enabled", value: "enabled" }
    ]
  },
  {
    key: "CODEX_SANDBOX_MODE",
    label: "Codex Sandbox Mode",
    hint: "Optional Codex sandbox mode override.",
    kind: "text"
  },
  {
    key: "CODEX_APPROVAL_POLICY",
    label: "Codex Approval Policy",
    hint: "Optional Codex approval policy override.",
    kind: "text"
  },
  {
    key: "CODEX_DANGEROUS_BYPASS",
    label: "Codex Dangerous Bypass",
    hint: "0-2; higher values reduce operator safety checks.",
    kind: "number"
  },
  {
    key: "CLAUDE_MODEL",
    label: "Claude Model",
    hint: "Optional Claude model override.",
    kind: "text"
  },
  {
    key: "CLAUDE_PERMISSION_MODE",
    label: "Claude Permission Mode",
    hint: "Permission mode passed into Claude.",
    kind: "select",
    options: [
      { label: "Default", value: "default" },
      { label: "Accept Edits", value: "acceptEdits" },
      { label: "Bypass Permissions", value: "bypassPermissions" }
    ]
  },
  {
    key: "TG_STREAM_ENABLED",
    label: "Streaming Enabled",
    hint: "Turn preview streaming on or off.",
    kind: "select",
    options: [
      { label: "Enabled", value: "1" },
      { label: "Disabled", value: "0" }
    ]
  },
  {
    key: "TG_STREAM_EDIT_INTERVAL_MS",
    label: "Stream Edit Interval",
    hint: "Milliseconds between streamed edit attempts.",
    kind: "number"
  },
  {
    key: "TG_STREAM_MIN_DELTA_CHARS",
    label: "Min Stream Delta",
    hint: "Minimum streamed character delta before editing.",
    kind: "number"
  },
  {
    key: "TG_THINKING_STATUS_INTERVAL_MS",
    label: "Thinking Status Interval",
    hint: "Milliseconds between thinking status updates.",
    kind: "number"
  },
  {
    key: "TG_HTTP_MAX_RETRIES",
    label: "HTTP Max Retries",
    hint: "Maximum Telegram API retry attempts.",
    kind: "number"
  },
  {
    key: "TG_HTTP_RETRY_BASE_MS",
    label: "HTTP Retry Base",
    hint: "Base backoff in milliseconds.",
    kind: "number"
  },
  {
    key: "TG_HTTP_RETRY_MAX_MS",
    label: "HTTP Retry Max",
    hint: "Maximum backoff in milliseconds.",
    kind: "number"
  },
  {
    key: "TG_STREAM_RETRY_COOLDOWN_MS",
    label: "Stream Retry Cooldown",
    hint: "Cooldown before retrying failed stream previews.",
    kind: "number"
  },
  {
    key: "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS",
    label: "Max Preview Errors",
    hint: "How many preview errors to tolerate before downgrading.",
    kind: "number"
  },
  {
    key: "TG_STREAM_PREVIEW_FAILFAST",
    label: "Preview Failfast",
    hint: "Whether preview failures should abort early.",
    kind: "select",
    options: [
      { label: "Enabled", value: "1" },
      { label: "Disabled", value: "0" }
    ]
  },
  {
    key: "TG_FORMATTING_ENABLED",
    label: "Formatting Enabled",
    hint: "Enable Telegram formatting transforms on final replies.",
    kind: "select",
    options: [
      { label: "Enabled", value: "1" },
      { label: "Disabled", value: "0" }
    ]
  },
  {
    key: "TG_FORMATTING_STYLE",
    label: "Formatting Style",
    hint: "Formatting density for final rendered replies.",
    kind: "select",
    options: [
      { label: "Light", value: "light" },
      { label: "Medium", value: "medium" },
      { label: "Strong", value: "strong" }
    ]
  },
  {
    key: "TG_FORMATTING_MODE",
    label: "Formatting Mode",
    hint: "HTML or plain text rendering mode.",
    kind: "select",
    options: [
      { label: "HTML", value: "html" },
      { label: "Plain", value: "plain" }
    ]
  },
  {
    key: "TG_FORMATTING_BACKEND",
    label: "Formatting Backend",
    hint: "Formatting backend used by the renderer.",
    kind: "select",
    options: [
      { label: "Telegramify", value: "telegramify" },
      { label: "Builtin", value: "builtin" },
      { label: "Sulguk", value: "sulguk" }
    ]
  },
  {
    key: "TG_LINK_PREVIEW_POLICY",
    label: "Link Preview Policy",
    hint: "How the renderer should expose Telegram link previews.",
    kind: "select",
    options: [
      { label: "Auto", value: "auto" },
      { label: "Off", value: "off" }
    ]
  },
  {
    key: "TG_FORMATTING_FAIL_OPEN",
    label: "Formatting Fail Open",
    hint: "Fallback to plain text when formatting transforms fail.",
    kind: "select",
    options: [
      { label: "Enabled", value: "1" },
      { label: "Disabled", value: "0" }
    ]
  }
];

export const wizardSteps = [
  {
    title: "Telegram",
    eyebrow: "Step 01",
    description: "Store the bot token securely, then define who is allowed to operate this bot from Telegram.",
    fields: ["ALLOWED_TELEGRAM_USER_IDS"]
  },
  {
    title: "Provider",
    eyebrow: "Step 02",
    description: "Choose the default agent and confirm both local CLIs resolve correctly on this host.",
    fields: ["DEFAULT_PROVIDER", "CODEX_BIN", "CLAUDE_BIN"]
  },
  {
    title: "Directories",
    eyebrow: "Step 03",
    description: "Set the working directory policy and point tiya at both providers' session stores.",
    fields: ["DEFAULT_CWD", "ALLOWED_CWD_ROOTS", "CODEX_SESSION_ROOT", "CLAUDE_SESSION_ROOT"]
  },
  {
    title: "Network",
    eyebrow: "Step 04",
    description: "Apply proxy settings only when your local network path requires them.",
    fields: ["TG_PROXY_URL"]
  },
  {
    title: "Review",
    eyebrow: "Step 05",
    description: "Validate the snapshot, review warnings, and commit the first operator-ready configuration.",
    fields: []
  }
] as const;
