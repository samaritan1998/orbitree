from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

CodexEventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
CodexRequestHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class CodexUnavailable(RuntimeError):
    pass


class CodexAdapter(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def health(self) -> dict[str, Any]: ...

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]: ...

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]: ...

    async def fork_thread(
        self, *, thread_id: str, cwd: str, instructions: str
    ) -> dict[str, Any]: ...

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]: ...

    async def read_goal_thread(self, *, thread_id: str) -> dict[str, Any]: ...

    async def read_goal_turn(self, *, thread_id: str) -> dict[str, Any]: ...

    async def prepare_goal_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]: ...

    async def set_goal(
        self,
        *,
        thread_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        update_token_budget: bool = False,
    ) -> dict[str, Any]: ...

    async def clear_goal(self, *, thread_id: str) -> dict[str, Any]: ...

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    async def send_message(
        self,
        *,
        thread_id: str,
        content: str,
        cwd: str,
        metadata: dict[str, str],
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def respond(self, request_id: str, result: dict[str, Any]) -> None: ...
