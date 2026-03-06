from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from ..domain.models import AgentRunResult, PromptImage


class RunnerProtocol(Protocol):
    async def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        images: tuple[PromptImage, ...] = (),
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> AgentRunResult:
        ...
