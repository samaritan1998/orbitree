from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_ops.settings import Settings

from .base import CodexEventHandler, CodexRequestHandler, CodexUnavailable

logger = logging.getLogger(__name__)


@dataclass
class _PiProcess:
    session_id: str
    cwd: str
    process: asyncio.subprocess.Process
    branch_id: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    response_waiters: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_id: str | None = None
    current_turn_status: str = "completed"
    current_turn_error: str | None = None
    current_turn_usage: dict[str, Any] = field(default_factory=dict)
    assistant_item_counter: int = 0
    has_prompt: bool = False
    queue: dict[str, list[str]] = field(
        default_factory=lambda: {"steering": [], "follow_up": []}
    )
    turn_started_emitted: bool = False
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    exited: bool = False


class PiRpcAdapter:
    """Run Pi through its stable JSONL RPC boundary.

    The adapter deliberately implements the existing CodexAdapter surface first so
    Research Ops can switch runtimes without changing Work/Branch state semantics.
    Pi-specific steering and queue methods are exposed in addition to that surface.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        event_handler: CodexEventHandler,
        request_handler: CodexRequestHandler,
    ) -> None:
        self.settings = settings
        self.event_handler = event_handler
        self.request_handler = request_handler
        self._processes: dict[str, _PiProcess] = {}
        self._turn_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._started = False

    async def start(self) -> None:
        if not self.settings.codex_enabled:
            raise CodexUnavailable("Agent integration is disabled")
        if self._started:
            return
        self._started = True

    async def stop(self) -> None:
        processes = list(self._processes.values())
        for state in processes:
            await self._close_process(state)
        self._processes.clear()
        self._started = False

    async def health(self) -> dict[str, Any]:
        command = [*shlex.split(self.settings.pi_command), "--version"]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
                limit=self.settings.pi_event_line_limit_bytes,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0:
                raise CodexUnavailable(
                    stderr.decode(errors="replace").strip() or "pi --version failed"
                )
            return {
                "available": True,
                "protocol": "pi-rpc-jsonl",
                "version": stdout.decode(errors="replace").strip(),
                "provider": self.settings.pi_provider,
                "model": self.settings.pi_model,
                "thinking": self.settings.pi_thinking,
                "proxy_configured": bool(
                    self.settings.pi_http_proxy or self.settings.pi_https_proxy
                ),
                "active_sessions": sum(
                    not item.exited for item in self._processes.values()
                ),
            }
        except Exception as exc:
            return {
                "available": False,
                "protocol": "pi-rpc-jsonl",
                "error": str(exc),
                "proxy_configured": bool(
                    self.settings.pi_http_proxy or self.settings.pi_https_proxy
                ),
            }

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]:
        await self.start()
        session_id = str(uuid.uuid4())
        state = await self._spawn(
            session_id=session_id,
            cwd=cwd,
            instructions=instructions,
            name="Research Ops Pi",
        )
        return {"thread": {"id": state.session_id}}

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        await self.start()
        state = self._processes.get(thread_id)
        if state is None or state.exited:
            state = await self._spawn(session_id=thread_id, cwd=cwd, resume=True)
        return {"thread": {"id": state.session_id}}

    async def fork_thread(
        self, *, thread_id: str, cwd: str, instructions: str
    ) -> dict[str, Any]:
        await self.start()
        child_id = str(uuid.uuid4())
        state = await self._spawn(
            session_id=child_id,
            cwd=cwd,
            instructions=instructions,
            parent_session_id=thread_id,
            name="Research Ops Pi fork",
        )
        return {"thread": {"id": state.session_id}}

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]:
        del thread_id
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def set_goal(
        self,
        *,
        thread_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        update_token_budget: bool = False,
    ) -> dict[str, Any]:
        del thread_id, objective, status, token_budget, update_token_budget
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def clear_goal(self, *, thread_id: str) -> dict[str, Any]:
        del thread_id
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def send_message(
        self,
        *,
        thread_id: str,
        content: str,
        cwd: str,
        metadata: dict[str, str],
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.start()
        state = await self._ensure_process(thread_id, cwd=cwd)
        state.branch_id = metadata.get("research_ops_branch_id", state.branch_id)
        lock = self._turn_locks[state.session_id]
        async with lock:
            if state.current_turn_id is not None:
                raise CodexUnavailable("Pi session already has a running turn")
            turn_id = str(uuid.uuid4())
            state.current_turn_id = turn_id
            state.current_turn_status = "completed"
            state.current_turn_error = None
            state.current_turn_usage = {}
            state.assistant_item_counter = 0
            state.turn_started_emitted = True
            state.settled.clear()
            await self._emit(
                "turn/started",
                {
                    "_researchOpsBranchId": state.branch_id,
                    "threadId": state.session_id,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "inProgress"},
                },
            )
            try:
                accepted = await self._command(
                    state,
                    {"type": "prompt", "message": content},
                    timeout=self.settings.pi_request_timeout_seconds,
                )
                if accepted.get("success") is False:
                    raise CodexUnavailable(
                        str(accepted.get("error") or "Pi rejected the prompt")
                    )
                state.has_prompt = True
                try:
                    await asyncio.wait_for(
                        state.settled.wait(),
                        timeout=self.settings.pi_turn_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise CodexUnavailable("Pi turn did not settle before the timeout") from exc
                if state.current_turn_status in {"failed", "aborted"}:
                    raise CodexUnavailable(
                        state.current_turn_error
                        or f"Pi turn {state.current_turn_status}"
                    )
                return {
                    "thread": {"id": state.session_id},
                    "turn": {"id": turn_id},
                }
            finally:
                state.current_turn_id = None
                state.turn_started_emitted = False

    async def steer_message(self, *, thread_id: str, content: str, cwd: str) -> dict[str, Any]:
        state = await self._ensure_process(thread_id, cwd=cwd)
        return await self._command(
            state,
            {"type": "steer", "message": content},
            timeout=self.settings.pi_request_timeout_seconds,
        )

    async def follow_up(self, *, thread_id: str, content: str, cwd: str) -> dict[str, Any]:
        state = await self._ensure_process(thread_id, cwd=cwd)
        return await self._command(
            state,
            {"type": "follow_up", "message": content},
            timeout=self.settings.pi_request_timeout_seconds,
        )

    async def abort(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        state = await self._ensure_process(thread_id, cwd=cwd)
        return await self._command(
            state,
            {"type": "abort"},
            timeout=self.settings.pi_request_timeout_seconds,
        )

    async def get_state(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        state = await self._ensure_process(thread_id, cwd=cwd)
        return await self._command(
            state,
            {"type": "get_state"},
            timeout=self.settings.pi_request_timeout_seconds,
        )

    def snapshot(self, thread_id: str) -> dict[str, Any] | None:
        state = self._processes.get(thread_id)
        if state is None or state.exited:
            return None
        if state.current_turn_id is not None:
            runtime_state = "running"
        elif state.current_turn_status == "failed":
            runtime_state = "failed"
        elif state.current_turn_status == "aborted":
            runtime_state = "aborted"
        else:
            runtime_state = "idle"
        return {"state": runtime_state, "queue": state.queue}

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        del request_id, result
        raise CodexUnavailable("Pi RPC does not expose Codex approval requests")

    async def _ensure_process(self, session_id: str, *, cwd: str) -> _PiProcess:
        state = self._processes.get(session_id)
        if state is not None and not state.exited:
            return state
        return await self._spawn(session_id=session_id, cwd=cwd, resume=True)

    async def _spawn(
        self,
        *,
        session_id: str,
        cwd: str,
        instructions: str | None = None,
        parent_session_id: str | None = None,
        resume: bool = False,
        name: str | None = None,
    ) -> _PiProcess:
        existing = self._processes.get(session_id)
        if existing is not None and not existing.exited:
            return existing
        args = [*shlex.split(self.settings.pi_command), "--mode", "rpc"]
        if self.settings.pi_session_dir:
            args.extend(["--session-dir", str(self.settings.pi_session_dir)])
        if self.settings.pi_provider:
            args.extend(["--provider", self.settings.pi_provider])
        if self.settings.pi_model:
            args.extend(["--model", self.settings.pi_model])
        if self.settings.pi_thinking:
            args.extend(["--thinking", self.settings.pi_thinking])
        if not self.settings.pi_extensions_enabled:
            args.append("--no-extensions")
        if self.settings.pi_tools:
            args.extend(["--tools", self.settings.pi_tools])
        if name:
            args.extend(["--name", name])
        if parent_session_id:
            args.extend(["--fork", parent_session_id])
        elif resume:
            args.extend(["--session", session_id])
        else:
            args.extend(["--session-id", session_id])
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(Path(cwd).resolve()),
                env=self._environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.settings.pi_event_line_limit_bytes,
            )
        except FileNotFoundError as exc:
            raise CodexUnavailable(f"Pi command not found: {args[0]}") from exc
        state = _PiProcess(
            session_id=session_id,
            cwd=str(Path(cwd).resolve()),
            process=process,
            has_prompt=resume,
        )
        self._processes[session_id] = state
        state.reader_task = asyncio.create_task(self._read_stdout(state))
        state.stderr_task = asyncio.create_task(self._read_stderr(state))
        try:
            handshake = await self._command(
                state,
                {"type": "get_state"},
                timeout=self.settings.pi_start_timeout_seconds,
            )
            if handshake.get("success") is False:
                raise CodexUnavailable(
                    str(handshake.get("error") or "Pi RPC rejected the startup handshake")
                )
            state.ready.set()
        except Exception as exc:
            detail = await self._process_error(state)
            await self._close_process(state)
            raise CodexUnavailable(detail or str(exc) or "Pi RPC did not become ready") from exc
        return state

    async def _command(
        self,
        state: _PiProcess,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if state.exited or state.process.returncode is not None:
            raise CodexUnavailable("Pi RPC process is not running")
        request_id = str(uuid.uuid4())
        command = {**payload, "id": request_id}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        state.response_waiters[request_id] = future
        try:
            assert state.process.stdin is not None
            encoded = (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
            async with state.write_lock:
                state.process.stdin.write(encoded)
                await state.process.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except (BrokenPipeError, ConnectionError) as exc:
            raise CodexUnavailable("Pi RPC stdin is closed") from exc
        finally:
            state.response_waiters.pop(request_id, None)

    async def _read_stdout(self, state: _PiProcess) -> None:
        assert state.process.stdout is not None
        try:
            while line := await state.process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response":
                    request_id = str(event.get("id", ""))
                    future = state.response_waiters.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(event)
                    state.ready.set()
                    continue
                if event.get("type") == "session":
                    observed_id = str(event.get("id") or "").strip()
                    if observed_id and observed_id != state.session_id:
                        self._processes.pop(state.session_id, None)
                        state.session_id = observed_id
                        self._processes[observed_id] = state
                    state.ready.set()
                elif not state.ready.is_set():
                    state.ready.set()
                await self._forward_event(state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.current_turn_error = str(exc)
            logger.exception("Pi RPC reader failed for %s", state.session_id)
        finally:
            state.exited = True
            state.settled.set()
            error = CodexUnavailable(
                state.current_turn_error or "Pi RPC process exited unexpectedly"
            )
            for future in list(state.response_waiters.values()):
                if not future.done():
                    future.set_exception(error)

    async def _read_stderr(self, state: _PiProcess) -> None:
        assert state.process.stderr is not None
        try:
            while line := await state.process.stderr.readline():
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                await self._emit(
                    "pi/transport",
                    {
                        "_researchOpsBranchId": state.branch_id,
                        "threadId": state.session_id,
                        "error": {"message": text[:4_000]},
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pi RPC stderr reader failed for %s", state.session_id)

    async def _forward_event(self, state: _PiProcess, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        common = {
            "_researchOpsBranchId": state.branch_id,
            "threadId": state.session_id,
            "turnId": state.current_turn_id,
            "runtime": "pi",
        }
        if event_type == "turn_start":
            if not state.turn_started_emitted:
                state.turn_started_emitted = True
                await self._emit(
                    "turn/started",
                    {
                        **common,
                        "turn": {
                            "id": state.current_turn_id or str(uuid.uuid4()),
                            "status": "inProgress",
                        },
                    },
                )
            return
        if event_type == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta" and update.get("delta"):
                await self._emit(
                    "item/agentMessage/delta",
                    {
                        **common,
                        "itemId": self._assistant_item_id(state),
                        "delta": str(update["delta"]),
                    },
                )
            return
        if event_type == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                text = _message_text(message.get("content"))
                if text:
                    item_id = str(
                        message.get("id")
                        or message.get("responseId")
                        or self._assistant_item_id(state)
                    )
                    state.assistant_item_counter += 1
                    await self._emit(
                        "item/completed",
                        {
                            **common,
                            "item": {
                                "id": item_id,
                                "type": "agentMessage",
                                "text": text,
                            },
                        },
                    )
            return
        if event_type == "tool_execution_start":
            tool_id = str(event.get("toolCallId") or uuid.uuid4())
            await self._emit(
                "item/started",
                {
                    **common,
                    "item": {
                        "id": tool_id,
                        "type": "commandExecution",
                        "command": _tool_label(event),
                        "status": "inProgress",
                    },
                },
            )
            return
        if event_type == "tool_execution_update":
            partial = event.get("partialResult") or event.get("result") or {}
            delta = _message_text(partial.get("content") if isinstance(partial, dict) else partial)
            if delta:
                await self._emit(
                    "command/exec/outputDelta",
                    {**common, "itemId": event.get("toolCallId"), "delta": delta[-4_000:]},
                )
            return
        if event_type == "tool_execution_end":
            result = event.get("result") or {}
            is_error = bool(event.get("isError") or result.get("isError"))
            await self._emit(
                "item/completed",
                {
                    **common,
                    "item": {
                        "id": event.get("toolCallId") or str(uuid.uuid4()),
                        "type": "commandExecution",
                        "command": _tool_label(event),
                        "status": "failed" if is_error else "completed",
                        "exitCode": 1 if is_error else 0,
                    },
                },
            )
            return
        if event_type == "turn_end":
            message = event.get("message") or {}
            status = _turn_status(message.get("stopReason"))
            state.current_turn_status = status
            state.current_turn_usage = _merge_usage(
                state.current_turn_usage,
                message.get("usage") if isinstance(message.get("usage"), dict) else {},
            )
            if status != "completed":
                state.current_turn_error = str(message.get("stopReason") or status)
            return
        if event_type == "agent_settled":
            await self._emit(
                "turn/completed",
                {
                    **common,
                    "turn": {
                        "id": state.current_turn_id or str(uuid.uuid4()),
                        "status": state.current_turn_status,
                        "usage": state.current_turn_usage,
                    },
                },
            )
            await self._emit("pi/settled", common)
            state.settled.set()
            return
        if event_type == "queue_update":
            state.queue = _safe_queue(event)
            await self._emit("pi/queue_update", {**common, "queue": state.queue})
            return
        if event_type.startswith("compaction"):
            await self._emit("pi/" + event_type, {**common, "event": _safe_event(event)})
            return
        if event_type in {"error", "extension_error", "auto_retry_end"}:
            error = event.get("message") or event.get("error") or event_type
            state.current_turn_error = str(error)[:4_000]
            state.current_turn_status = "failed"
            await self._emit(
                "error",
                {**common, "error": {"message": state.current_turn_error}},
            )

    async def _emit(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self.event_handler(method, params)
        except Exception:
            logger.exception("Research Ops event handler failed for Pi event %s", method)

    async def _close_process(self, state: _PiProcess) -> None:
        current = asyncio.current_task()
        for task in (state.reader_task, state.stderr_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        process = state.process
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)
            if process.returncode is None:
                process.kill()
                await process.wait()
        for task in (state.reader_task, state.stderr_task):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        state.exited = True

    async def _process_error(self, state: _PiProcess) -> str | None:
        if state.current_turn_error:
            return state.current_turn_error
        if state.process.returncode is None:
            return None
        return f"Pi RPC exited with status {state.process.returncode}"

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key, value in (
            ("HTTP_PROXY", self.settings.pi_http_proxy),
            ("HTTPS_PROXY", self.settings.pi_https_proxy),
            ("http_proxy", self.settings.pi_http_proxy),
            ("https_proxy", self.settings.pi_https_proxy),
            ("NO_PROXY", self.settings.pi_no_proxy),
            ("no_proxy", self.settings.pi_no_proxy),
        ):
            if value:
                environment[key] = value
        return environment

    @staticmethod
    def _assistant_item_id(state: _PiProcess) -> str:
        return f"{state.current_turn_id or 'turn'}:assistant:{state.assistant_item_counter}"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "".join(parts)


def _tool_label(event: dict[str, Any]) -> str:
    name = str(event.get("toolName") or event.get("tool_name") or "tool")
    arguments = event.get("args") or event.get("arguments")
    if arguments is None:
        return name
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return f"{name} {encoded}"[:4_000]


def _turn_status(stop_reason: Any) -> str:
    normalized = str(stop_reason or "").casefold()
    if normalized in {"abort", "aborted", "cancelled", "canceled"}:
        return "aborted"
    if normalized in {"error", "failed", "failure"}:
        return "failed"
    return "completed"


def _safe_queue(event: dict[str, Any]) -> dict[str, list[str]]:
    def values(key: str) -> list[str]:
        raw = event.get(key)
        return [str(item)[:2_000] for item in raw] if isinstance(raw, list) else []

    return {"steering": values("steering"), "follow_up": values("followUp")}


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = {"reason", "tokensBefore", "tokensAfter", "firstKeptEntryId"}
    return {key: event[key] for key in allowed if key in event}


def _merge_usage(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key, 0), (int, float)):
            merged[key] = merged.get(key, 0) + value
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_usage(merged[key], value)
        else:
            merged[key] = value
    return merged
