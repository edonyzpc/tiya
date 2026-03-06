from pathlib import Path

import pytest

from src.services.interaction_coordinator import InteractionCoordinator
from src.services.state_store import StateStore


@pytest.mark.asyncio
async def test_open_interaction_requires_active_run(tmp_path: Path):
    state = StateStore(tmp_path / "state.json", flush_delay_sec=0.01)
    coordinator = InteractionCoordinator(state)

    with pytest.raises(RuntimeError, match="active run"):
        await coordinator.open_interaction(
            user_id=1,
            provider="codex",
            kind="approval",
            title="need approval",
            body="body",
            options=(),
            reply_mode="buttons",
            chat_id=101,
            message_id=None,
            timeout_sec=60,
        )

    await state.close()
