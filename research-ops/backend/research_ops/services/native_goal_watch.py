from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.services.codex_events import redact
from research_ops.services.native_goal_history import record_goal_history
from research_ops.settings import Settings

TOOL_TYPES = {
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "collabAgentToolCall",
}
SETTLED_TOOL_STATES = {"completed", "failed", "declined"}
OBSERVATION_ERROR_PREFIX = "Codex status unavailable: "


def control_request_timeout(error: str) -> bool:
    return str(error).strip() in {
        "Codex request timed out: thread/goal/get",
        "Codex request timed out: thread/read",
        "Codex request timed out: thread/turns/list",
        "Codex request timed out: thread/resume",
    }


def observation_error(error: str | None) -> bool:
    return bool(error) and (
        str(error).startswith(OBSERVATION_ERROR_PREFIX) or control_request_timeout(str(error))
    )


def rate_limited(error: str) -> bool:
    return any(
        word in error.lower()
        for word in (
            "concurrency limit exceeded",
            "rate limit exceeded",
            "rate_limit_exceeded",
            "rate limit reached",
            "too many requests",
        )
    )


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def transient_error(error: str) -> bool:
    if observation_error(error):
        return False
    text = error.lower()
    if any(
        word in text
        for word in (
            "request blocked",
            "unauthorized",
            "forbidden",
            "authentication",
            "api key",
            "quota",
            "usage limit",
            "usagelimitexceeded",
            "budget",
            "credit",
            "401",
            "403",
            "not supported",
            "invalid_request",
            "interrupted",
        )
    ):
        return False
    if rate_limited(error):
        return True
    if "429" in text:
        return False
    return any(
        word in text
        for word in (
            "timeout",
            "timed out",
            "connection reset",
            "connection refused",
            "connection closed",
            "stream disconnected",
            "streamdisconnected",
            "failed to connect",
            "app server exited",
            "server_error",
            "internal server error",
            "internalservererror",
            "service unavailable",
            "bad gateway",
            "at capacity",
            "overloaded",
            "502",
            "503",
            "504",
        )
    )


class NativeGoalWatch:
    """Persist observations and notifications, never a second Goal state machine."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def observe_goal(
        self,
        branch_id: str,
        thread_id: str,
        goal: dict[str, Any] | None,
        *,
        notify_initial: bool = False,
        turn_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None or branch.codex_thread_id != thread_id:
                return
            if goal is None:
                if row and row.goal_snapshot and row.thread_id == thread_id:
                    self._clear_observation_error(row)
                    row.execution_status = "cleared"
                    row.next_retry_at = None
                    row.retryable_error = False
                    self._queue(session, row, "cleared", now)
                    record_goal_history(session, row, now, cleared=True)
                    row.goal_snapshot = {}
                    session.commit()
                return
            if (
                row
                and row.thread_id == thread_id
                and int(goal.get("updatedAt", 0)) < int(row.goal_snapshot.get("updatedAt", 0))
            ):
                return
            key = hashlib.sha256(
                f"{thread_id}:{goal.get('createdAt')}:{goal.get('objective')}".encode()
            ).hexdigest()
            initial = row is None or row.goal_key != key
            if initial:
                if row is None:
                    row = models.NativeGoalMonitor(branch_id=branch_id)
                    session.add(row)
                else:
                    record_goal_history(session, row, now)
                row.thread_id = thread_id
                row.goal_key = key
                row.goal_snapshot = {}
                row.execution_status = "unknown"
                row.last_activity_at = None
                row.last_summary = None
                row.last_error = None
                row.last_error_at = None
                row.turn_id = None
                row.turn_has_side_effects = False
                row.turn_tool_state = None
                row.retryable_error = False
                row.retry_attempts = 0
                row.next_retry_at = None
                row.last_progress_at = now
                row.notice_sequence = row.notice_sequence or 0
            assert row is not None
            previous = row.goal_snapshot
            if int(goal.get("updatedAt", 0)) < int(previous.get("updatedAt", 0)):
                return
            row.goal_snapshot = dict(goal)
            if turn_id and turn_id != row.turn_id:
                # Pre-sampling failures can identify a new turn on the Goal
                # event without ever emitting turn/started.
                row.turn_id = turn_id
                row.turn_has_side_effects = False
                row.turn_tool_state = {}
            if previous and (
                goal.get("tokensUsed", 0) > previous.get("tokensUsed", 0)
                or goal.get("timeUsedSeconds", 0) > previous.get("timeUsedSeconds", 0)
            ):
                row.last_activity_at = now
            status = goal.get("status")
            if status != "active":
                self._clear_observation_error(row)
                row.execution_status = str(status or "unknown")
            if status in {"paused", "complete", "usageLimited", "budgetLimited"}:
                row.next_retry_at = None
                row.retryable_error = False
            elif row.next_retry_at:
                row.execution_status = "retrying"
            if previous.get("status") != status and (not initial or notify_initial):
                self._queue(session, row, "state", now)
            record_goal_history(session, row, now)
            session.commit()

    def observe_event(self, branch_id: str, method: str, params: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if row is None or row.thread_id != params.get("threadId") or not row.goal_snapshot:
                return
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None or branch.codex_thread_id != row.thread_id:
                return
            turn = params.get("turn") or {}
            turn_id = str(params.get("turnId") or turn.get("id") or "")
            item = params.get("item") or {}
            if method == "turn/started":
                self._clear_observation_error(row)
                if row.turn_id != turn_id:
                    row.turn_id = turn_id
                    row.turn_has_side_effects = False
                    row.turn_tool_state = {}
                row.execution_status = "running"
                row.last_activity_at = now
            elif method == "item/started":
                if turn_id and row.turn_id and turn_id != row.turn_id:
                    return
                self._record_tool(row, item, completed=False)
                if row.last_error and self._unsettled_tools(row):
                    row.retryable_error = False
                    row.next_retry_at = None
                    row.execution_status = "error"
                row.last_activity_at = now
            elif method == "item/completed":
                if turn_id and row.turn_id and turn_id != row.turn_id:
                    return
                row.last_activity_at = now
                if item.get("type") == "agentMessage":
                    row.last_summary = redact(str(item.get("text") or ""))[:1_200]
                elif item.get("type") in TOOL_TYPES:
                    self._record_tool(row, item, completed=True)
                    self._schedule_retry(session, row, now, notify=True)
            elif method == "turn/completed":
                if turn_id and row.turn_id and turn_id != row.turn_id:
                    return
                row.last_activity_at = now
                if turn.get("status") == "completed":
                    row.execution_status = "idle"
                    row.retry_attempts = 0
                    row.next_retry_at = None
                    row.retryable_error = False
                    row.last_error = None
                elif turn.get("status") == "failed":
                    error = turn.get("error") or {}
                    self._failure(session, row, self._error_text(error), now)
                elif turn.get("status") == "interrupted":
                    self._failure(session, row, "interrupted", now)
            elif method == "error" and not params.get("willRetry"):
                if turn_id and row.turn_id and turn_id != row.turn_id:
                    return
                self._failure(session, row, self._error_text(params.get("error") or params), now)
            else:
                return
            if method in {"turn/completed", "error"} or (
                method == "item/completed" and item.get("type") == "agentMessage"
            ):
                record_goal_history(session, row, now)
            session.commit()

    @staticmethod
    def _unsettled_tools(row: models.NativeGoalMonitor) -> bool:
        if row.turn_tool_state is None:
            return row.turn_has_side_effects
        return any(status not in SETTLED_TOOL_STATES for status in row.turn_tool_state.values())

    @staticmethod
    def _record_tool(
        row: models.NativeGoalMonitor, item: dict[str, Any], *, completed: bool
    ) -> None:
        if item.get("type") not in TOOL_TYPES:
            return
        state = dict(row.turn_tool_state or {})
        if row.turn_tool_state is None and row.turn_has_side_effects:
            state["__legacy__"] = "unknown"
        row.turn_has_side_effects = True
        item_id = str(item.get("id") or "__unknown__")
        status = str(item.get("status") or "unknown") if completed else "inProgress"
        # Completed events are authoritative, even if a duplicate start arrives later.
        if completed or state.get(item_id) not in SETTLED_TOOL_STATES:
            state[item_id] = status
        row.turn_tool_state = state

    def recovery_candidate(
        self, branch_id: str, *, detached: bool = False
    ) -> tuple[str, str] | None:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if (
                row
                and row.turn_id
                and row.goal_snapshot.get("status") in {"active", "blocked"}
                and not row.next_retry_at
                and row.retry_attempts < self.settings.native_goal_max_recovery_attempts
            ):
                if row.last_error and transient_error(row.last_error):
                    return row.turn_id, str(row.last_error_at)
                if detached and not row.last_error and self._unsettled_tools(row):
                    return row.turn_id, "detached"
            return None

    def reconcile_tools(
        self, branch_id: str, thread_id: str, turn: dict[str, Any], *, detached: bool = False
    ) -> bool:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if (
                row is None
                or row.thread_id != thread_id
                or not row.turn_id
                or turn.get("id") != row.turn_id
                or turn.get("status")
                not in (
                    {"completed", "inProgress", "failed"}
                    if detached and row.goal_snapshot.get("status") == "active"
                    else {"failed"}
                )
                or turn.get("itemsView") != "full"
                or not isinstance(turn.get("items"), list)
                or row.goal_snapshot.get("status") not in {"active", "blocked"}
            ):
                return False
            tools = [item for item in turn["items"] if item.get("type") in TOOL_TYPES]
            if row.turn_has_side_effects and not tools:
                return False
            state = {
                str(item.get("id") or "__unknown__"): str(item.get("status") or "unknown")
                for item in tools
            }
            for item_id, status in (row.turn_tool_state or {}).items():
                if item_id not in state and item_id != "__legacy__":
                    state[item_id] = status
            row.turn_tool_state = state
            row.turn_has_side_effects = row.turn_has_side_effects or bool(tools)
            self._schedule_retry(session, row, datetime.now(UTC), notify=True)
            session.commit()
            return row.next_retry_at is not None

    def _schedule_retry(
        self,
        session: Session,
        row: models.NativeGoalMonitor,
        now: datetime,
        *,
        notify: bool = False,
    ) -> None:
        if not row.last_error or row.goal_snapshot.get("status") not in {"active", "blocked"}:
            return
        row.retryable_error = transient_error(row.last_error) and not self._unsettled_tools(row)
        if not row.retryable_error:
            row.next_retry_at = None
            return
        if row.retry_attempts >= self.settings.native_goal_max_recovery_attempts:
            row.next_retry_at = None
            return
        if row.next_retry_at is None:
            delay = self.settings.native_goal_retry_delay_seconds
            if rate_limited(row.last_error):
                delay = max(60, delay)
            row.next_retry_at = now + timedelta(seconds=delay * 2**row.retry_attempts)
            row.execution_status = "retrying"
            if notify:
                self._queue(session, row, "retrying", now)

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, dict):
            return (
                " ".join(
                    str(error.get(key) or "")
                    for key in ("message", "codexErrorInfo", "additionalDetails")
                ).strip()
                or "Codex execution failed"
            )
        return str(error)

    def _failure(
        self, session: Session, row: models.NativeGoalMonitor, error: str, now: datetime
    ) -> None:
        error = redact(error)[:1_000]
        if row.goal_snapshot.get("status") not in {"active", "blocked"}:
            return
        if "interrupted" in error.lower():
            row.next_retry_at = None
            row.retryable_error = False
            row.last_error = error
            row.last_error_at = now
            row.execution_status = "paused"
            return
        if row.last_error == error:
            return
        row.last_error = error
        row.last_error_at = now
        row.retryable_error = False
        row.next_retry_at = None
        row.execution_status = "error"
        self._schedule_retry(session, row, now)
        self._queue(session, row, "error", now)

    def failure(self, branch_id: str, error: str) -> None:
        if control_request_timeout(error):
            self.observation_failed(branch_id, error, recovery_started=True)
            return
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if row and branch and row.thread_id == branch.codex_thread_id:
                self._failure(session, row, error, datetime.now(UTC))
                record_goal_history(session, row, datetime.now(UTC))
                session.commit()

    def pause_control_result(self, branch_id: str, error: str | None = None) -> None:
        """Keep a failed interruption actionable without scheduling Goal recovery."""
        prefix = "Goal paused, but the current turn could not be interrupted:"
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if (
                row is None or branch is None or row.thread_id != branch.codex_thread_id
                or row.goal_snapshot.get("status") != "paused"
            ):
                return
            if error:
                row.last_error = redact(error)[:1_000]
                row.last_error_at = datetime.now(UTC)
            elif str(row.last_error or "").startswith(prefix):
                row.last_error = None
                row.last_error_at = None
            row.next_retry_at = None
            row.retryable_error = False
            record_goal_history(session, row, datetime.now(UTC))
            session.commit()

    def observation_failed(
        self, branch_id: str, error: str, *, recovery_started: bool = False
    ) -> dict[str, Any] | None:
        """A missing RPC response does not establish that the executing turn failed."""
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if row is None or branch is None or row.thread_id != branch.codex_thread_id:
                return None
            if recovery_started and row.execution_status == "recovering":
                row.retry_attempts = max(0, row.retry_attempts - 1)
            if not row.last_error or observation_error(row.last_error):
                row.execution_status = "unavailable"
                message = OBSERVATION_ERROR_PREFIX + redact(error)[:900]
                if row.last_error != message:
                    row.last_error = message
                    row.last_error_at = datetime.now(UTC)
                row.retryable_error = False
                row.next_retry_at = None
            snapshot = dict(row.goal_snapshot)
            session.commit()
            return snapshot or None

    @staticmethod
    def _clear_observation_error(row: models.NativeGoalMonitor) -> None:
        if observation_error(row.last_error):
            row.last_error = None
            row.last_error_at = None
            row.retryable_error = False
            row.next_retry_at = None
            row.retry_attempts = 0

    def checked(self, branch_id: str, status: str) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if row is None:
                return
            row.last_checked_at = now
            goal_status = row.goal_snapshot.get("status")
            if status != "unknown":
                self._clear_observation_error(row)
            if row.next_retry_at:
                row.execution_status = "retrying"
            elif (
                row.last_error
                and "interrupted" in row.last_error.lower()
                and goal_status == "active"
            ):
                row.execution_status = "paused"
            elif goal_status == "active":
                if (status == "detached" and row.execution_status == "external_owner") or (
                    row.last_error and row.execution_status == "error"
                ):
                    pass
                else:
                    row.execution_status = status
            elif not row.next_retry_at:
                row.execution_status = str(goal_status or "unknown")
            if (
                goal_status == "active"
                and (now - (as_utc(row.last_progress_at) or now)).total_seconds()
                >= self.settings.native_goal_progress_interval_seconds
            ):
                self._queue(session, row, "progress", now)
                row.last_progress_at = now
            session.commit()

    def retry_due(self, branch_id: str) -> bool:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            return bool(
                row
                and row.retryable_error
                and row.next_retry_at
                and as_utc(row.next_retry_at) <= datetime.now(UTC)
                and row.retry_attempts < self.settings.native_goal_max_recovery_attempts
            )

    def begin_recovery(self, branch_id: str, *, automatic: bool) -> bool:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if row is None or branch is None or row.thread_id != branch.codex_thread_id:
                return False
            if automatic and row.last_error and "interrupted" in row.last_error.lower():
                return False
            if automatic and self._unsettled_tools(row):
                self._failure(
                    session,
                    row,
                    "Previous tool results are not confirmed; resume manually.",
                    datetime.now(UTC),
                )
                session.commit()
                return False
            if automatic and row.retry_attempts >= self.settings.native_goal_max_recovery_attempts:
                self._failure(
                    session,
                    row,
                    "Automatic recovery attempts exhausted; resume manually.",
                    datetime.now(UTC),
                )
                session.commit()
                return False
            row.retry_attempts = row.retry_attempts + 1 if automatic else 0
            row.next_retry_at = None
            row.retryable_error = False
            row.last_error = None
            row.execution_status = "recovering"
            session.commit()
            return True

    def recovered(self, branch_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if row:
                self._queue(session, row, "recovered", datetime.now(UTC))
                session.commit()

    def external_owner(self, branch_id: str) -> None:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if row:
                row.execution_status = "external_owner"
                row.retry_attempts = max(0, row.retry_attempts - 1)
                row.next_retry_at = None
                row.last_error = None
                session.commit()

    def runtime(self, branch_id: str) -> schemas.NativeGoalRuntime | None:
        with self.session_factory() as session:
            row = session.get(models.NativeGoalMonitor, branch_id)
            if row is None:
                return None
            notice = session.scalar(
                select(models.NativeGoalNotification)
                .where(
                    models.NativeGoalNotification.branch_id == branch_id,
                    models.NativeGoalNotification.event_key.startswith(
                        f"{branch_id}:{row.goal_key}:"
                    ),
                )
                .order_by(models.NativeGoalNotification.created_at.desc())
                .limit(1)
            )
            return schemas.NativeGoalRuntime(
                status=row.execution_status,
                last_checked_at=as_utc(row.last_checked_at),
                last_activity_at=as_utc(row.last_activity_at),
                last_summary=row.last_summary,
                last_error=row.last_error,
                retry_attempts=row.retry_attempts,
                next_retry_at=as_utc(row.next_retry_at),
                notification_status=notice.status if notice else None,
                notification_sent_at=as_utc(notice.sent_at) if notice else None,
                notification_error=notice.last_error if notice else None,
                progress_interval_seconds=self.settings.native_goal_progress_interval_seconds,
                supervised=self.settings.native_goal_supervision_enabled,
                notifications_enabled=self.settings.infoflow_notifications_enabled,
            )

    def _queue(
        self, session: Session, row: models.NativeGoalMonitor, kind: str, now: datetime
    ) -> None:
        if not self.settings.infoflow_notifications_enabled or observation_error(row.last_error):
            return
        row.notice_sequence += 1
        session.add(
            models.NativeGoalNotification(
                branch_id=row.branch_id,
                event_key=f"{row.branch_id}:{row.goal_key}:{row.notice_sequence}",
                kind=kind,
                recipient=self.settings.infoflow_recipient,
                payload={
                    "thread_id": row.thread_id,
                    "objective": redact(str(row.goal_snapshot.get("objective") or "")),
                    "goal_status": "cleared"
                    if kind == "cleared"
                    else row.goal_snapshot.get("status"),
                    "execution_status": row.execution_status,
                    "tokens_used": row.goal_snapshot.get("tokensUsed", 0),
                    "time_used_seconds": row.goal_snapshot.get("timeUsedSeconds", 0),
                    "summary": row.last_summary,
                    "error": row.last_error,
                    "retry_attempts": row.retry_attempts,
                    "next_retry_at": as_utc(row.next_retry_at).isoformat()
                    if row.next_retry_at
                    else None,
                    "requires_review": self._unsettled_tools(row) and bool(row.last_error),
                    "observed_at": now.isoformat(),
                },
            )
        )
