import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from ..domain.models import AgentRunResult, ApprovalDecision, ApprovalRequest, PromptImage, QuestionRequest


class InteractionHandlerProtocol(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        ...

    async def request_question(self, request: QuestionRequest) -> Optional[list[str]]:
        ...


class RunnerProtocol(Protocol):
    async def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        images: tuple[PromptImage, ...] = (),
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
        interaction_handler: Optional[InteractionHandlerProtocol] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AgentRunResult:
        ...
