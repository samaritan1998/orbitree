from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from research_ops.settings import Settings

from .base import CodexEventHandler, CodexRequestHandler, CodexUnavailable
from .claude_execution import claude_execution_snapshot
from .claude_handoff import handoff_path


class ClaudeCliAdapter:
    """Expose Claude Code's print-mode JSON stream through the agent runtime API."""

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
        self._turn_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._runtime_state: dict[str, str] = {}
        self._approvals: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._started = False
        self._state_dir = settings.runtime_data_dir / "claude-runtime"

    async def start(self) -> None:
        if not self.settings.codex_enabled or not self.settings.claude_enabled:
            raise CodexUnavailable("Claude integration is disabled")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._started = True

    async def stop(self) -> None:
        for process in list(self._processes.values()):
            await self._terminate(process)
        self._processes.clear()
        self._started = False

    async def health(self) -> dict[str, Any]:
        command = [*shlex.split(self.settings.claude_command), "--version"]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.settings.claude_event_line_limit_bytes,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            if process.returncode != 0:
                raise CodexUnavailable(
                    stderr.decode(errors="replace").strip() or "claude --version failed"
                )
            return {
                "available": True,
                "protocol": "claude-stream-json",
                "version": stdout.decode(errors="replace").strip(),
                "model": self.settings.claude_model,
                "effort": self.settings.claude_effort,
                "active_sessions": len(self._processes),
            }
        except Exception as exc:
            return {
                "available": False,
                "protocol": "claude-stream-json",
                "error": str(exc),
            }

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]:
        del instructions
        await self.start()
        session_id = str(uuid.uuid4())
        self._write_pending(session_id, cwd=cwd)
        return {"thread": {"id": session_id}}

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del cwd
        await self.start()
        return {"thread": {"id": thread_id}}

    async def fork_thread(
        self, *, thread_id: str, cwd: str, instructions: str
    ) -> dict[str, Any]:
        del instructions
        await self.start()
        child_id = str(uuid.uuid4())
        self._write_pending(
            child_id,
            cwd=cwd,
            parent_session_id=thread_id,
        )
        return {"thread": {"id": child_id}}

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]:
        del thread_id
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def read_goal_thread(self, *, thread_id: str) -> dict[str, Any]:
        del thread_id
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def read_goal_turn(self, *, thread_id: str) -> dict[str, Any]:
        del thread_id
        raise CodexUnavailable("Native Goal is only available for Codex threads")

    async def prepare_goal_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del thread_id, cwd
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
        del additional_context
        await self.start()
        async with self._turn_locks[thread_id]:
            if thread_id in self._processes:
                raise CodexUnavailable("Claude session already has a running turn")
            turn_id = str(uuid.uuid4())
            branch_id = metadata.get("research_ops_branch_id", "")
            await self._emit(
                "turn/started",
                {
                    "_researchOpsBranchId": branch_id,
                    "runtime": "claude",
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "inProgress"},
                },
            )
            pending = self._read_pending(thread_id)
            initial_context = self._initial_handoff(thread_id)
            prompt = (
                f"{initial_context}\n\nCurrent user message:\n{content}"
                if initial_context else content
            )
            use_stdin = len(prompt.encode("utf-8")) > 100_000
            control = self.settings.claude_web_approvals_enabled
            args = self._turn_command(
                thread_id=thread_id,
                content=None if use_stdin else prompt,
                fresh=pending is not None and self._session_path(thread_id) is None,
                execution={
                    "model": metadata.get("research_ops_model"),
                    "reasoning_effort": metadata.get("research_ops_reasoning_effort"),
                    "permission_mode": metadata.get("research_ops_permission_mode"),
                },
                control=control,
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(Path(cwd).resolve()),
                    stdin=asyncio.subprocess.PIPE if use_stdin or control else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.settings.claude_event_line_limit_bytes,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise CodexUnavailable(f"Claude command not found: {args[0]}") from exc
            self._processes[thread_id] = process
            self._runtime_state[thread_id] = "running"
            stderr_task = asyncio.create_task(self._read_stderr(process))
            stdin_task = (
                asyncio.create_task(self._write_prompt(process, prompt))
                if use_stdin and not control else None
            )
            assistant_texts: list[str] = []
            tool_names: dict[str, str] = {}
            wakeup_requests: dict[str, dict[str, Any]] = {}
            result_payload: dict[str, Any] = {}
            try:
                assert process.stdout is not None
                if control:
                    await self._initialize_control(process)
                    await self._write_control(process, {
                        "type": "user", "session_id": thread_id, "parent_tool_use_id": None,
                        "message": {"role": "user", "content": prompt},
                        "client_composed": True,
                    })
                async with asyncio.timeout(self.settings.claude_turn_timeout_seconds):
                    async for raw_line in process.stdout:
                        line = raw_line.decode(errors="replace").strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "control_request":
                            await self._request_approval(event, branch_id, thread_id, turn_id)
                            continue
                        if event.get("type") == "system" and event.get("subtype") == "init":
                            self._clear_pending(thread_id)
                        if initial_context and (
                            event.get("type") == "assistant"
                            or (event.get("type") == "result" and not event.get("is_error"))
                        ):
                            self._mark_handoff_delivered(thread_id)
                        await self._translate_event(
                            event,
                            branch_id=branch_id,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            assistant_texts=assistant_texts,
                            tool_names=tool_names,
                            wakeup_requests=wakeup_requests,
                        )
                        if event.get("type") == "result":
                            result_payload = event
                            if control and process.stdin is not None:
                                process.stdin.close()
                    return_code = await process.wait()
                    if stdin_task is not None:
                        await stdin_task
                stderr = await stderr_task
                if return_code != 0 or result_payload.get("is_error"):
                    error = str(
                        result_payload.get("result")
                        or result_payload.get("error")
                        or stderr
                        or f"Claude exited with status {return_code}"
                    ).strip()
                    self._runtime_state[thread_id] = "failed"
                    await self._emit(
                        "error",
                        self._params(branch_id, thread_id, turn_id, error=error),
                    )
                    await self._emit_turn_completed(
                        branch_id, thread_id, turn_id, "failed", result_payload
                    )
                    raise CodexUnavailable(error)
                final_result = str(result_payload.get("result") or "").strip()
                if final_result and (
                    not assistant_texts or assistant_texts[-1].strip() != final_result
                ):
                    await self._emit_assistant(
                        branch_id, thread_id, turn_id, str(uuid.uuid4()), final_result
                    )
                self._runtime_state[thread_id] = "idle"
                await self._emit_turn_completed(
                    branch_id, thread_id, turn_id, "completed", result_payload
                )
                return {"thread": {"id": thread_id}, "turn": {"id": turn_id}}
            except (asyncio.CancelledError, TimeoutError):
                self._runtime_state[thread_id] = "aborted"
                await self._terminate(process)
                await self._emit_turn_completed(
                    branch_id, thread_id, turn_id, "aborted", result_payload
                )
                raise
            finally:
                for request_id, (owner, _, _) in list(self._approvals.items()):
                    if owner == thread_id:
                        self._approvals.pop(request_id, None)
                if process.returncode is None:
                    await self._terminate(process)
                if stdin_task is not None and not stdin_task.done():
                    stdin_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stdin_task
                if not stderr_task.done():
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task
                self._processes.pop(thread_id, None)
                await self._emit("claude/approvals/expired", self._params(
                    branch_id, thread_id, turn_id,
                ))

    async def abort(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del cwd
        process = self._processes.get(thread_id)
        if process is None:
            return {"accepted": False, "reason": "Claude is not running"}
        await self._terminate(process)
        self._runtime_state[thread_id] = "aborted"
        return {"accepted": True}

    def snapshot(self, thread_id: str) -> dict[str, Any] | None:
        state = self._runtime_state.get(thread_id)
        if state is None:
            return None
        return {"state": state, "queue": {"steering": [], "follow_up": []}}

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        pending = self._approvals.get(request_id)
        if pending is None:
            raise CodexUnavailable("This Claude approval has expired; it was not executed")
        thread_id, native_id, original_input = pending
        process = self._processes.get(thread_id)
        if process is None or process.returncode is not None:
            raise CodexUnavailable("Claude is no longer waiting for this approval")
        decision = result.get("decision")
        if decision not in {"accept", "decline", "cancel"}:
            raise CodexUnavailable("Claude requires an explicit one-time approval or rejection")
        response = (
            {"behavior": "allow", "updatedInput": original_input}
            if decision == "accept" else
            {"behavior": "deny", "message": "The user declined this operation"}
        )
        self._approvals.pop(request_id, None)
        await self._write_control(process, {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": native_id, "response": response},
        })
        self._runtime_state[thread_id] = "running"

    @staticmethod
    async def _write_control(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
        if process.stdin is None or process.stdin.is_closing():
            raise CodexUnavailable("Claude permission channel is closed")
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await process.stdin.drain()

    async def _initialize_control(self, process: asyncio.subprocess.Process) -> None:
        request_id = str(uuid.uuid4())
        await self._write_control(process, {
            "type": "control_request", "request_id": request_id,
            "request": {"subtype": "initialize", "hooks": None},
        })
        assert process.stdout is not None
        async with asyncio.timeout(60):
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                response = event.get("response") or {}
                if (
                    event.get("type") == "control_response"
                    and response.get("request_id") == request_id
                ):
                    if response.get("subtype") == "error":
                        raise CodexUnavailable("Claude could not initialize the permission channel")
                    return
        raise CodexUnavailable("Claude exited before initializing the permission channel")

    async def _request_approval(
        self, event: dict[str, Any], branch_id: str, thread_id: str, turn_id: str
    ) -> None:
        request = event.get("request") or {}
        native_id = str(event.get("request_id") or "")
        process = self._processes[thread_id]
        if request.get("subtype") != "can_use_tool":
            await self._write_control(process, {
                "type": "control_response", "response": {
                    "subtype": "error", "request_id": native_id,
                    "error": "Unsupported Orbitree control request",
                },
            })
            return
        request_id = f"claude:{uuid.uuid4()}"
        original_input = request.get("input") or {}
        self._approvals[request_id] = (thread_id, native_id, original_input)
        self._runtime_state[thread_id] = "waiting_approval"
        await self.request_handler(request_id, "claude/tool/requestApproval", {
            **self._params(branch_id, thread_id, turn_id),
            "tool": request.get("tool_name"), "input": original_input,
            "blocked_path": request.get("blocked_path"),
            "reason": request.get("decision_reason"),
        })

    def terminal_command(
        self, *, thread_id: str, cwd: str, execution: dict[str, Any] | None = None
    ) -> str:
        selected = claude_execution_snapshot(self.settings, execution)
        command = [*shlex.split(self.settings.claude_command)]
        if self._session_path(thread_id) is not None:
            self._clear_pending(thread_id)
            command.extend(["--resume", thread_id])
        elif self._read_pending(thread_id) is not None:
            command.extend(["--session-id", thread_id])
        else:
            command.extend(["--resume", thread_id])
        command.extend([
            "--add-dir", str(Path(cwd).resolve()),
            "--model", selected["model"],
            "--effort", selected["reasoning_effort"],
            "--permission-mode", selected["permission_mode"],
        ])
        initial_context = self._initial_handoff(thread_id)
        if initial_context:
            command.append(initial_context)
        return shlex.join(command)

    def _initial_handoff(self, thread_id: str) -> str | None:
        path = handoff_path(self._state_dir, thread_id)
        if not path.is_file() or path.with_suffix(".sent").exists():
            return None
        session_path = self._session_path(thread_id)
        if session_path is not None:
            # A TUI may consume the handoff without the Web adapter observing it.
            # Inspect only the beginning of this one session, not the whole history.
            with session_path.open("rb") as handle:
                for line in handle.read(1024 * 1024).splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(event, dict) and event.get("type") == "user":
                        return None
        return path.read_text(encoding="utf-8")

    def _mark_handoff_delivered(self, thread_id: str) -> None:
        path = handoff_path(self._state_dir, thread_id).with_suffix(".sent")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)

    def _turn_command(
        self,
        *,
        thread_id: str,
        content: str | None,
        fresh: bool,
        execution: dict[str, Any] | None = None,
        control: bool = False,
    ) -> list[str]:
        selected = claude_execution_snapshot(self.settings, execution)
        command = [
            *shlex.split(self.settings.claude_command),
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            selected["permission_mode"],
            "--model",
            selected["model"],
            "--effort",
            selected["reasoning_effort"],
            "--name",
            "Orbitree",
        ]
        if fresh:
            command.extend(["--session-id", thread_id])
        else:
            command.extend(["--resume", thread_id])
        if control:
            command.extend(["--input-format", "stream-json", "--permission-prompt-tool", "stdio"])
        elif content is None:
            command.extend(["--input-format", "text"])
        else:
            command.append(content)
        return command

    @staticmethod
    async def _write_prompt(process: asyncio.subprocess.Process, prompt: str) -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The normal exit/stderr path reports an early CLI failure.
        finally:
            process.stdin.close()

    async def _translate_event(
        self,
        event: dict[str, Any],
        *,
        branch_id: str,
        thread_id: str,
        turn_id: str,
        assistant_texts: list[str],
        tool_names: dict[str, str],
        wakeup_requests: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        event_type = event.get("type")
        if event_type == "stream_event":
            stream = event.get("event") or {}
            if stream.get("type") == "content_block_delta":
                delta = stream.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    await self._emit(
                        "item/agentMessage/delta",
                        {
                            **self._params(branch_id, thread_id, turn_id),
                            "itemId": f"claude-message-{turn_id}",
                            "delta": str(delta["text"]),
                        },
                    )
            return
        if event_type == "assistant":
            message = event.get("message") or {}
            message_id = str(message.get("id") or uuid.uuid4())
            texts: list[str] = []
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    tool_id = str(block.get("id") or uuid.uuid4())
                    name = str(block.get("name") or "tool")
                    tool_names[tool_id] = name
                    tool_input = block.get("input")
                    if (
                        name == "ScheduleWakeup" and isinstance(tool_input, dict)
                        and wakeup_requests is not None
                    ):
                        wakeup_requests[tool_id] = dict(tool_input)
                    rendered = json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
                    await self._emit(
                        "item/started",
                        {
                            **self._params(branch_id, thread_id, turn_id),
                            "item": {
                                "id": tool_id,
                                "type": "commandExecution",
                                "command": f"{name} {rendered}".strip(),
                                "status": "inProgress",
                            },
                        },
                    )
            text = "\n".join(texts).strip()
            if text:
                assistant_texts.append(text)
                await self._emit_assistant(branch_id, thread_id, turn_id, message_id, text)
            return
        if event_type == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or uuid.uuid4())
                wakeup = wakeup_requests.pop(tool_id, None) if wakeup_requests is not None else None
                if wakeup is not None and not block.get("is_error"):
                    await self._emit("claude/wakeup/scheduled", self._params(
                        branch_id, thread_id, turn_id, toolId=tool_id, input=wakeup,
                    ))
                content = block.get("content")
                if isinstance(content, list):
                    output = "\n".join(
                        str(item.get("text") or "") if isinstance(item, dict) else str(item)
                        for item in content
                    )
                else:
                    output = str(content or "")
                if output:
                    await self._emit(
                        "command/exec/outputDelta",
                        {
                            **self._params(branch_id, thread_id, turn_id),
                            "itemId": tool_id,
                            "delta": output,
                        },
                    )
                await self._emit(
                    "item/completed",
                    {
                        **self._params(branch_id, thread_id, turn_id),
                        "item": {
                            "id": tool_id,
                            "type": "commandExecution",
                            "command": tool_names.get(tool_id, "Claude tool"),
                            "status": "failed" if block.get("is_error") else "completed",
                            "exitCode": 1 if block.get("is_error") else 0,
                        },
                    },
                )

    async def _emit_assistant(
        self,
        branch_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        text: str,
    ) -> None:
        await self._emit(
            "item/completed",
            {
                **self._params(branch_id, thread_id, turn_id),
                "item": {
                    "id": item_id,
                    "type": "agentMessage",
                    "text": text,
                    "status": "completed",
                },
            },
        )

    async def _emit_turn_completed(
        self,
        branch_id: str,
        thread_id: str,
        turn_id: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        await self._emit(
            "turn/completed",
            {
                **self._params(branch_id, thread_id, turn_id),
                "turn": {
                    "id": turn_id,
                    "status": status,
                    "usage": result.get("usage") or {},
                },
            },
        )

    @staticmethod
    def _params(branch_id: str, thread_id: str, turn_id: str, **extra: Any) -> dict[str, Any]:
        return {
            "_researchOpsBranchId": branch_id,
            "runtime": "claude",
            "threadId": thread_id,
            "turnId": turn_id,
            **extra,
        }

    async def _emit(self, method: str, params: dict[str, Any]) -> None:
        await self.event_handler(method, params)

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        chunks: list[str] = []
        async for raw_line in process.stderr:
            chunks.append(raw_line.decode(errors="replace"))
        return "".join(chunks)[-8_000:].strip()

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    def _state_path(self, session_id: str) -> Path:
        return self._state_dir / f"{session_id}.json"

    def _write_pending(
        self,
        session_id: str,
        *,
        cwd: str,
        parent_session_id: str | None = None,
    ) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(session_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "cwd": str(Path(cwd).resolve()),
                    "parent_session_id": parent_session_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _read_pending(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._state_path(session_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _clear_pending(self, session_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._state_path(session_id).unlink()

    def _session_path(self, session_id: str) -> Path | None:
        projects = self.settings.claude_home / "projects"
        return next(projects.glob(f"*/{session_id}.jsonl"), None)
