import os
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from .config import load_config
from .domain.models import AgentProvider, StreamConfig
from .instance_lock import BotInstanceLock
from .logging_utils import configure_logging, get_logger, log
from .runtime_paths import RuntimePaths
from .services.claude_runner import ClaudeRunner
from .services.codex_runner import CodexRunner
from .services.runner_protocol import RunnerProtocol
from .services.session_store import AsyncSessionStore, ClaudeSessionStore, CodexSessionStore
from .services.state_store import StateStore
from .telegram.client import TelegramClient
from .telegram.rendering import TelegramMessageRenderer
from .telegram.router import TgCodexService, build_router


async def run() -> None:
    config = load_config()
    configure_logging(RuntimePaths.for_token(config.telegram_token).log_file)
    instance_lock = BotInstanceLock(config.tg_instance_lock_path, config.telegram_token)
    acquired, lock_owner = instance_lock.acquire()
    if not acquired:
        owner_pid = lock_owner.get("pid")
        owner_started = lock_owner.get("started_at")
        owner_cmdline = lock_owner.get("cmdline", "")
        log(
            "[error] instance lock rejected "
            f"(path={instance_lock.path}, owner_pid={owner_pid}, owner_started_at={owner_started}, "
            f"owner_cmdline={owner_cmdline[:220]!r})"
        )
        return

    log(
        "[info] instance lock acquired "
        f"(path={instance_lock.path}, token_hash={instance_lock.token_digest}, pid={lock_owner.get('pid')})"
    )

    if config.dangerous_bypass_level == 1:
        log("[warn] HIGH RISK: CODEX_DANGEROUS_BYPASS=1 enables danger-full-access + approval_policy=never")
    elif config.dangerous_bypass_level >= 2:
        log("[warn] HIGH RISK: CODEX_DANGEROUS_BYPASS=2 fully bypasses approvals and sandbox")

    stream_config = StreamConfig(
        enabled=config.stream_enabled,
        edit_interval_ms=config.stream_edit_interval_ms,
        min_delta_chars=config.stream_min_delta_chars,
        thinking_status_interval_ms=config.thinking_status_interval_ms,
        retry_cooldown_ms=config.tg_stream_retry_cooldown_ms,
        max_consecutive_preview_errors=config.tg_stream_max_consecutive_preview_errors,
        preview_failfast=config.tg_stream_preview_failfast,
    )

    if stream_config.enabled:
        log(
            "[info] TG streaming enabled "
            f"(edit interval: {stream_config.edit_interval_ms}ms, "
            f"min delta: {stream_config.min_delta_chars}, "
            f"thinking interval: {stream_config.thinking_status_interval_ms}ms, "
            f"retry cooldown: {stream_config.retry_cooldown_ms}ms, "
            f"max preview errors: {stream_config.max_consecutive_preview_errors}, "
            f"preview failfast: {str(stream_config.preview_failfast).lower()})"
        )
    else:
        log("[info] TG streaming disabled")

    log(
        "[info] TG http retry "
        f"(max_retries={config.tg_http_max_retries}, "
        f"base_ms={config.tg_http_retry_base_ms}, "
        f"max_ms={config.tg_http_retry_max_ms})"
    )

    bot_session = AiohttpSession(proxy=config.telegram_proxy) if config.telegram_proxy else None
    if config.telegram_proxy:
        log("[info] TG proxy enabled")
    bot = Bot(token=config.telegram_token, session=bot_session) if bot_session else Bot(token=config.telegram_token)
    api = TelegramClient(
        bot=bot,
        request_max_retries=config.tg_http_max_retries,
        request_retry_base_ms=config.tg_http_retry_base_ms,
        request_retry_max_ms=config.tg_http_retry_max_ms,
    )
    formatting_backend = config.tg_formatting_backend
    if os.getenv("TG_FORMATTING_FINAL_ONLY") is not None:
        log("[warn] TG_FORMATTING_FINAL_ONLY is deprecated and ignored; formatting now follows TG_FORMATTING_ENABLED only")
    if formatting_backend == "sulguk":
        log("[warn] TG_FORMATTING_BACKEND=sulguk is not implemented; fallback to builtin")
        formatting_backend = "builtin"
    renderer = TelegramMessageRenderer(
        enabled=config.tg_formatting_enabled,
        final_only=False,
        style=config.tg_formatting_style,
        mode=config.tg_formatting_mode,
        link_preview_policy=config.tg_link_preview_policy,
        fail_open=config.tg_formatting_fail_open,
        backend=formatting_backend,
    )

    session_stores = {
        "codex": AsyncSessionStore(CodexSessionStore(config.codex_session_root)),
        "claude": AsyncSessionStore(ClaudeSessionStore(config.claude_session_root)),
    }
    state = StateStore(config.state_path, default_provider=config.default_provider)
    codex = CodexRunner(
        codex_bin=config.codex_bin,
        sandbox_mode=config.codex_sandbox_mode,
        approval_policy=config.codex_approval_policy,
        dangerous_bypass_level=config.dangerous_bypass_level,
    )
    claude = ClaudeRunner(
        claude_bin=config.claude_bin,
        model=config.claude_model,
        permission_mode=config.claude_permission_mode,
    )
    runners: dict[AgentProvider, RunnerProtocol] = {
        "codex": codex,
        "claude": claude,
    }
    runner_bins = {
        "codex": config.codex_bin,
        "claude": config.claude_bin,
    }

    log(
        "[info] provider defaults "
        f"(active={config.default_provider}, codex_bin={config.codex_bin}, claude_bin={config.claude_bin})"
    )
    log(
        "[info] TG formatting "
        f"(enabled={str(config.tg_formatting_enabled).lower()}, "
        f"style={config.tg_formatting_style}, mode={config.tg_formatting_mode}, "
        f"link_preview={config.tg_link_preview_policy}, "
        f"fail_open={str(config.tg_formatting_fail_open).lower()}, "
        f"backend={formatting_backend})"
    )
    if config.allowed_cwd_roots:
        roots = ", ".join(str(path) for path in config.allowed_cwd_roots)
        log(f"[info] cwd roots restricted ({roots})")

    service = TgCodexService(
        api=api,
        session_stores=session_stores,
        state=state,
        runners=runners,
        runner_bins=runner_bins,
        default_cwd=config.default_cwd,
        attachments_root=RuntimePaths.for_token(config.telegram_token).attachments_dir,
        allowed_user_ids=config.allowed_user_ids,
        allowed_cwd_roots=config.allowed_cwd_roots,
        stream_config=stream_config,
        renderer=renderer,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))

    try:
        me = await bot.get_me()
        log(f"[info] bot identity confirmed (id={me.id}, username=@{me.username or '-'})")
        try:
            await service.setup_bot_menu()
            log("[info] bot command menu configured")
        except Exception as exc:
            log(f"[warn] bot command menu setup failed: {exc}")

        log("[info] tiya service ready")
        await dispatcher.start_polling(bot)
    finally:
        await service.shutdown()
        await bot.session.close()
        instance_lock.release()
        log(f"[info] instance lock released (path={instance_lock.path})")


def main() -> None:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        configure_logging(RuntimePaths.for_token(token).log_file)
    else:
        configure_logging()
    try:
        asyncio.run(run())
    except Exception:
        get_logger().exception("fatal service error")
        raise
