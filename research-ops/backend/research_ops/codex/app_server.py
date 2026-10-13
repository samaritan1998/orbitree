from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from research_ops import __version__
from research_ops.settings import Settings

from .base import CodexEventHandler, CodexRequestHandler, CodexUnavailable
from .identity import CodexLaunch

logger = logging.getLogger(__name__)


class AppServerCodexAdapter:
    """Version-isolated JSON-RPC client for the experimental Codex App Server."""

    def __init__(
        self,
        settings: Settings,
        *,
        event_handler: CodexEventHandler,
        request_handler: CodexRequestHandler,
        launch: CodexLaunch | None = None,
    ) -> None:
        self.settings = settings
        self.event_handler = event_handler
        self.request_handler = request_handler
        self.launch = launch
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._stderr_tail: list[str] = []
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    async def start(self) -> None:
        if not self.settings.codex_enabled:
            raise CodexUnavailable("Codex integration is disabled")
        async with self._start_lock:
            if self.process and self.process.returncode is None:
                return
            self._stopping = False
            command = [self.settings.codex_command, "app-server", "--stdio"]
            if self.launch:
                command[1:1] = self.launch.args()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    env=self.launch.environment() if self.launch else None,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.settings.codex_event_line_limit_bytes,
                )
            except FileNotFoundError as exc:
                raise CodexUnavailable(f"Codex command not found: {command[0]}") from exc
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "research-ops",
                        "title": "Research Ops",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._notify("initialized", {})

    async def stop(self) -> None:
        self._stopping = True
        handlers = tuple(self._handler_tasks)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        self._handler_tasks.clear()
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)
            if process.returncode is None:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.process = None

    async def health(self) -> dict[str, Any]:
        try:
            await self.start()
            result = await self._request("thread/list", {"limit": 1})
            return {
                "available": True,
                "protocol": "app-server-stdio",
                "threads_visible": len(result.get("data", [])),
                "stderr_tail": self._stderr_tail[-3:],
            }
        except Exception as exc:
            return {
                "available": False,
                "protocol": "app-server-stdio",
                "error": str(exc),
                "stderr_tail": self._stderr_tail[-3:],
            }

    async def list_models(self, *, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": 100, "includeHidden": False}
        if cursor:
            params["cursor"] = cursor
        return await self._request("model/list", params)

    def _permission_overrides(self) -> dict[str, str]:
        return {
            key: value for key, value in (
                ("approvalPolicy", self.settings.codex_approval_policy),
                ("sandbox", self.settings.codex_sandbox),
            ) if value
        }

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(Path(cwd).resolve()),
            **self._permission_overrides(),
            "ephemeral": False,
            "serviceName": "research-ops",
        }
        if self.settings.codex_model:
            params["model"] = self.settings.codex_model
        if self.launch:
            params["modelProvider"] = self.launch.execution["provider"]
        return await self._request("thread/start", params)

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        return await self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(Path(cwd).resolve()),
                **self._permission_overrides(),
                **(
                    {
                        "model": self.launch.execution["model"],
                        "modelProvider": self.launch.execution["provider"],
                    }
                    if self.launch
                    else {}
                ),
            },
        )

    async def fork_thread(self, *, thread_id: str, cwd: str, instructions: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(Path(cwd).resolve()),
            **self._permission_overrides(),
            "ephemeral": False,
            # Keep the native history, but do not hydrate it into one RPC line.
            "excludeTurns": True,
        }
        if self.settings.codex_model:
            params["model"] = self.settings.codex_model
        if self.launch:
            params["modelProvider"] = self.launch.execution["provider"]
        return await self._request(
            "thread/fork",
            params,
            timeout=self.settings.codex_fork_timeout_seconds,
        )

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]:
        return await self._request("thread/goal/get", {"threadId": thread_id})

    async def read_goal_thread(self, *, thread_id: str) -> dict[str, Any]:
        return await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})

    async def read_goal_turn(self, *, thread_id: str) -> dict[str, Any]:
        response = await self._request(
            "thread/turns/list",
            {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "full"},
        )
        turns = response.get("data") or []
        if not turns:
            return {}
        turn = turns[0]
        # Summary views omit tools. Read one full turn, but retain no output or reasoning.
        return {
            "id": turn.get("id"),
            "status": turn.get("status"),
            "itemsView": turn.get("itemsView"),
            "items": [
                {key: item.get(key) for key in ("id", "type", "status")}
                for item in turn.get("items", [])
            ],
        }

    async def prepare_goal_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        # Resume also restores event subscriptions. A still-loaded thread can
        # be unsubscribed after pause, so its runtime status is not sufficient.
        return await self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(Path(cwd).resolve()),
                "excludeTurns": True,
                **self._permission_overrides(),
                **(
                    {
                        "model": self.launch.execution["model"],
                        "modelProvider": self.launch.execution["provider"],
                        "config": {
                            "model_reasoning_effort": self.launch.execution.get(
                                "reasoning_effort", self.settings.codex_reasoning_effort
                            )
                        },
                    }
                    if self.launch
                    else {}
                ),
            },
        )

    async def set_goal(
        self,
        *,
        thread_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        update_token_budget: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        if update_token_budget:
            params["tokenBudget"] = token_budget
        return await self._request("thread/goal/set", params)

    async def clear_goal(self, *, thread_id: str) -> dict[str, Any]:
        return await self._request("thread/goal/clear", {"threadId": thread_id})

    async def unsubscribe_thread(self, *, thread_id: str) -> dict[str, Any]:
        """Detach events; the thread writer remains until the inactivity grace period ends."""

        return await self._request("thread/unsubscribe", {"threadId": thread_id})

    async def owns_thread(self, *, thread_id: str) -> bool:
        if self.process is None or self.process.returncode is not None:
            return False
        params: dict[str, Any] = {}
        while True:
            response = await self._request("thread/loaded/list", params)
            if thread_id in response.get("data", []):
                return True
            cursor = response.get("nextCursor")
            if not cursor:
                return False
            params = {"cursor": cursor}

    async def steer_message(
        self, *, thread_id: str, turn_id: str, content: str
    ) -> dict[str, Any]:
        return await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": content, "text_elements": []}],
            },
        )

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def send_message(
        self,
        *,
        thread_id: str,
        content: str,
        cwd: str,
        metadata: dict[str, str],
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del metadata
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(Path(cwd).resolve()),
            "input": [{"type": "text", "text": content, "text_elements": []}],
        }
        return await self._request("turn/start", params)

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": _coerce_rpc_id(request_id), "result": result})

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self.process is None or self.process.returncode is not None:
            await self.start()
        self._request_id += 1
        request_id = str(self._request_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            return await asyncio.wait_for(
                future,
                timeout=(
                    timeout if timeout is not None else self.settings.codex_request_timeout_seconds
                ),
            )
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CodexUnavailable(f"Codex request timed out: {method}") from exc

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if not process or process.returncode is not None or not process.stdin:
            raise CodexUnavailable("Codex App Server is not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            process.stdin.write(payload.encode("utf-8"))
            await process.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        error = CodexUnavailable("Codex App Server exited")
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Invalid App Server JSON: %r", line[:500])
                    continue
                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and method is None:
                    future = self._pending.pop(str(request_id), None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexUnavailable(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                params = message.get("params") or {}
                if self._stopping:
                    continue
                task = None
                if request_id is not None and method:
                    task = asyncio.create_task(
                        self.request_handler(str(request_id), method, params)
                    )
                elif method:
                    task = asyncio.create_task(self.event_handler(method, params))
                if task:
                    self._handler_tasks.add(task)
                    task.add_done_callback(self._handler_finished)
        except ValueError as exc:
            error = CodexUnavailable(
                "Codex App Server emitted a JSON line larger than "
                f"{self.settings.codex_event_line_limit_bytes} bytes"
            )
            logger.warning("Codex App Server stream limit exceeded: %s", exc)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def _handler_finished(self, task: asyncio.Task[None]) -> None:
        self._handler_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Codex event handler failed: %s", type(error).__name__)

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]
                logger.debug("Codex App Server: %s", text)


def _coerce_rpc_id(value: str) -> str | int:
    try:
        return int(value)
    except ValueError:
        return value
