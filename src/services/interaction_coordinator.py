import asyncio
import secrets
import time
from dataclasses import replace
from dataclasses import dataclass, field
from typing import Any, Optional

from ..domain.models import (
    ActiveRunState,
    AgentProvider,
    InteractionOption,
    PendingInteraction,
)
from .state_store import StateStore


@dataclass
class ActiveRun:
    run_id: str
    user_id: int
    provider: AgentProvider
    chat_id: int
    chat_type: str
    started_at: int
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_interaction_id: Optional[str] = None
    task: Optional[asyncio.Task[Any]] = None

    def to_state(self) -> ActiveRunState:
        return ActiveRunState(
            run_id=self.run_id,
            chat_id=self.chat_id,
            chat_type=self.chat_type,
            started_at=self.started_at,
        )


@dataclass
class PendingWaiter:
    user_id: int
    provider: AgentProvider
    interaction: PendingInteraction
    # The future resolves to a string by convention:
    # - selected option id
    # - free-form text reply
    # - the sentinel value "cancel"
    future: asyncio.Future[str]


class InteractionCoordinator:
    def __init__(self, state: StateStore):
        self.state = state
        self._lock = asyncio.Lock()
        self._active_runs: dict[tuple[int, AgentProvider], ActiveRun] = {}
        self._pending_by_id: dict[str, PendingWaiter] = {}

    @staticmethod
    def _new_token(length_bytes: int = 4) -> str:
        return secrets.token_hex(length_bytes)

    async def start_run(
        self,
        user_id: int,
        provider: AgentProvider,
        chat_id: int,
        chat_type: str,
    ) -> Optional[ActiveRun]:
        async with self._lock:
            key = (user_id, provider)
            if key in self._active_runs:
                return None
            run = ActiveRun(
                run_id=self._new_token(5),
                user_id=user_id,
                provider=provider,
                chat_id=chat_id,
                chat_type=chat_type,
                started_at=int(time.time()),
            )
            self._active_runs[key] = run
        await self.state.set_active_run(user_id, run.to_state(), provider=provider)
        return run

    async def set_task(
        self,
        user_id: int,
        provider: AgentProvider,
        task: asyncio.Task[Any],
    ) -> None:
        async with self._lock:
            run = self._active_runs.get((user_id, provider))
            if run is not None:
                run.task = task

    async def get_active_run(
        self,
        user_id: int,
        provider: AgentProvider,
    ) -> Optional[ActiveRun]:
        async with self._lock:
            return self._active_runs.get((user_id, provider))

    async def get_pending_interaction(
        self,
        user_id: int,
        provider: AgentProvider,
    ) -> Optional[PendingInteraction]:
        async with self._lock:
            run = self._active_runs.get((user_id, provider))
            if run is None or run.pending_interaction_id is None:
                return None
            waiter = self._pending_by_id.get(run.pending_interaction_id)
            if waiter is None:
                return None
            return waiter.interaction

    async def open_interaction(
        self,
        *,
        user_id: int,
        provider: AgentProvider,
        kind: str,
        title: str,
        body: str,
        options: tuple[InteractionOption, ...],
        reply_mode: str,
        chat_id: int,
        message_id: Optional[int],
        timeout_sec: int,
    ) -> PendingWaiter:
        async with self._lock:
            run = self._active_runs.get((user_id, provider))
            if run is None:
                raise RuntimeError("cannot open interaction without an active run")
            interaction = PendingInteraction(
                interaction_id=self._new_token(4),
                run_id=run.run_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                body=body,
                options=options,
                reply_mode=reply_mode,  # type: ignore[arg-type]
                created_at=int(time.time()),
                expires_at=int(time.time()) + max(1, timeout_sec),
                chat_id=chat_id,
                message_id=message_id,
            )
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            waiter = PendingWaiter(
                user_id=user_id,
                provider=provider,
                interaction=interaction,
                future=future,
            )
            run.pending_interaction_id = interaction.interaction_id
            self._pending_by_id[interaction.interaction_id] = waiter
        # Persist after releasing the in-memory lock so Telegram/network I/O never runs while
        # coordinator state is locked. The short-lived mismatch on crash is acceptable because
        # pending interactions are explicitly treated as transient and cleared on boot.
        await self.state.set_pending_interaction(user_id, interaction, provider=provider)
        return waiter

    async def wait_for_interaction(
        self,
        waiter: PendingWaiter,
        timeout_sec: int,
    ) -> Any:
        try:
            return await asyncio.wait_for(waiter.future, timeout=max(1, timeout_sec))
        finally:
            await self._clear_interaction(waiter.user_id, waiter.provider, waiter.interaction.interaction_id)

    async def bind_message_id(
        self,
        *,
        user_id: int,
        provider: AgentProvider,
        interaction_id: str,
        message_id: int,
    ) -> Optional[PendingInteraction]:
        if message_id <= 0:
            return None
        async with self._lock:
            waiter = self._pending_by_id.get(interaction_id)
            if waiter is None:
                return None
            if waiter.user_id != user_id or waiter.provider != provider:
                return None
            if waiter.interaction.message_id == message_id:
                return waiter.interaction
            waiter.interaction = replace(waiter.interaction, message_id=message_id)
            interaction = waiter.interaction
        await self.state.set_pending_interaction(user_id, interaction, provider=provider)
        return interaction

    async def resolve_option(
        self,
        *,
        user_id: int,
        provider: AgentProvider,
        chat_id: int,
        interaction_id: str,
        option_id: str,
    ) -> bool:
        async with self._lock:
            waiter = self._pending_by_id.get(interaction_id)
            if waiter is None:
                return False
            if waiter.user_id != user_id or waiter.provider != provider:
                return False
            if waiter.interaction.chat_id != chat_id:
                return False
            if waiter.future.done():
                return False
            if option_id not in {option.id for option in waiter.interaction.options}:
                return False
            waiter.future.set_result(option_id)
            return True

    async def resolve_text_reply(
        self,
        *,
        user_id: int,
        provider: AgentProvider,
        chat_id: int,
        text: str,
    ) -> bool:
        async with self._lock:
            run = self._active_runs.get((user_id, provider))
            if run is None or run.pending_interaction_id is None:
                return False
            waiter = self._pending_by_id.get(run.pending_interaction_id)
            if waiter is None or waiter.future.done():
                return False
            if waiter.interaction.chat_id != chat_id:
                return False
            if waiter.interaction.reply_mode != "text":
                return False
            waiter.future.set_result(text)
            return True

    async def cancel_run(
        self,
        user_id: int,
        provider: AgentProvider,
    ) -> bool:
        async with self._lock:
            run = self._active_runs.get((user_id, provider))
            if run is None:
                return False
            run.cancel_event.set()
            if run.pending_interaction_id is not None:
                waiter = self._pending_by_id.get(run.pending_interaction_id)
                if waiter is not None and not waiter.future.done():
                    waiter.future.set_result("cancel")
            return True

    async def finish_run(
        self,
        user_id: int,
        provider: AgentProvider,
        run_id: str,
    ) -> None:
        async with self._lock:
            key = (user_id, provider)
            run = self._active_runs.get(key)
            if run is None or run.run_id != run_id:
                return
            pending_id = run.pending_interaction_id
            self._active_runs.pop(key, None)
            if pending_id is not None:
                waiter = self._pending_by_id.pop(pending_id, None)
                if waiter is not None and not waiter.future.done():
                    waiter.future.cancel()
        await self.state.clear_pending_interaction(user_id, provider=provider)
        await self.state.clear_active_run(user_id, provider=provider)

    async def discard_interaction(
        self,
        user_id: int,
        provider: AgentProvider,
        interaction_id: str,
    ) -> None:
        await self._clear_interaction(user_id, provider, interaction_id)

    async def _clear_interaction(
        self,
        user_id: int,
        provider: AgentProvider,
        interaction_id: str,
    ) -> None:
        async with self._lock:
            self._pending_by_id.pop(interaction_id, None)
            run = self._active_runs.get((user_id, provider))
            if run is not None and run.pending_interaction_id == interaction_id:
                run.pending_interaction_id = None
        await self.state.clear_pending_interaction(user_id, provider=provider)
