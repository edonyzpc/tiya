from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from ..domain.models import AgentRunResult


class RunnerProtocol(Protocol):
    async def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> AgentRunResult:
        ...
