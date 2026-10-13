from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.codex.base import CodexAdapter
from research_ops.db import run_db
from research_ops.events import EventHub

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=：]\s*)([^\s,;，；]+)"),
    re.compile(
        r"(?i)((?:api[_-]?key|app[_-]?key|app[_-]?secret|access[_-]?key|"
        r"token|password|secret)\s*[:=：]\s*)([^\s,;，；]+)"
    ),
    re.compile(r"(?i)\b(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{12,}\b"),
]


class CodexEventService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        event_hub: EventHub,
        *,
        notification_recipient: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.event_hub = event_hub
        self.notification_recipient = str(notification_recipient or "").strip() or None
        self.adapter: CodexAdapter | None = None
        self.claude_adapter: CodexAdapter | None = None
        self.goal_observer: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None
        self.claude_wakeup_observer: (
            Callable[[str, str, dict[str, Any]], Awaitable[None]] | None
        ) = None

    async def handle_event(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        branch_id = params.get("_researchOpsBranchId")
        if not branch_id and thread_id:
            branch_id = await run_db(self._branch_id, thread_id)
        if method == "claude/wakeup/scheduled" and branch_id and self.claude_wakeup_observer:
            await self.claude_wakeup_observer(branch_id, method, params)
            return
        if method == "claude/approvals/expired" and branch_id:
            await run_db(self._expire_claude_approvals, branch_id, params.get("turnId"))
            await self.event_hub.publish(branch_id, {"type": "approval.expired"})
            return
        if self.goal_observer and branch_id and method in {
            "thread/goal/updated", "thread/goal/cleared", "turn/started", "turn/completed",
            "item/started", "item/completed", "error",
        }:
            try:
                await self.goal_observer(branch_id, method, params)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Native Goal observation failed for %s", branch_id
                )
        if method == "item/agentMessage/delta" and branch_id:
            await self.event_hub.publish(
                branch_id,
                {
                    "type": "assistant.delta",
                    "turn_id": params.get("turnId"),
                    "item_id": params.get("itemId"),
                    "delta": redact(str(params.get("delta", ""))),
                },
            )
            return
        if (
            method in {"item/commandExecution/outputDelta", "command/exec/outputDelta"}
            and branch_id
        ):
            await self.event_hub.publish(
                branch_id,
                {
                    "type": "command.delta",
                    "turn_id": params.get("turnId"),
                    "item_id": params.get("itemId"),
                    "delta": redact(str(params.get("delta", "")))[:4000],
                },
            )
            return
        if method == "item/started" and branch_id:
            await self._item_started(branch_id, params)
            return
        if method == "item/completed" and branch_id:
            await self._item_completed(branch_id, params)
            return
        if method == "turn/started" and branch_id:
            await self._turn_started(branch_id, params)
            return
        if method == "thread/started" and branch_id:
            await self._thread_started(branch_id, params)
            return
        if method == "turn/completed" and branch_id:
            await self._turn_completed(branch_id, params)
            if params.get("runtime") == "claude" and self.claude_wakeup_observer:
                await self.claude_wakeup_observer(branch_id, method, params)
            return
        if method == "thread/goal/updated" and branch_id:
            goal = params.get("goal") or {}
            await run_db(self._save_activity, branch_id, "codex.goal_updated", {
                "thread_id": params.get("threadId"),
                "turn_id": params.get("turnId"),
                "status": goal.get("status"),
                "token_budget": goal.get("tokenBudget"),
                "tokens_used": goal.get("tokensUsed"),
                "time_used_seconds": goal.get("timeUsedSeconds"),
            })
            await self.event_hub.publish(
                branch_id,
                {"type": "native_goal.updated"},
            )
            return
        if method == "thread/goal/cleared" and branch_id:
            await run_db(self._save_activity, branch_id, "codex.goal_cleared", {
                "thread_id": params.get("threadId"),
            })
            await self.event_hub.publish(
                branch_id,
                {"type": "native_goal.cleared"},
            )
            return
        if method == "error" and branch_id:
            runtime = str(params.get("runtime") or "codex")
            await run_db(self._save_activity, branch_id, f"{runtime}.error", sanitize(params))
            await self.event_hub.publish(
                branch_id, {"type": f"{runtime}.error", "error": sanitize(params)}
            )
            return
        if branch_id:
            await self.event_hub.publish(
                branch_id,
                {"type": "codex.event", "method": method, "params": sanitize(params)},
            )

    def _save_activity(self, branch_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.session_factory() as session:
            session.add(models.ActivityEvent(
                scope_type="branch",
                scope_id=branch_id,
                event_type=event_type,
                actor="codex",
                payload=payload,
            ))
            session.commit()

    async def handle_request(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        if method == "currentTime/read" and self.adapter:
            await self.adapter.respond(request_id, {"currentTimeAt": int(time.time())})
            return
        thread_id = params.get("threadId")
        branch_id = params.get("_researchOpsBranchId") or (
            self._branch_id(thread_id) if thread_id else None
        )
        with self.session_factory() as session:
            approval = models.PendingApproval(
                branch_id=branch_id,
                thread_id=thread_id,
                rpc_request_id=request_id,
                method=method,
                params=sanitize(params),
            )
            session.add(approval)
            session.commit()
            payload = schemas.PendingApprovalRead.model_validate(approval).model_dump(mode="json")
        if branch_id:
            await self.event_hub.publish(
                branch_id,
                {"type": "approval.requested", "approval": payload},
            )

    async def resolve_approval(
        self, approval_id: str, payload: schemas.ApprovalResolve
    ) -> models.PendingApproval:
        with self.session_factory() as session:
            approval = session.get(models.PendingApproval, approval_id)
            if approval is None:
                raise LookupError("Approval not found")
            if approval.status != "pending":
                return approval
            adapter = (
                self.claude_adapter if approval.method == "claude/tool/requestApproval"
                else self.adapter
            )
            if adapter is None:
                raise RuntimeError("The approval runtime is unavailable")
            if approval.method == "claude/tool/requestApproval":
                if payload.response is not None or payload.decision not in {
                    "accept", "decline", "cancel"
                }:
                    raise ValueError("Choose Allow once or Deny for this Claude operation")
                response = {"decision": payload.decision}
            elif approval.method == "item/tool/requestUserInput":
                if payload.decision is not None or not isinstance(payload.response, dict):
                    raise ValueError("Answer every Codex question before continuing")
                answers = payload.response.get("answers")
                questions = approval.params.get("questions")
                if (
                    not isinstance(answers, dict)
                    or not isinstance(questions, list)
                    or not questions
                ):
                    raise ValueError("Answer every Codex question before continuing")
                question_ids = {q.get("id") for q in questions if isinstance(q, dict)}
                if (len(question_ids) != len(questions) or set(answers) != question_ids
                    or any(
                        not isinstance(value, dict)
                        or not isinstance(value.get("answers"), list)
                        or not value["answers"]
                        or any(not isinstance(answer, str) or not answer.strip()
                               for answer in value["answers"])
                        for value in answers.values()
                    )):
                    raise ValueError("Answer every Codex question before continuing")
                response = {"answers": answers}
            elif payload.response is not None:
                response = payload.response
            elif approval.method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "execCommandApproval",
                "applyPatchApproval",
            }:
                response = {"decision": payload.decision or "decline"}
            else:
                raise ValueError("This request needs a structured response")
            request_id = approval.rpc_request_id
        await adapter.respond(request_id, response)
        with self.session_factory() as session:
            approval = session.get(models.PendingApproval, approval_id)
            assert approval
            approval.status = "resolved"
            # Questions may ask for credentials. Never persist their answers or
            # broadcast them on the Branch event stream.
            approval.response = (
                {"answered": True} if approval.method == "item/tool/requestUserInput"
                else sanitize(response)
            )
            approval.resolved_at = datetime.now(UTC)
            session.commit()
            branch_id = approval.branch_id
        if branch_id:
            await self.event_hub.publish(
                branch_id,
                {
                    "type": "approval.resolved",
                    "approval_id": approval_id,
                    "response": (
                        {"answered": True} if approval.method == "item/tool/requestUserInput"
                        else sanitize(response)
                    ),
                },
            )
        return approval

    def _expire_claude_approvals(self, branch_id: str, turn_id: str | None) -> None:
        with self.session_factory() as session:
            for approval in session.scalars(select(models.PendingApproval).where(
                models.PendingApproval.branch_id == branch_id,
                models.PendingApproval.method == "claude/tool/requestApproval",
                models.PendingApproval.status == "pending",
            )):
                if approval.params.get("turnId") == turn_id:
                    approval.status = "expired"
                    approval.resolved_at = datetime.now(UTC)
            session.commit()

    def expire_detached_claude_approvals(self) -> None:
        """No interactive RPC callback survives a server process restart."""
        with self.session_factory() as session:
            for approval in session.scalars(select(models.PendingApproval).where(
                models.PendingApproval.method.in_((
                    "claude/tool/requestApproval", "item/tool/requestUserInput"
                )),
                models.PendingApproval.status == "pending",
            )):
                approval.status = "expired"
                approval.resolved_at = datetime.now(UTC)
            session.commit()

    async def _item_completed(self, branch_id: str, params: dict[str, Any]) -> None:
        serialized = await run_db(self._save_completed_item, branch_id, params)
        if serialized is not None:
            await self.event_hub.publish(
                branch_id, {"type": "message.created", "message": serialized}
            )
        elif (params.get("item") or {}).get("type") not in {
            "agentMessage", "commandExecution", "fileChange"
        }:
            await self.event_hub.publish(
                branch_id, {"type": "item.completed", "item": sanitize(params.get("item") or {})}
            )

    def _save_completed_item(
        self, branch_id: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        item = params.get("item") or {}
        runtime = str(params.get("runtime") or "codex")
        item_type = item.get("type")
        if item_type == "agentMessage":
            role = "assistant"
            kind = "message"
            content = redact(str(item.get("text", "")))
        elif item_type == "commandExecution":
            role = "tool"
            kind = "command"
            command = redact(str(item.get("command", "")))
            status = item.get("status", "unknown")
            exit_code = item.get("exitCode")
            content = f"$ {command}\nstatus={status} exit_code={exit_code}"
        elif item_type == "fileChange":
            role = "tool"
            kind = "file_change"
            content = f"File change {item.get('status', 'unknown')}"
        else:
            return None
        if not content.strip():
            return None
        with self.session_factory() as session:
            attempt = session.scalar(
                select(models.SessionAttempt)
                .where(models.SessionAttempt.branch_id == branch_id)
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            message = models.ConversationMessage(
                branch_id=branch_id,
                session_id=attempt.id if attempt else None,
                turn_id=params.get("turnId"),
                external_item_id=item.get("id"),
                role=role,
                kind=kind,
                content=content,
                metadata_json={
                    "runtime": runtime,
                    "status": item.get("status"),
                    "duration_ms": item.get("durationMs"),
                    "exit_code": item.get("exitCode"),
                },
            )
            session.add(message)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            serialized = schemas.ConversationMessageRead.model_validate(message).model_dump(
                mode="json"
            )
        return serialized

    async def _item_started(self, branch_id: str, params: dict[str, Any]) -> None:
        serialized = await run_db(self._save_started_item, branch_id, params)
        if serialized is not None:
            await self.event_hub.publish(
                branch_id, {"type": "message.updated", "message": serialized}
            )
        await self.event_hub.publish(
            branch_id,
            {
                "type": "item.started",
                "turn_id": str(params.get("turnId") or "").strip() or None,
                "item": sanitize(params.get("item", {})),
            },
        )

    def _save_started_item(
        self, branch_id: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Record an irreversible-execution boundary for safe transport retry.

        An item may start before it has a completed event to store. Mark the
        pending Web query at that point so a gateway reconnect can never replay
        a command that might already have reached an external system.
        """
        turn_id = str(params.get("turnId") or "").strip()
        item = sanitize(params.get("item", {}))
        item_type = str(item.get("type") or "").strip()
        serialized: dict[str, Any] | None = None
        # A model message is safe to re-request after a transport loss. Tool
        # execution and file changes are not: either may have crossed an
        # external boundary before the connection disappears.
        if turn_id and item_type in {"commandExecution", "fileChange"}:
            with self.session_factory() as session:
                message = session.scalar(
                    select(models.ConversationMessage)
                    .where(
                        models.ConversationMessage.branch_id == branch_id,
                        models.ConversationMessage.role == "user",
                        models.ConversationMessage.turn_id == turn_id,
                    )
                    .order_by(models.ConversationMessage.created_at.desc())
                    .limit(1)
                )
                if (
                    message is not None
                    and message.metadata_json.get("origin") in {"web", "continuation"}
                    and message.metadata_json.get("send_status") == "pending"
                    and not message.metadata_json.get("runtime_execution_started")
                ):
                    message.metadata_json = {
                        **message.metadata_json,
                        "runtime_execution_started": {
                            "at": datetime.now(UTC).isoformat(),
                            "item_type": item_type,
                        },
                    }
                    session.commit()
                    serialized = schemas.ConversationMessageRead.model_validate(
                        message
                    ).model_dump(mode="json")
        return serialized

    async def _turn_started(self, branch_id: str, params: dict[str, Any]) -> None:
        serialized = await run_db(self._save_started_turn, branch_id, params)
        if serialized is not None:
            await self.event_hub.publish(
                branch_id, {"type": "message.updated", "message": serialized}
            )
        await self.event_hub.publish(
            branch_id,
            {"type": "turn.started", "turn": sanitize(params.get("turn") or {})},
        )

    def _save_started_turn(
        self, branch_id: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id") or params.get("turnId") or "").strip()
        serialized: dict[str, Any] | None = None
        if turn_id:
            with self.session_factory() as session:
                recent_users = list(
                    session.scalars(
                        select(models.ConversationMessage)
                        .where(
                            models.ConversationMessage.branch_id == branch_id,
                            models.ConversationMessage.role == "user",
                        )
                        .order_by(models.ConversationMessage.created_at.desc())
                        .limit(20)
                    )
                )
                pending = next(
                    (
                        message
                        for message in recent_users
                        if message.metadata_json.get("origin") in {"web", "continuation"}
                        and message.metadata_json.get("send_status") == "pending"
                    ),
                    None,
                )
                if pending is not None and pending.turn_id != turn_id:
                    # A safe transport retry invokes a fresh Codex turn for
                    # the same pending query. Keep the query bound to the
                    # current attempt so subsequent item events can mark its
                    # execution boundary correctly.
                    pending.turn_id = turn_id
                    # A previous transport attempt can have started a turn
                    # but emitted no items. Its execution marker must not
                    # suppress the explicitly approved safe retry.
                    if pending.metadata_json.get("runtime_execution_started"):
                        pending.metadata_json = {
                            **pending.metadata_json,
                            "runtime_execution_started": None,
                        }
                    session.commit()
                    serialized = schemas.ConversationMessageRead.model_validate(
                        pending
                    ).model_dump(mode="json")
        return serialized

    async def _thread_started(self, branch_id: str, params: dict[str, Any]) -> None:
        published = await run_db(self._save_started_thread, branch_id, params)
        if published is not None:
            await self.event_hub.publish(branch_id, {"type": "thread.bound", **published})

    def _save_started_thread(
        self, branch_id: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Bind a newly materialized CLI thread before its first turn ends.

        Fresh CLI execution starts with a local ``deferred:`` placeholder.
        Persisting the real id as soon as the JSONL ``thread.started`` event
        arrives keeps live activity, result sync, and a future restart attached
        to the same Branch. An existing non-deferred thread is never replaced
        by a late event from an older process.
        """
        thread = params.get("thread") or {}
        thread_id = str(thread.get("id") or params.get("threadId") or "").strip()
        if not thread_id or thread_id.startswith("deferred:"):
            return
        published: dict[str, Any] | None = None
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                return
            previous_thread_id = str(branch.codex_thread_id or "").strip()
            if previous_thread_id and not previous_thread_id.startswith("deferred:"):
                if previous_thread_id != thread_id:
                    return
                # The active binding already exists or will be reconciled by
                # the normal result synchronizer; nothing needs replacing.
                return
            pending = session.scalar(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch_id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            if (
                pending is None
                or pending.metadata_json.get("origin") not in {"web", "continuation"}
                or pending.metadata_json.get("send_status") != "pending"
                or pending.session_id is None
            ):
                return
            attempt = session.get(models.SessionAttempt, pending.session_id)
            if attempt is None or attempt.branch_id != branch_id:
                return
            existing = session.scalar(
                select(models.CodexThreadBinding).where(
                    models.CodexThreadBinding.thread_id == thread_id
                )
            )
            if existing is not None and existing.branch_id != branch_id:
                session.add(
                    models.ActivityEvent(
                        scope_type="branch",
                        scope_id=branch_id,
                        event_type="codex.thread_binding_conflict",
                        actor="codex",
                        payload={"thread_id": thread_id},
                    )
                )
                session.commit()
                return
            current_bindings = list(
                session.scalars(
                    select(models.CodexThreadBinding).where(
                        models.CodexThreadBinding.branch_id == branch_id,
                        models.CodexThreadBinding.status == "current",
                    )
                )
            )
            for binding in current_bindings:
                if binding.thread_id != thread_id:
                    binding.status = "historical"
                    binding.retired_at = datetime.now(UTC)
            if existing is None:
                existing = models.CodexThreadBinding(
                    branch_id=branch_id,
                    initial_session_id=attempt.id,
                    thread_id=thread_id,
                    source="web",
                    bound_at=attempt.started_at or datetime.now(UTC),
                )
                session.add(existing)
            else:
                existing.status = "current"
                existing.retired_at = None
            branch.codex_thread_id = thread_id
            attempt.codex_thread_id = thread_id
            session.add(
                models.ActivityEvent(
                    scope_type="branch",
                    scope_id=branch_id,
                    event_type="codex.thread_bound",
                    actor="codex",
                    payload={
                        "thread_id": thread_id,
                        "replaced_thread_id": previous_thread_id or None,
                        "session_id": attempt.id,
                    },
                )
            )
            session.commit()
            published = {
                "thread_id": thread_id,
                "session_id": attempt.id,
                "replaced_thread_id": previous_thread_id or None,
            }
        return published

    async def _turn_completed(self, branch_id: str, params: dict[str, Any]) -> None:
        await run_db(self._save_completed_turn, branch_id, params)
        await self.event_hub.publish(
            branch_id,
            {
                "type": "turn.completed",
                "turn": sanitize(params.get("turn") or {}),
                "runtime": str(params.get("runtime") or "codex"),
            },
        )

    def _save_completed_turn(self, branch_id: str, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        runtime = str(params.get("runtime") or "codex")
        with self.session_factory() as session:
            attempt = session.scalar(
                select(models.SessionAttempt)
                .where(models.SessionAttempt.branch_id == branch_id)
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            if attempt:
                attempt.last_turn_id = turn.get("id") or attempt.last_turn_id
                attempt.heartbeat_at = datetime.now(UTC)
            session.add(
                models.ActivityEvent(
                    scope_type="branch",
                    scope_id=branch_id,
                    event_type=f"{runtime}.turn_completed",
                    actor=runtime,
                    payload={
                        "turn_id": turn.get("id"),
                        "status": sanitize(turn.get("status")),
                        "runtime": runtime,
                    },
                )
            )
            if runtime != "codex":
                self._persist_external_runtime_turn(session, branch_id, params, turn, runtime)
            session.commit()

    def _persist_external_runtime_turn(
        self,
        session: Session,
        branch_id: str,
        params: dict[str, Any],
        turn: dict[str, Any],
        runtime: str,
    ) -> None:
        """Persist runs for runtimes that have no Codex rollout synchronizer."""
        thread_id = str(params.get("threadId") or "").strip()
        external_turn_id = str(turn.get("id") or params.get("turnId") or "").strip()
        if not thread_id or not external_turn_id:
            return
        existing = session.scalar(
            select(models.CodexTurn).where(
                models.CodexTurn.external_turn_id == external_turn_id
            )
        )
        if existing is not None:
            return
        attempt = session.scalar(
            select(models.SessionAttempt)
            .where(models.SessionAttempt.branch_id == branch_id)
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )
        binding = session.scalar(
            select(models.CodexThreadBinding).where(
                models.CodexThreadBinding.thread_id == thread_id
            )
        )
        now = datetime.now(UTC)
        if binding is None:
            binding = models.CodexThreadBinding(
                branch_id=branch_id,
                initial_session_id=attempt.id if attempt else None,
                thread_id=thread_id,
                source=runtime,
                bound_at=now,
            )
            session.add(binding)
            session.flush()
        user_message = session.scalar(
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == branch_id,
                models.ConversationMessage.turn_id == external_turn_id,
                models.ConversationMessage.role == "user",
            )
            .order_by(models.ConversationMessage.created_at.asc())
            .limit(1)
        )
        assistant_message = session.scalar(
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == branch_id,
                models.ConversationMessage.turn_id == external_turn_id,
                models.ConversationMessage.role == "assistant",
            )
            .order_by(models.ConversationMessage.created_at.desc())
            .limit(1)
        )
        query_metadata = user_message.metadata_json if user_message else {}
        stored_turn = models.CodexTurn(
            branch_id=branch_id,
            session_id=attempt.id if attempt else None,
            thread_binding_id=binding.id,
            thread_id=thread_id,
            external_turn_id=external_turn_id,
            status=str(turn.get("status") or "completed"),
            prompt=user_message.content if user_message else None,
            final_answer=assistant_message.content if assistant_message else "",
            started_at=user_message.created_at if user_message else now,
            completed_at=now,
            metadata_json={
                "runtime": runtime,
                "usage": sanitize(turn.get("usage") or {}),
                **({"execution": sanitize(query_metadata["execution"])}
                   if query_metadata.get("execution") else {}),
                **{key: query_metadata[key] for key in (
                    "scheduled_check_id", "scheduled_check_run_id",
                    "claude_wakeup_id", "prompt_kind",
                ) if key in query_metadata},
            },
        )
        try:
            with session.begin_nested():
                session.add(stored_turn)
                session.flush()
        except IntegrityError:
            return
        # External runtimes bypass the Codex JSONL importer and its notification outbox.
        if (
            self.notification_recipient
            and stored_turn.status == "completed"
            and stored_turn.final_answer.strip()
            and not query_metadata.get("scheduled_check_id")
        ):
            session.add(models.TurnNotification(
                turn_id=stored_turn.id,
                channel="infoflow",
                recipient=self.notification_recipient,
            ))

    def _branch_id(self, thread_id: str | None) -> str | None:
        if not thread_id:
            return None
        with self.session_factory() as session:
            return session.scalar(
                select(models.ExecutionBranch.id).where(
                    models.ExecutionBranch.codex_thread_id == thread_id
                )
            )


def redact(value: str) -> str:
    text = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    return value
