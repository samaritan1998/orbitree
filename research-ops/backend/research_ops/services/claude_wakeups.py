from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from research_ops import models, schemas
from research_ops.codex.claude_execution import claude_execution_snapshot
from research_ops.db import run_db

from .codex_events import redact
from .core import ConflictError, NotFoundError

PENDING = ("awaiting_turn", "scheduled", "claimed")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ClaudeWakeupService:
    """One-shot continuations observed live, never reconstructed from historical JSONL."""

    def __init__(self, factory, settings):
        self.factory = factory
        self.settings = settings

    def observe(self, branch_id: str, method: str, params: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self.factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if (
                not branch
                or branch.runtime != "claude"
                or branch.codex_thread_id != params.get("threadId")
            ):
                return
            if method == "claude/wakeup/scheduled":
                tool_id = str(params.get("toolId") or "")
                payload = params.get("input") or {}
                delay, prompt = payload.get("delaySeconds"), payload.get("prompt")
                if not tool_id or not params.get("turnId"):
                    return
                if session.scalar(
                    select(models.ClaudeWakeup.id).where(models.ClaudeWakeup.tool_id == tool_id)
                ):
                    return
                valid = (
                    isinstance(delay, int)
                    and not isinstance(delay, bool)
                    and 1 <= delay <= 604800
                    and isinstance(prompt, str)
                    and 0 < len(prompt.strip()) <= 32000
                    and redact(prompt) == prompt
                )
                session.execute(
                    update(models.ClaudeWakeup)
                    .where(
                        models.ClaudeWakeup.branch_id == branch_id,
                        models.ClaudeWakeup.status.in_(PENDING),
                    )
                    .values(
                        status="cancelled", reason="Replaced by a newer wakeup", completed_at=now
                    )
                )
                message = session.scalar(
                    select(models.ConversationMessage)
                    .where(
                        models.ConversationMessage.branch_id == branch_id,
                        models.ConversationMessage.turn_id == params["turnId"],
                        models.ConversationMessage.role == "user",
                    )
                    .order_by(models.ConversationMessage.created_at.desc())
                    .limit(1)
                )
                execution = (message.metadata_json.get("execution") if message else None) or (
                    claude_execution_snapshot(self.settings, branch.execution_preferences)
                )
                enabled = valid and self.settings.scheduled_checks_enabled
                due_at = now + timedelta(seconds=delay if valid else 0)
                session.add(
                    models.ClaudeWakeup(
                        branch_id=branch_id,
                        thread_id=branch.codex_thread_id,
                        source_turn_id=params["turnId"],
                        tool_id=tool_id,
                        status="awaiting_turn" if enabled else "cancelled",
                        prompt=prompt if valid else "",
                        execution=execution,
                        due_at=due_at,
                        next_attempt_at=due_at,
                        reason=None
                        if enabled
                        else (
                            "Invalid wakeup prompt or delay"
                            if not valid
                            else "Scheduler is disabled"
                        ),
                    )
                )
            elif method == "turn/completed":
                succeeded = (params.get("turn") or {}).get("status") == "completed"
                session.execute(
                    update(models.ClaudeWakeup)
                    .where(
                        models.ClaudeWakeup.branch_id == branch_id,
                        models.ClaudeWakeup.source_turn_id == params.get("turnId"),
                        models.ClaudeWakeup.status == "awaiting_turn",
                    )
                    .values(
                        status="scheduled" if succeeded else "cancelled",
                        reason=None if succeeded else "Source turn failed or was interrupted",
                    )
                )
            session.commit()

    def detail(self, branch_id: str) -> list[dict[str, Any]]:
        with self.factory() as session:
            return [
                {
                    "id": row.id,
                    "status": row.status,
                    "prompt": row.prompt,
                    "due_at": utc(row.due_at).isoformat(),
                    "reason": row.reason,
                    "source_turn_id": row.source_turn_id,
                    "message_id": row.message_id,
                }
                for row in session.scalars(
                    select(models.ClaudeWakeup)
                    .where(
                        models.ClaudeWakeup.branch_id == branch_id,
                    )
                    .order_by(models.ClaudeWakeup.created_at.desc())
                    .limit(10)
                )
            ]

    def cancel(self, branch_id: str, wakeup_id: str) -> None:
        with self.factory() as session:
            row = session.get(models.ClaudeWakeup, wakeup_id)
            if not row or row.branch_id != branch_id:
                raise NotFoundError("Wakeup not found")
            if row.status == "running":
                raise ConflictError(
                    "Wakeup already started; interrupt its conversation turn instead"
                )
            if row.status in PENDING:
                row.status, row.reason = "cancelled", "Cancelled by user"
                row.completed_at = datetime.now(UTC)
                session.commit()

    def reconcile_and_claim(self) -> tuple[list[str], list[str]]:
        now = datetime.now(UTC)
        claimed, changed = [], set()
        with self.factory() as session:
            rows = list(
                session.scalars(
                    select(models.ClaudeWakeup)
                    .where(
                        models.ClaudeWakeup.status.in_((*PENDING, "running")),
                    )
                    .order_by(models.ClaudeWakeup.next_attempt_at)
                    .limit(200)
                )
            )
            for row in rows:
                before = (row.status, row.reason)
                branch = session.get(models.ExecutionBranch, row.branch_id)
                if row.status != "running" and (
                    not branch
                    or branch.hidden
                    or branch.status != "active"
                    or branch.mode == "capture"
                    or branch.runtime != "claude"
                    or branch.codex_thread_id != row.thread_id
                ):
                    row.status, row.reason = (
                        "cancelled",
                        "The original Claude Branch is no longer available",
                    )
                if row.status == "awaiting_turn":
                    turn = session.scalar(
                        select(models.CodexTurn)
                        .where(
                            models.CodexTurn.branch_id == row.branch_id,
                            models.CodexTurn.external_turn_id == row.source_turn_id,
                        )
                        .limit(1)
                    )
                    if turn:
                        row.status = "scheduled" if turn.status == "completed" else "cancelled"
                        row.reason = (
                            None if turn.status == "completed" else "Source turn did not complete"
                        )
                    elif now - utc(row.due_at) > timedelta(days=1):
                        row.status, row.reason = "missed", "Source turn completion was not recorded"
                if (
                    row.status == "claimed"
                    and row.claimed_at
                    and now - utc(row.claimed_at) > timedelta(minutes=5)
                ):
                    # Accepted messages are linked atomically before dispatch. Only an
                    # unaccepted claim may safely return to the queue after a crash.
                    row.status, row.reason = "scheduled", "Recovered an unaccepted dispatch"
                if row.status == "running":
                    message = (
                        session.get(models.ConversationMessage, row.message_id)
                        if row.message_id
                        else None
                    )
                    status = message.metadata_json.get("send_status") if message else None
                    if status in {"submitted", "failed", "interrupted"}:
                        row.status = "completed" if status == "submitted" else "failed"
                        row.reason = (
                            None
                            if status == "submitted"
                            else str(message.metadata_json.get("error") or status)[:2000]
                        )
                        row.completed_at = now
                    elif not message:
                        row.status, row.reason = (
                            "failed",
                            "Accepted wakeup message is missing; not replayed",
                        )
                if row.status == "scheduled" and now - utc(row.due_at) > timedelta(days=1):
                    row.status, row.reason = "missed", "Wakeup is over 24 hours late; not replayed"
                session.flush()
                if row.status == "scheduled" and utc(row.next_attempt_at) <= now:
                    result = session.execute(
                        update(models.ClaudeWakeup)
                        .where(
                            models.ClaudeWakeup.id == row.id,
                            models.ClaudeWakeup.status == "scheduled",
                        )
                        .values(status="claimed", claimed_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount == 1:
                        claimed.append(row.id)
                        changed.add(row.branch_id)
                if before != (row.status, row.reason):
                    changed.add(row.branch_id)
            session.commit()
        return claimed, sorted(changed)

    def get(self, wakeup_id: str) -> models.ClaudeWakeup | None:
        with self.factory() as session:
            return session.get(models.ClaudeWakeup, wakeup_id)

    def dispatch_failed(self, wakeup_id: str, reason: str, *, busy: bool = False) -> None:
        with self.factory() as session:
            row = session.get(models.ClaudeWakeup, wakeup_id)
            if row and row.status == "claimed" and not row.message_id:
                row.status = "scheduled" if busy else "failed"
                row.reason = redact(reason)[:2000]
                row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=30)
                session.commit()


class ClaudeWakeupWorker:
    def __init__(self, wakeups, branches, events):
        self.wakeups, self.branches, self.events = wakeups, branches, events
        self.tasks: dict[str, asyncio.Task] = {}

    async def observe(self, branch_id, method, params):
        await run_db(self.wakeups.observe, branch_id, method, params)
        await self.publish(branch_id)

    async def tick(self):
        claimed, changed = await run_db(self.wakeups.reconcile_and_claim)
        for branch_id in changed:
            await self.publish(branch_id)
        for wakeup_id in claimed:
            task = asyncio.create_task(self._start(wakeup_id), name=f"claude-wakeup:{wakeup_id}")
            self.tasks[wakeup_id] = task
            task.add_done_callback(lambda done, key=wakeup_id: self._done(key, done))

    def _done(self, wakeup_id, task):
        self.tasks.pop(wakeup_id, None)
        if not task.cancelled() and (error := task.exception()):
            logging.getLogger(__name__).error(
                "Claude wakeup dispatch failed: %s", redact(str(error))[:2000]
            )

    async def _start(self, wakeup_id):
        row = await run_db(self.wakeups.get, wakeup_id)
        if not row or row.status != "claimed":
            return
        try:
            await self.branches.queue_web_message(
                row.branch_id,
                schemas.ConversationSend(content=row.prompt),
                metadata={"claude_wakeup_id": row.id, "execution": row.execution},
                supersede_pending=False,
            )
        except ConflictError as exc:
            await run_db(self.wakeups.dispatch_failed, row.id, str(exc), busy=True)
        except Exception as exc:
            await run_db(self.wakeups.dispatch_failed, row.id, str(exc))
        finally:
            await self.publish(row.branch_id)

    async def publish(self, branch_id):
        await self.events.publish(branch_id, {"type": "scheduled_check.updated"})

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
