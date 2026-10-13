from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Program(TimestampMixin, Base):
    __tablename__ = "programs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_sequence: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    work_items: Mapped[list[WorkItem]] = relationship(back_populates="program")


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)
    remote_url: Mapped[str | None] = mapped_column(Text)
    default_ref: Mapped[str] = mapped_column(String(160), default="HEAD", nullable=False)
    program_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WorkItem(TimestampMixin, Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_program_lifecycle", "program_id", "lifecycle"),
        Index("ix_work_program_flow", "program_id", "flow"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    human_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    program_id: Mapped[str] = mapped_column(
        ForeignKey("programs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    primary_parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("work_items.id", ondelete="SET NULL"), index=True
    )
    repository_id: Mapped[str | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="task", nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    flow: Mapped[str] = mapped_column(String(32), default="actionable", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    acceptance_criteria: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    labels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    program: Mapped[Program] = relationship(back_populates="work_items")
    repository: Mapped[Repository | None] = relationship()
    primary_parent: Mapped[WorkItem | None] = relationship(remote_side="WorkItem.id")
    branches: Mapped[list[ExecutionBranch]] = relationship(
        back_populates="work_item", cascade="all, delete-orphan"
    )


class WorkEdge(TimestampMixin, Base):
    __tablename__ = "work_edges"
    __table_args__ = (
        Index("uq_work_edge", "from_work_item_id", "to_work_item_id", "edge_type", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)


class ExecutionBranch(TimestampMixin, Base):
    __tablename__ = "execution_branches"
    __table_args__ = (
        Index("uq_branch_work_slug", "work_item_id", "slug", unique=True),
        Index("ix_branch_thread", "codex_thread_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(24), default="write", nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_path: Mapped[str | None] = mapped_column(Text)
    git_branch: Mapped[str | None] = mapped_column(String(320))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    codex_thread_id: Mapped[str | None] = mapped_column(String(80))
    runtime: Mapped[str] = mapped_column(String(16), default="codex", nullable=False, index=True)
    execution_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    native_goal_execution: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    continued_by_branch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    memory_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    work_item: Mapped[WorkItem] = relationship(back_populates="branches")
    parent_branch: Mapped[ExecutionBranch | None] = relationship(remote_side="ExecutionBranch.id")
    sessions: Mapped[list[SessionAttempt]] = relationship(
        back_populates="branch", cascade="all, delete-orphan"
    )


class SessionAttempt(TimestampMixin, Base):
    __tablename__ = "session_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    codex_thread_id: Mapped[str | None] = mapped_column(String(80), index=True)
    runtime: Mapped[str] = mapped_column(String(16), default="codex", nullable=False, index=True)
    execution_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="starting", nullable=False, index=True)
    lease_id: Mapped[str] = mapped_column(String(36), default=new_id, nullable=False, unique=True)
    lease_owner: Mapped[str] = mapped_column(String(160), default="web", nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    work_item_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    handoff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_turn_id: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    branch: Mapped[ExecutionBranch] = relationship(back_populates="sessions")


class CodexThreadBinding(TimestampMixin, Base):
    __tablename__ = "codex_thread_bindings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    initial_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_attempts.id", ondelete="SET NULL"), index=True
    )
    thread_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="current", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="terminal", nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class CodexWindow(TimestampMixin, Base):
    """A locally discoverable Codex thread and its current terminal presence."""

    __tablename__ = "codex_windows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    parent_thread_id: Mapped[str | None] = mapped_column(String(80), index=True)
    cwd: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rollout_path: Mapped[str] = mapped_column(Text, nullable=False)
    cli_version: Mapped[str | None] = mapped_column(String(40))
    originator: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(24), default="window", nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    title_source: Mapped[str | None] = mapped_column(String(24))
    pid: Mapped[int | None] = mapped_column(Integer, index=True)
    tty: Mapped[str | None] = mapped_column(String(80))
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    match_confidence: Mapped[str] = mapped_column(
        String(24), default="rollout_only", nullable=False
    )
    branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="SET NULL"), index=True
    )
    session_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_attempts.id", ondelete="SET NULL"), index=True
    )
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CodexTurn(TimestampMixin, Base):
    __tablename__ = "codex_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_attempts.id", ondelete="SET NULL"), index=True
    )
    thread_binding_id: Mapped[str] = mapped_column(
        ForeignKey("codex_thread_bindings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    external_turn_id: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="completed", nullable=False, index=True)
    prompt: Mapped[str | None] = mapped_column(Text)
    final_answer: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TurnNotification(TimestampMixin, Base):
    """Durable, idempotent delivery record for one completed Codex turn."""

    __tablename__ = "turn_notifications"
    __table_args__ = (
        Index("uq_turn_notification", "turn_id", "channel", "recipient", unique=True),
        Index("ix_turn_notification_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    turn_id: Mapped[str] = mapped_column(
        ForeignKey("codex_turns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(240))
    last_error: Mapped[str | None] = mapped_column(Text)


class ConversationFailureNotification(TimestampMixin, Base):
    """Durable, idempotent Infoflow alert for a failed Web conversation message."""

    __tablename__ = "conversation_failure_notifications"
    __table_args__ = (
        Index(
            "uq_conversation_failure_notification",
            "message_id",
            "channel",
            "recipient",
            unique=True,
        ),
        Index("ix_conversation_failure_notification_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(240))
    last_error: Mapped[str | None] = mapped_column(Text)


class NativeGoalMonitor(TimestampMixin, Base):
    """Execution observations only; Codex remains the owner of Goal state."""

    __tablename__ = "native_goal_monitors"

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), primary_key=True
    )
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    goal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    goal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_summary: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    turn_id: Mapped[str | None] = mapped_column(String(80))
    turn_has_side_effects: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    turn_tool_state: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    retryable_error: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notice_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NativeGoalHistory(Base):
    """Read-only observations retained across Goal replacement and thread rollover."""

    __tablename__ = "native_goal_history"
    __table_args__ = (
        Index("ix_native_goal_history_branch_recorded", "branch_id", "first_recorded_at"),
    )

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), primary_key=True
    )
    goal_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False)
    goal_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    first_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_summary: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    turn_id: Mapped[str | None] = mapped_column(String(80))


class NativeGoalNotification(TimestampMixin, Base):
    """Durable outbox for native Goal transitions, progress, and recovery."""

    __tablename__ = "native_goal_notifications"
    __table_args__ = (
        Index("ix_native_goal_notification_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    recipient: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(240))
    last_error: Mapped[str | None] = mapped_column(Text)


class ClaudeWakeup(TimestampMixin, Base):
    __tablename__ = "claude_wakeups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_turn_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="awaiting_turn", index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL"), unique=True
    )
    reason: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledCheck(TimestampMixin, Base):
    __tablename__ = "scheduled_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    phase: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    stop_mode: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    stop_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_jobs.id", ondelete="SET NULL")
    )
    notification_mode: Mapped[str] = mapped_column(String(24), default="important", nullable=False)
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(32))
    last_summary: Mapped[str | None] = mapped_column(Text)
    last_notice_fingerprint: Mapped[str | None] = mapped_column(String(64))
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class ScheduledCheckRun(TimestampMixin, Base):
    __tablename__ = "scheduled_check_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    check_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_checks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="claimed", nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL"), unique=True
    )
    outcome: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledCheckNotification(TimestampMixin, Base):
    __tablename__ = "scheduled_check_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    recipient: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(240))
    last_error: Mapped[str | None] = mapped_column(Text)


class DailyReflection(TimestampMixin, Base):
    """One idempotent reflection over a completed local calendar day."""

    __tablename__ = "daily_reflections"
    __table_args__ = (
        Index("uq_daily_reflection_day_tz", "reflection_date", "timezone", unique=True),
        Index("ix_daily_reflection_status_due", "status", "next_attempt_at"),
        Index(
            "ix_daily_reflection_notification_due",
            "notification_status",
            "notification_next_attempt_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reflection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    source_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    report_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    agent_model: Mapped[str | None] = mapped_column(String(160))
    agent_provider: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    notification_status: Mapped[str] = mapped_column(
        String(24), default="pending", nullable=False, index=True
    )
    notification_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notification_next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    notification_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_message_id: Mapped[str | None] = mapped_column(String(240))
    notification_error: Mapped[str | None] = mapped_column(Text)


class CodexSyncCursor(TimestampMixin, Base):
    __tablename__ = "codex_sync_cursors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_binding_id: Mapped[str] = mapped_column(
        ForeignKey("codex_thread_bindings.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    st_dev: Mapped[int] = mapped_column(Integer, nullable=False)
    st_ino: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    last_event_id: Mapped[str | None] = mapped_column(String(120))
    pending_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


class ConversationMessage(TimestampMixin, Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_message_branch_created", "branch_id", "created_at"),
        Index(
            "uq_message_external_turn",
            "branch_id",
            "turn_id",
            "external_item_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("session_attempts.id", ondelete="SET NULL"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(80), index=True)
    external_item_id: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="message", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MemoryEntry(TimestampMixin, Base):
    __tablename__ = "memory_entries"
    __table_args__ = (Index("ix_memory_scope", "scope_type", "scope_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    program_id: Mapped[str] = mapped_column(
        ForeignKey("programs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="observation", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(320), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    source_branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="SET NULL"), index=True
    )
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_entries.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Experiment(TimestampMixin, Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    candidate_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    controls: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    success_criteria: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Execution(TimestampMixin, Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="SET NULL"), index=True
    )
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="SET NULL"), index=True
    )
    execution_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionWorkflow(TimestampMixin, Base):
    """Disposable read-side projection; never a source of execution state."""

    __tablename__ = "session_workflows"

    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), primary_key=True
    )
    graphs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    analyzed_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(24), default="waiting", nullable=False)
    analysis_error: Mapped[str | None] = mapped_column(Text)
    analysis_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class OctoWorkflowSnapshot(TimestampMixin, Base):
    __tablename__ = "octo_workflow_snapshots"

    job_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExternalJob(TimestampMixin, Base):
    __tablename__ = "external_jobs"
    __table_args__ = (Index("uq_external_job", "provider", "remote_job_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    remote_job_id: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ManagedJob(TimestampMixin, Base):
    """User-facing logical job whose provider placement remains internal."""

    __tablename__ = "managed_jobs"
    __table_args__ = (
        Index("uq_managed_job_submitter_key", "submitter", "idempotency_key", unique=True),
        Index("ix_managed_job_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_jobs.id", ondelete="SET NULL"), index=True
    )
    submitter: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    hardware: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_path: Mapped[str] = mapped_column(Text, nullable=False)
    run_path: Mapped[str] = mapped_column(Text, nullable=False)
    train_path: Mapped[str | None] = mapped_column(Text)
    preflight_receipt_path: Mapped[str | None] = mapped_column(Text)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    run_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    train_sha256: Mapped[str | None] = mapped_column(String(64))
    requested_nodes: Mapped[int] = mapped_column(Integer, nullable=False)
    gpus_per_node: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    dispatch_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LtpMonitorState(TimestampMixin, Base):
    """Singleton control record for the independent LTP monitor."""

    __tablename__ = "ltp_monitor_state"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LtpMonitoredJob(TimestampMixin, Base):
    """A durable, user-scoped identity observed by the LTP monitor."""

    __tablename__ = "ltp_monitored_jobs"
    __table_args__ = (
        Index("uq_ltp_monitored_job_identity", "user", "job_name", unique=True),
        Index("ix_ltp_monitored_job_state_seen", "state", "last_observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    job_name: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    cluster: Mapped[str | None] = mapped_column(String(160), index=True)
    state: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False, index=True)
    provider_status: Mapped[str | None] = mapped_column(String(160))
    state_group: Mapped[str | None] = mapped_column(String(80))
    category: Mapped[str | None] = mapped_column(String(80))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LtpMonitorEvent(TimestampMixin, Base):
    """Append-only observable LTP state transition, independent of Sessions."""

    __tablename__ = "ltp_monitor_events"
    __table_args__ = (Index("ix_ltp_monitor_event_job_observed", "job_id", "observed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("ltp_monitored_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_status: Mapped[str | None] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(48), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LtpMonitorNotification(TimestampMixin, Base):
    """Durable group-message outbox for one LTP status event."""

    __tablename__ = "ltp_monitor_notifications"
    __table_args__ = (
        Index("uq_ltp_monitor_notification", "event_id", "channel", "target", unique=True),
        Index("ix_ltp_monitor_notification_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("ltp_monitor_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(240))
    last_error: Mapped[str | None] = mapped_column(Text)


class GoalRun(TimestampMixin, Base):
    """A durable, user-approved background objective for one Branch.

    A GoalRun deliberately stores the orchestration state outside Codex JSONL.
    The worker can therefore survive a browser close, a CLI exit, or a service
    restart without treating an assistant sentence as a running monitor.
    """

    __tablename__ = "goal_runs"
    __table_args__ = (
        Index("ix_goal_runs_due", "status", "next_run_at"),
        Index("ix_goal_runs_branch_status", "branch_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    executor: Mapped[str] = mapped_column(String(24), default="agent", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    cadence_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    max_agent_turns: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    agent_turns_started: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    completion_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class GoalEvent(TimestampMixin, Base):
    """Append-only evidence and action ledger for a GoalRun."""

    __tablename__ = "goal_events"
    __table_args__ = (Index("ix_goal_events_goal_created", "goal_run_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    goal_run_id: Mapped[str] = mapped_column(
        ForeignKey("goal_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Artifact(TimestampMixin, Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="SET NULL"), index=True
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(320), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Metric(TimestampMixin, Base):
    __tablename__ = "metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    dataset: Mapped[str | None] = mapped_column(String(320))
    split: Mapped[str | None] = mapped_column(String(120))
    baseline_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL")
    )
    delta: Mapped[float | None] = mapped_column(Float)


class EvalRun(TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_run_work_status", "work_item_id", "status"),
        Index("ix_eval_run_config", "adapter", "config_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_item_id: Mapped[str] = mapped_column(
        ForeignKey("work_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(320), nullable=False)
    adapter: Mapped[str] = mapped_column(String(80), default="generic", nullable=False)
    mode: Mapped[str] = mapped_column(String(24), default="canonical", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(320), nullable=False)
    candidate_digest: Mapped[str | None] = mapped_column(String(128))
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text)
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    tool_parser: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    expected_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalUnit(TimestampMixin, Base):
    __tablename__ = "eval_units"
    __table_args__ = (
        Index("uq_eval_unit_run_key", "run_id", "unit_key", unique=True),
        Index("ix_eval_unit_run_status_priority", "run_id", "status", "priority"),
        Index("ix_eval_unit_cache_lookup", "unit_key", "status"),
        Index("ix_eval_unit_material", "run_id", "material_key", "status"),
        Index("ix_eval_unit_group", "run_id", "case_key", "rubric_group", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_key: Mapped[str] = mapped_column(String(640), nullable=False, index=True)
    domain: Mapped[str | None] = mapped_column(String(120), index=True)
    rubric_key: Mapped[str] = mapped_column(String(240), nullable=False)
    rubric_group: Mapped[str] = mapped_column(String(160), nullable=False)
    unit_key: Mapped[str] = mapped_column(String(64), nullable=False)
    material_key: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(36), index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_category: Mapped[str | None] = mapped_column(String(80), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cache_source_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("eval_units.id", ondelete="SET NULL"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BranchResult(TimestampMixin, Base):
    __tablename__ = "branch_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    acceptance_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    proposed_promotions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    next_action: Mapped[str | None] = mapped_column(Text)


class MergeReview(TimestampMixin, Base):
    __tablename__ = "merge_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    branch_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("branch_results.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    resolution: Mapped[str | None] = mapped_column(String(24))
    selected_changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillProposal(TimestampMixin, Base):
    __tablename__ = "skill_proposals"
    __table_args__ = (
        Index("uq_skill_proposal_session", "session_id", unique=True),
        Index("ix_skill_proposal_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session_attempts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    action: Mapped[str | None] = mapped_column(String(16))
    candidate_name: Mapped[str | None] = mapped_column(String(64))
    target_skill: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reusable_steps: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    proposed_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    diff_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    install_path: Mapped[str | None] = mapped_column(Text)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityEvent(TimestampMixin, Base):
    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_event_scope", "scope_type", "scope_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(160), default="system", nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PendingApproval(TimestampMixin, Base):
    __tablename__ = "pending_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    branch_id: Mapped[str | None] = mapped_column(
        ForeignKey("execution_branches.id", ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(80), index=True)
    rpc_request_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    method: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
