from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from research_ops.settings import Settings

from .app_server import AppServerCodexAdapter
from .base import CodexEventHandler, CodexRequestHandler, CodexUnavailable
from .identity import CodexLaunch
from .rollout import find_rollout


class CliCodexAdapter:
    """CLI turns, reusing App Server for threads it already owns through a native Goal."""

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
        self._native_goal_threads: set[str] = set()
        self._goal_release_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_web_turns: dict[str, tuple[str, asyncio.Queue[dict[str, Any]]]] = {}
        self.lifecycle = AppServerCodexAdapter(
            settings,
            event_handler=self._handle_lifecycle_event,
            request_handler=request_handler,
            launch=launch,
        )
        self._thread_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._started = False

    async def start(self) -> None:
        if not self.settings.codex_enabled:
            raise CodexUnavailable("Codex integration is disabled")
        if self._started:
            return
        # Official CLI turns remain usable even if the local lineage server is unavailable.
        with contextlib.suppress(Exception):
            await self.lifecycle.start()
        self._started = True

    async def stop(self) -> None:
        for task in self._goal_release_tasks:
            task.cancel()
        if self._goal_release_tasks:
            await asyncio.gather(*self._goal_release_tasks, return_exceptions=True)
        self._goal_release_tasks.clear()
        await self.lifecycle.stop()
        self._native_goal_threads.clear()
        self._started = False

    async def health(self) -> dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.codex_command,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            if process.returncode != 0:
                raise CodexUnavailable(stderr.decode().strip() or "codex --version failed")
            lifecycle = await self.lifecycle.health()
            return {
                "available": True,
                "protocol": "official-cli-jsonl",
                "version": stdout.decode().strip(),
                "fork_support": lifecycle.get("available", False),
            }
        except Exception as exc:
            return {"available": False, "protocol": "official-cli-jsonl", "error": str(exc)}

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]:
        del cwd, instructions
        # `thread/start` in App Server does not create the local rollout required by
        # `codex exec resume`. The official CLI creates the durable thread on its first turn.
        return {"thread": {"id": f"deferred:{uuid.uuid4()}"}}

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del cwd
        return {"thread": {"id": thread_id}}

    async def fork_thread(self, *, thread_id: str, cwd: str, instructions: str) -> dict[str, Any]:
        if thread_id.startswith("deferred:"):
            return {"thread": {"id": f"deferred:{uuid.uuid4()}"}}
        adapter = self.lifecycle
        if self._native_goal_threads or self._lifecycle_web_turns:
            # Stopping the shared App Server would also stop every native Goal
            # operation it currently owns. Isolate lineage work while any Goal
            # is active, then release only the temporary child's writer.
            adapter = AppServerCodexAdapter(
                self.settings,
                event_handler=self.event_handler,
                request_handler=self.request_handler,
                launch=self.launch,
            )
        try:
            return await adapter.fork_thread(
                thread_id=thread_id,
                cwd=cwd,
                instructions=instructions,
            )
        except Exception as exc:
            raise CodexUnavailable(f"Unable to fork persisted Codex thread: {exc}") from exc
        finally:
            # App Server's fork operation keeps a thread-store writer open for
            # the returned child. Normal Research Ops turns use the official
            # CLI, so leave the lineage process stopped after the fork; this
            # releases the lock before the child is resumed by Web/CLI.
            with contextlib.suppress(Exception):
                await adapter.stop()

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]:
        response = await self.lifecycle.get_goal(thread_id=thread_id)
        goal = response.get("goal")
        if isinstance(goal, dict) and goal.get("status") == "active":
            self._native_goal_threads.add(thread_id)
        else:
            self._native_goal_threads.discard(thread_id)
            self._schedule_thread_release(thread_id)
        return response

    async def read_goal_thread(self, *, thread_id: str) -> dict[str, Any]:
        return await self.lifecycle.read_goal_thread(thread_id=thread_id)

    async def read_goal_turn(self, *, thread_id: str) -> dict[str, Any]:
        return await self.lifecycle.read_goal_turn(thread_id=thread_id)

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self.lifecycle.interrupt_turn(thread_id=thread_id, turn_id=turn_id)

    async def prepare_goal_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        return await self.lifecycle.prepare_goal_thread(thread_id=thread_id, cwd=cwd)

    async def set_goal(
        self,
        *,
        thread_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        update_token_budget: bool = False,
    ) -> dict[str, Any]:
        was_active = thread_id in self._native_goal_threads
        if status == "active":
            self._native_goal_threads.add(thread_id)
        elif status is not None:
            self._native_goal_threads.discard(thread_id)
        try:
            response = await self.lifecycle.set_goal(
                thread_id=thread_id,
                objective=objective,
                status=status,
                token_budget=token_budget,
                update_token_budget=update_token_budget,
            )
        except Exception:
            if was_active:
                self._native_goal_threads.add(thread_id)
            else:
                self._native_goal_threads.discard(thread_id)
            raise
        goal = response.get("goal")
        if isinstance(goal, dict) and goal.get("status") == "active":
            self._native_goal_threads.add(thread_id)
        else:
            self._native_goal_threads.discard(thread_id)
            self._schedule_thread_release(thread_id)
        return response

    async def clear_goal(self, *, thread_id: str) -> dict[str, Any]:
        try:
            return await self.lifecycle.clear_goal(thread_id=thread_id)
        finally:
            self._native_goal_threads.discard(thread_id)
            self._schedule_thread_release(thread_id)

    async def _handle_lifecycle_event(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        thread_id = str(params.get("threadId") or "")
        web_turn = self._lifecycle_web_turns.get(thread_id)
        if web_turn:
            params = {**params, "_researchOpsBranchId": web_turn[0]}
        if method == "thread/goal/updated" and thread_id:
            goal = params.get("goal") or {}
            if goal.get("status") == "active":
                self._native_goal_threads.add(thread_id)
            else:
                self._native_goal_threads.discard(thread_id)
                self._schedule_thread_release(thread_id)
        elif method == "thread/goal/cleared" and thread_id:
            self._native_goal_threads.discard(thread_id)
            self._schedule_thread_release(thread_id)
        elif (
            method == "turn/completed" and thread_id
            and thread_id not in self._native_goal_threads
        ):
            self._schedule_thread_release(thread_id)
        try:
            await self.event_handler(method, params)
        finally:
            if web_turn and method == "turn/completed":
                web_turn[1].put_nowait(params.get("turn") or {})

    async def _unsubscribe_thread(self, thread_id: str) -> None:
        with contextlib.suppress(Exception):
            await self.lifecycle.unsubscribe_thread(thread_id=thread_id)

    def _schedule_thread_release(self, thread_id: str) -> None:
        task = asyncio.create_task(self._release_inactive_thread(thread_id))
        self._goal_release_tasks.add(task)
        task.add_done_callback(self._goal_release_tasks.discard)

    async def _release_inactive_thread(self, thread_id: str) -> None:
        # Let the App Server reader finish dispatching the notification before
        # issuing a request that needs the same reader to resolve its response.
        await asyncio.sleep(0)
        if (
            thread_id not in self._native_goal_threads
            and thread_id not in self._lifecycle_web_turns
        ):
            with contextlib.suppress(Exception):
                thread = await self.lifecycle.read_goal_thread(thread_id=thread_id)
                status = (thread.get("thread", {}).get("status") or {}).get("type")
                if (
                    status != "active"
                    and thread_id not in self._native_goal_threads
                    and thread_id not in self._lifecycle_web_turns
                ):
                    await self._unsubscribe_thread(thread_id)

    async def _send_lifecycle_message(
        self, *, thread_id: str, prompt: str, cwd: str, branch_id: str
    ) -> dict[str, Any]:
        # Unsubscribe is not a writer release. Keep the existing owner and
        # wait for the requested turn, just as the CLI path waits for exec.
        completed: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifecycle_web_turns[thread_id] = (branch_id, completed)
        turn_id: str | None = None
        try:
            resumed = await self.lifecycle.prepare_goal_thread(thread_id=thread_id, cwd=cwd)
            status = (resumed.get("thread", {}).get("status") or {}).get("type")
            if status == "active":
                current = await self.lifecycle.read_goal_turn(thread_id=thread_id)
                if current.get("status") == "inProgress" and current.get("id"):
                    response = await self.lifecycle.steer_message(
                        thread_id=thread_id, turn_id=current["id"], content=prompt
                    )
                    turn_id = response.get("turnId")
            if turn_id is None:
                response = await self.lifecycle.send_message(
                    thread_id=thread_id,
                    content=prompt,
                    cwd=cwd,
                    metadata={"research_ops_branch_id": branch_id},
                )
                turn_id = response.get("turn", {}).get("id")
            if not turn_id:
                raise CodexUnavailable("Codex did not acknowledge the Web message with a turn ID")
            while True:
                try:
                    turn = await asyncio.wait_for(completed.get(), timeout=1)
                except TimeoutError:
                    process = self.lifecycle.process
                    if process is None or process.returncode is not None:
                        raise CodexUnavailable(
                            "Codex App Server exited before the Web turn finished"
                        ) from None
                    continue
                if turn.get("id") != turn_id:
                    continue
                if turn.get("status") != "completed":
                    error = turn.get("error") or {}
                    reason = error.get("message") if isinstance(error, dict) else str(error)
                    raise CodexUnavailable(reason or f"Codex turn {turn.get('status', 'failed')}")
                return {"thread": {"id": thread_id}, "turn": {"id": turn_id}}
        except asyncio.CancelledError:
            if turn_id:
                with contextlib.suppress(Exception):
                    await self.lifecycle.interrupt_turn(thread_id=thread_id, turn_id=turn_id)
            raise
        finally:
            self._lifecycle_web_turns.pop(thread_id, None)
            self._schedule_thread_release(thread_id)

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
        branch_id = metadata.get("research_ops_branch_id", "")
        turn_id = str(uuid.uuid4())
        can_resume = (
            not thread_id.startswith("deferred:")
            and find_rollout(self.settings.codex_home / "sessions", thread_id) is not None
        )
        if not can_resume and not thread_id.startswith("deferred:"):
            raise CodexUnavailable(
                f"No saved rollout found for thread {thread_id}; refusing to replace its history. "
                "Restore the session or explicitly create a new conversation."
            )
        prompt = content
        lock_key = thread_id if can_resume else branch_id
        async with self._thread_locks[lock_key]:
            if can_resume and await self.lifecycle.owns_thread(thread_id=thread_id):
                return await self._send_lifecycle_message(
                    thread_id=thread_id, prompt=prompt, cwd=cwd, branch_id=branch_id
                )
            if not can_resume:
                args = [
                    self.settings.codex_command,
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    *(["-s", self.settings.codex_sandbox] if self.settings.codex_sandbox else []),
                    "-C",
                    str(Path(cwd).resolve()),
                    "-",
                ]
            else:
                args = [
                    self.settings.codex_command,
                    # ``-C`` and ``--skip-git-repo-check`` are root ``codex``
                    # options.  They must precede ``exec`` for the nested
                    # ``exec resume`` parser; putting ``-C`` after ``resume``
                    # is rejected by current Codex versions.
                    "-C",
                    str(Path(cwd).resolve()),
                    "exec",
                    "resume",
                    "--json",
                    "--skip-git-repo-check",
                    thread_id,
                    "-",
                ]
            args[1:1] = self._global_cli_args()
            process = await asyncio.create_subprocess_exec(
                *args,
                env=self.launch.environment() if self.launch else None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.settings.codex_event_line_limit_bytes,
            )
            stderr_task: asyncio.Task[None] | None = None
            stderr_lines: list[str] = []
            cli_errors: list[str] = []
            try:
                assert process.stdin and process.stdout and process.stderr
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
                stderr_task = asyncio.create_task(
                    self._read_stderr(
                        process.stderr,
                        branch_id=branch_id,
                        turn_id=turn_id,
                        errors=stderr_lines,
                    )
                )
                resolved_thread_id = thread_id
                await self.event_handler(
                    "turn/started",
                    {
                        "_researchOpsBranchId": branch_id,
                        "threadId": resolved_thread_id,
                        "turn": {"id": turn_id, "status": "inProgress"},
                    },
                )
                while line := await process.stdout.readline():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "thread.started":
                        resolved_thread_id = event["thread_id"]
                    elif event.get("type") == "error":
                        cli_errors.append(str(event.get("message") or "Codex error")[:2000])
                    await self._forward_event(
                        event,
                        branch_id=branch_id,
                        thread_id=resolved_thread_id,
                        turn_id=turn_id,
                    )
                return_code = await process.wait()
                await stderr_task
                if return_code != 0:
                    details = [*cli_errors[-3:], *stderr_lines[-3:]]
                    reason = " | ".join(dict.fromkeys(details))
                    if reason:
                        raise CodexUnavailable(
                            f"codex exec exited with status {return_code}: {reason}"
                        )
                    raise CodexUnavailable(f"codex exec exited with status {return_code}")
                return {
                    "thread": {"id": resolved_thread_id},
                    "turn": {"id": turn_id},
                }
            except CodexUnavailable:
                raise
            except Exception as exc:
                raise CodexUnavailable(f"Codex CLI turn failed: {exc}") from exc
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                if stderr_task and not stderr_task.done():
                    await stderr_task

    def _global_cli_args(self) -> list[str]:
        if self.launch:
            return self.launch.args()
        args: list[str] = []
        if self.settings.codex_model_provider:
            args.extend(
                [
                    "-c",
                    f"model_provider={json.dumps(self.settings.codex_model_provider)}",
                ]
            )
        if self.settings.codex_model:
            args.extend(["-m", self.settings.codex_model])
        if self.settings.codex_reasoning_effort:
            args.extend(
                [
                    "-c",
                    f"model_reasoning_effort={json.dumps(self.settings.codex_reasoning_effort)}",
                ]
            )
        return args

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        await self.lifecycle.respond(request_id, result)

    async def _forward_event(
        self,
        event: dict[str, Any],
        *,
        branch_id: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        common = {
            "_researchOpsBranchId": branch_id,
            "threadId": thread_id,
            "turnId": turn_id,
        }
        event_type = event.get("type")
        if event_type == "item.completed":
            item = _normalize_item(event.get("item") or {})
            await self.event_handler("item/completed", {**common, "item": item})
        elif event_type == "thread.started":
            await self.event_handler(
                "thread/started",
                {**common, "thread": {"id": thread_id}},
            )
        elif event_type == "item.started":
            item = _normalize_item(event.get("item") or {})
            await self.event_handler("item/started", {**common, "item": item})
        elif event_type == "error":
            message = str(event.get("message", "Codex error"))
            method = "codex/transport" if message.startswith("Reconnecting") else "error"
            await self.event_handler(method, {**common, "error": {"message": message}})
        elif event_type == "turn.completed":
            await self.event_handler(
                "turn/completed",
                {
                    **common,
                    "turn": {
                        "id": turn_id,
                        "status": "completed",
                        "usage": event.get("usage", {}),
                    },
                },
            )
        elif event_type == "turn.failed":
            await self.event_handler(
                "turn/completed",
                {**common, "turn": {"id": turn_id, "status": "failed"}},
            )

    async def _read_stderr(
        self,
        stream: asyncio.StreamReader,
        *,
        branch_id: str,
        turn_id: str,
        errors: list[str],
    ) -> None:
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace").strip()
            if not text or text.startswith("Reading additional input"):
                continue
            errors.append(text[:2000])
            method = "codex/transport" if "rmcp::transport" in text else "error"
            await self.event_handler(
                method,
                {
                    "_researchOpsBranchId": branch_id,
                    "turnId": turn_id,
                    "error": {"message": text[:4000]},
                },
            )


def _prompt(content: str, context: dict[str, Any] | None) -> str:
    """Compatibility boundary: platform metadata must not alter a user's prompt."""
    return content


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type")
    mapping = {
        "agent_message": "agentMessage",
        "command_execution": "commandExecution",
        "file_change": "fileChange",
    }
    normalized = dict(item)
    normalized["type"] = mapping.get(item_type, item_type)
    if "aggregated_output" in normalized:
        normalized["aggregatedOutput"] = normalized.pop("aggregated_output")
    if "exit_code" in normalized:
        normalized["exitCode"] = normalized.pop("exit_code")
    return normalized
