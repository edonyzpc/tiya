import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from config import load_config
from domain.models import AgentProvider
from instance_lock import BotInstanceLock
from logging_utils import log
from services.claude_runner import ClaudeRunner
from services.codex_runner import CodexRunner
from services.runner_protocol import RunnerProtocol
from services.session_store import ClaudeSessionStore, CodexSessionStore
from services.state_store import StateStore
from telegram.client import TelegramClient
from telegram.rendering import TelegramMessageRenderer
from telegram.router import TgCodexService, build_router


async def run() -> None:
    config = load_config()
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
        log("[warn] CODEX_DANGEROUS_BYPASS=1, enabling sandbox_mode=danger-full-access and approval_policy=never")
    elif config.dangerous_bypass_level >= 2:
        log("[warn] CODEX_DANGEROUS_BYPASS=2, approvals and sandbox are fully bypassed")

    if config.stream_enabled:
        log(
            "[info] TG streaming enabled "
            f"(edit interval: {config.stream_edit_interval_ms}ms, "
            f"min delta: {config.stream_min_delta_chars}, "
            f"thinking interval: {config.thinking_status_interval_ms}ms, "
            f"retry cooldown: {config.tg_stream_retry_cooldown_ms}ms, "
            f"max preview errors: {config.tg_stream_max_consecutive_preview_errors}, "
            f"preview failfast: {str(config.tg_stream_preview_failfast).lower()})"
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
    renderer = TelegramMessageRenderer(
        enabled=config.tg_formatting_enabled,
        final_only=config.tg_formatting_final_only,
        style=config.tg_formatting_style,
        mode=config.tg_formatting_mode,
        link_preview_policy=config.tg_link_preview_policy,
        fail_open=config.tg_formatting_fail_open,
        backend=config.tg_formatting_backend,
    )

    session_stores = {
        "codex": CodexSessionStore(config.codex_session_root),
        "claude": ClaudeSessionStore(config.claude_session_root),
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
        f"final_only={str(config.tg_formatting_final_only).lower()}, "
        f"style={config.tg_formatting_style}, mode={config.tg_formatting_mode}, "
        f"link_preview={config.tg_link_preview_policy}, "
        f"fail_open={str(config.tg_formatting_fail_open).lower()}, "
        f"backend={config.tg_formatting_backend})"
    )

    service = TgCodexService(
        api=api,
        session_stores=session_stores,
        state=state,
        runners=runners,
        runner_bins=runner_bins,
        default_cwd=config.default_cwd,
        allowed_user_ids=config.allowed_user_ids,
        stream_enabled=config.stream_enabled,
        stream_edit_interval_ms=config.stream_edit_interval_ms,
        stream_min_delta_chars=config.stream_min_delta_chars,
        thinking_status_interval_ms=config.thinking_status_interval_ms,
        stream_retry_cooldown_ms=config.tg_stream_retry_cooldown_ms,
        stream_max_consecutive_preview_errors=config.tg_stream_max_consecutive_preview_errors,
        stream_preview_failfast=config.tg_stream_preview_failfast,
        renderer=renderer,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))

    try:
        try:
            await service.setup_bot_menu()
            log("bot command menu configured")
        except Exception as exc:
            log(f"bot command menu setup failed: {exc}")

        log("tiya service started")
        try:
            await dispatcher.start_polling(bot)
        finally:
            await bot.session.close()
    finally:
        instance_lock.release()
        log(f"[info] instance lock released (path={instance_lock.path})")


def main() -> None:
    asyncio.run(run())
