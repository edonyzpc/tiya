import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from config import load_config
from domain.models import AgentProvider
from logging_utils import log
from services.claude_runner import ClaudeRunner
from services.codex_runner import CodexRunner
from services.runner_protocol import RunnerProtocol
from services.session_store import ClaudeSessionStore, CodexSessionStore
from services.state_store import StateStore
from telegram.client import TelegramClient
from telegram.router import TgCodexService, build_router


async def run() -> None:
    config = load_config()

    if config.dangerous_bypass_level == 1:
        log("[warn] CODEX_DANGEROUS_BYPASS=1, enabling sandbox_mode=danger-full-access and approval_policy=never")
    elif config.dangerous_bypass_level >= 2:
        log("[warn] CODEX_DANGEROUS_BYPASS=2, approvals and sandbox are fully bypassed")

    if config.stream_enabled:
        log(
            "[info] TG streaming enabled "
            f"(edit interval: {config.stream_edit_interval_ms}ms, "
            f"min delta: {config.stream_min_delta_chars}, "
            f"thinking interval: {config.thinking_status_interval_ms}ms)"
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
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service))

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


def main() -> None:
    asyncio.run(run())
