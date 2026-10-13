from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.infoflow import InfoflowSendResult
from research_ops.services.codex_events import redact
from research_ops.settings import Settings


class TextSender(Protocol):
    @property
    def configured(self) -> bool: ...

    def send_text(self, recipient: str, content: str) -> InfoflowSendResult: ...


@dataclass(frozen=True)
class NotificationDispatch:
    notification_id: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class _QueuedNotification:
    kind: str
    notification_id: str
    created_at: datetime


Notification = (
    models.TurnNotification | models.ConversationFailureNotification | models.NativeGoalNotification
    | models.ScheduledCheckNotification
)


def _notification_type(kind: str) -> type[Notification]:
    return {
        "turn": models.TurnNotification,
        "failure": models.ConversationFailureNotification,
        "goal": models.NativeGoalNotification,
        "check": models.ScheduledCheckNotification,
    }[kind]


class TurnNotificationService:
    """Deliver completed-turn and failed-conversation alerts without blocking Codex."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        sender: TextSender,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.sender = sender

    def dispatch_pending(self) -> list[NotificationDispatch]:
        if not self.settings.infoflow_notifications_enabled or not self.sender.configured:
            return []
        now = datetime.now(UTC)
        self._recover_stale_claims(now)
        notifications = self._pending_notifications(now)
        results: list[NotificationDispatch] = []
        for queued in notifications:
            claimed = self._claim(queued.kind, queued.notification_id)
            if claimed is None:
                continue
            try:
                message = self._message(queued.kind, claimed)
                sent = self.sender.send_text(claimed.recipient, message)
            except Exception as exc:
                error = redact(str(exc) or type(exc).__name__)[:1_000]
                self._finish_failed(queued.kind, queued.notification_id, error)
                results.append(NotificationDispatch(queued.notification_id, "failed", error))
                continue
            self._finish_sent(queued.kind, queued.notification_id, sent.message_id)
            results.append(NotificationDispatch(queued.notification_id, "sent"))
        return results

    def _pending_notifications(self, now: datetime) -> list[_QueuedNotification]:
        """Read both durable outboxes and preserve their creation order."""
        with self.session_factory() as session:
            turns = list(
                session.execute(
                    select(models.TurnNotification.id, models.TurnNotification.created_at)
                    .where(
                        models.TurnNotification.channel == "infoflow",
                        models.TurnNotification.status.in_(("pending", "failed")),
                        or_(
                            models.TurnNotification.next_attempt_at.is_(None),
                            models.TurnNotification.next_attempt_at <= now,
                        ),
                    )
                    .order_by(models.TurnNotification.created_at)
                    .limit(self.settings.infoflow_dispatch_batch_size)
                )
            )
            failures = []
            if self.settings.infoflow_error_notifications_enabled:
                failures = list(
                    session.execute(
                        select(
                            models.ConversationFailureNotification.id,
                            models.ConversationFailureNotification.created_at,
                        )
                        .where(
                            models.ConversationFailureNotification.channel == "infoflow",
                            models.ConversationFailureNotification.status.in_(
                                ("pending", "failed")
                            ),
                            or_(
                                models.ConversationFailureNotification.next_attempt_at.is_(None),
                                models.ConversationFailureNotification.next_attempt_at <= now,
                            ),
                        )
                        .order_by(models.ConversationFailureNotification.created_at)
                        .limit(self.settings.infoflow_dispatch_batch_size)
                    )
                )
            goals = list(
                session.execute(
                    select(
                        models.NativeGoalNotification.id, models.NativeGoalNotification.created_at
                    )
                    .where(
                        models.NativeGoalNotification.status.in_(("pending", "failed")),
                        or_(
                            models.NativeGoalNotification.next_attempt_at.is_(None),
                            models.NativeGoalNotification.next_attempt_at <= now,
                        ),
                    )
                    .order_by(models.NativeGoalNotification.created_at)
                    .limit(self.settings.infoflow_dispatch_batch_size)
                )
            )
            checks = list(session.execute(select(
                models.ScheduledCheckNotification.id, models.ScheduledCheckNotification.created_at
            ).where(
                models.ScheduledCheckNotification.status.in_(("pending", "failed")),
                or_(models.ScheduledCheckNotification.next_attempt_at.is_(None),
                    models.ScheduledCheckNotification.next_attempt_at <= now),
            ).order_by(models.ScheduledCheckNotification.created_at).limit(
                self.settings.infoflow_dispatch_batch_size
            )))
        queued = [
            _QueuedNotification("turn", notification_id, created_at)
            for notification_id, created_at in turns
        ]
        queued.extend(
            _QueuedNotification("failure", notification_id, created_at)
            for notification_id, created_at in failures
        )
        queued.extend(
            _QueuedNotification("goal", notification_id, created_at)
            for notification_id, created_at in goals
        )
        queued.extend(
            _QueuedNotification("check", notification_id, created_at)
            for notification_id, created_at in checks
        )
        queued.sort(key=lambda item: _as_utc(item.created_at))
        return queued[: self.settings.infoflow_dispatch_batch_size]

    def _recover_stale_claims(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=10)
        with self.session_factory() as session:
            notification_types = [
                models.TurnNotification, models.NativeGoalNotification,
                models.ScheduledCheckNotification,
            ]
            if self.settings.infoflow_error_notifications_enabled:
                notification_types.append(models.ConversationFailureNotification)
            changed = False
            for notification_type in notification_types:
                stale = list(
                    session.scalars(
                        select(notification_type).where(
                            notification_type.status == "sending",
                            notification_type.updated_at < cutoff,
                        )
                    )
                )
                for notification in stale:
                    notification.status = "failed"
                    notification.last_error = "Delivery worker stopped before confirming the send"
                    notification.next_attempt_at = now
                    changed = True
            if changed:
                session.commit()

    def _claim(self, kind: str, notification_id: str) -> Notification | None:
        notification_type = _notification_type(kind)
        with self.session_factory() as session:
            notification = session.get(notification_type, notification_id)
            if notification is None or notification.status not in {"pending", "failed"}:
                return None
            if isinstance(notification, models.TurnNotification):
                reason = self._goal_turn_suppression(session, notification.turn_id)
                if reason:
                    notification.status = "suppressed"
                    notification.last_error = reason
                    notification.next_attempt_at = None
                    session.commit()
                    return None
            notification.status = "sending"
            notification.attempts += 1
            notification.next_attempt_at = None
            session.commit()
            return notification

    @staticmethod
    def _goal_turn_suppression(session: Session, turn_id: str) -> str | None:
        turn = session.get(models.CodexTurn, turn_id)
        if turn is None or turn.metadata_json.get("prompt_kind") != "goal_continuation":
            return None
        branch = session.get(models.ExecutionBranch, turn.branch_id)
        if branch and branch.codex_thread_id and branch.codex_thread_id != turn.thread_id:
            return "Automatic Goal turn belongs to a superseded session"
        monitor = session.get(models.NativeGoalMonitor, turn.branch_id)
        if monitor and monitor.thread_id == turn.thread_id and (
            monitor.goal_snapshot.get("status") == "paused"
            or monitor.execution_status in {"cleared", "superseded"}
        ):
            return "Automatic Goal turn completed after its Goal was paused or cleared"
        return None

    def _message(
        self,
        kind: str,
        notification: Notification,
    ) -> str:
        if kind == "check":
            assert isinstance(notification, models.ScheduledCheckNotification)
            return self._check_message(notification)
        if kind == "goal":
            assert isinstance(notification, models.NativeGoalNotification)
            return self._goal_message(notification)
        if kind == "failure":
            assert isinstance(notification, models.ConversationFailureNotification)
            return self._failure_message(notification)
        assert isinstance(notification, models.TurnNotification)
        return self._completed_message(notification)

    def _check_message(self, notification: models.ScheduledCheckNotification) -> str:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, notification.branch_id)
            if branch is None:
                raise RuntimeError("Scheduled check Branch no longer exists")
            work = session.get(models.WorkItem, branch.work_item_id)
            assert work
            program = session.get(models.Program, work.program_id)
            code = program.code if program else "WORK"
            label = {
                "unchanged": "无变化", "changed": "有进展", "failed": "任务失败",
                "completed": "任务完成", "needs_input": "需要决定", "error": "检查失败",
                "interrupted": "检查被打断", "unclassified": "结果待确认",
                "schedule_ended": "检查计划结束",
            }.get(notification.kind, notification.kind)
            observed = _as_utc(notification.created_at).astimezone(ZoneInfo("Asia/Shanghai"))
            lines = [
                f"Research Ops · 定时检查 · {label}",
                f"[{code}] {work.human_id} · {work.title or '未命名工作'}",
                f"Branch：{branch.name}",
                f"Session ID：{branch.codex_thread_id or '尚未创建'}",
                f"时间：{observed:%Y-%m-%d %H:%M}（上海时间）",
                self._clip(redact(str(notification.payload.get("summary") or ""))),
            ]
            if notification.payload.get("check_status") == "paused":
                lines.append("后续检查已暂停，请查看结果后手动恢复；外部任务未停止。")
            lines.append(f"查看：{self._results_url(code, branch.id)}")
            return "\n".join(lines)

    def _goal_message(self, notification: models.NativeGoalNotification) -> str:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, notification.branch_id)
            if branch is None:
                raise RuntimeError("Goal Branch no longer exists")
            work = session.get(models.WorkItem, branch.work_item_id)
            if work is None:
                raise RuntimeError("Goal Work no longer exists")
            program = session.get(models.Program, work.program_id)
            code = program.code if program else "WORK"
            data = notification.payload
            status = str(data.get("goal_status") or "cleared")
            labels = {
                "active": "进行中",
                "paused": "已暂停",
                "blocked": "受阻",
                "usageLimited": "额度受限",
                "budgetLimited": "预算用尽",
                "complete": "已完成",
                "cleared": "已清除",
            }
            runtime_labels = {
                "running": "正在执行",
                "idle": "等待下一轮",
                "detached": "执行进程未连接",
                "recovering": "正在恢复",
                "retrying": "等待重试",
                "error": "执行失败",
                "external_owner": "由其他 Codex 进程持有",
                "unknown": "待核验",
            }
            execution_label = runtime_labels.get(
                str(data.get("execution_status")), labels.get(status, status)
            )
            title = {
                "progress": "目标进度",
                "error": "目标执行失败",
                "recovered": "目标执行已恢复",
                "retrying": "目标等待重试",
            }.get(notification.kind, f"目标{labels.get(status, status)}")
            if notification.kind == "error" and data.get("next_retry_at"):
                title = "目标等待重试"
            observed = _as_utc(notification.created_at).astimezone(ZoneInfo("Asia/Shanghai"))
            lines = [
                f"Research Ops · {title}",
                f"[{code}] {work.human_id} · {work.title.strip() or '未命名工作'}",
                f"Branch：{branch.name}",
                f"Codex Session ID：{data.get('thread_id')}",
                f"目标：{self._clip(str(data.get('objective') or ''))}",
                f"状态：{labels.get(status, status)}",
                f"执行：{execution_label}",
                f"时间：{observed:%Y-%m-%d %H:%M}（上海时间）",
            ]
            if data.get("summary"):
                lines.append(f"最近进展：{self._clip(' '.join(str(data['summary']).split()))}")
            if data.get("error"):
                lines.append(f"原因：{self._clip(redact(str(data['error'])))}")
            if data.get("next_retry_at"):
                retry_at = datetime.fromisoformat(str(data["next_retry_at"])).astimezone(
                    ZoneInfo("Asia/Shanghai")
                )
                lines.append(
                    f"下一步：{retry_at:%H:%M:%S} 自动重试，"
                    f"最多 {self.settings.native_goal_max_recovery_attempts} 次；保持原身份和模型。"
                )
            elif data.get("requires_review"):
                lines.append(
                    "下一步：仍有工具未结束或结果未确认，暂不自动重试；"
                    "请核验工具结果及任务回执后在 Goals 中恢复。"
                )
            elif status == "paused":
                lines.append("下一步：等待手动 Resume；不会自动恢复已暂停的目标。")
            elif (
                status in {"blocked", "usageLimited", "budgetLimited"}
                or notification.kind == "error"
            ):
                lines.append("下一步：解决阻塞或额度问题后，在 Goals 中手动 Resume。")
            lines.append(f"查看：{self._results_url(code, branch.id)}")
            return "\n".join(lines)

    def _completed_message(self, notification: models.TurnNotification) -> str:
        with self.session_factory() as session:
            turn = session.get(models.CodexTurn, notification.turn_id)
            if turn is None:
                raise RuntimeError("Completed turn no longer exists")
            branch = session.get(models.ExecutionBranch, turn.branch_id)
            if branch is None:
                raise RuntimeError("Turn branch no longer exists")
            work = session.get(models.WorkItem, branch.work_item_id)
            if work is None:
                raise RuntimeError("Turn work item no longer exists")
            program = session.get(models.Program, work.program_id)
            attempt = (
                session.get(models.SessionAttempt, turn.session_id) if turn.session_id else None
            )

            source = (
                "Native Goal"
                if turn.metadata_json.get("prompt_kind") == "goal_continuation"
                else self._source_label(attempt)
            )
            title = work.title.strip() or "未命名工作"
            runtime = str(turn.metadata_json.get("runtime") or branch.runtime or "codex")
            runtime_label = {"claude": "Claude", "pi": "Pi"}.get(runtime, "Codex")
            summary = self._clip(" ".join((turn.final_answer or "").split()))
            completed = _as_utc(turn.completed_at).astimezone(ZoneInfo("Asia/Shanghai"))
            code = program.code if program else "WORK"
            url = self._results_url(code, branch.id)
            return "\n".join(
                (
                    "Research Ops · 一轮已完成",
                    f"[{code}] {work.human_id} · {title}",
                    f"Branch：{branch.name}",
                    f"{runtime_label} Session ID：{turn.thread_id}",
                    f"来源：{source}",
                    f"完成时间：{completed:%Y-%m-%d %H:%M}",
                    f"结果：{summary}",
                    f"查看：{url}",
                )
            )

    def _failure_message(self, notification: models.ConversationFailureNotification) -> str:
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, notification.message_id)
            if message is None:
                raise RuntimeError("Failed conversation message no longer exists")
            branch = session.get(models.ExecutionBranch, message.branch_id)
            if branch is None:
                raise RuntimeError("Failed conversation branch no longer exists")
            work = session.get(models.WorkItem, branch.work_item_id)
            if work is None:
                raise RuntimeError("Failed conversation work item no longer exists")
            program = session.get(models.Program, work.program_id)
            attempt = (
                session.get(models.SessionAttempt, message.session_id)
                if message.session_id
                else None
            )
            title = work.title.strip() or "未命名工作"
            code = program.code if program else "WORK"
            error = redact(str(message.metadata_json.get("error") or "Web 对话未完成"))
            error = self._clip(" ".join(error.split()))
            failed = _as_utc(message.updated_at).astimezone(ZoneInfo("Asia/Shanghai"))
            return "\n".join(
                (
                    "Research Ops · 对话失败",
                    f"[{code}] {work.human_id} · {title}",
                    f"Branch：{branch.name}",
                    f"来源：{self._source_label(attempt)}",
                    f"失败时间：{failed:%Y-%m-%d %H:%M}",
                    f"原因：{error}",
                    f"查看：{self._results_url(code, branch.id)}",
                )
            )

    @staticmethod
    def _source_label(attempt: models.SessionAttempt | None) -> str:
        owner = attempt.lease_owner if attempt else "unknown"
        if owner == "web":
            return "Web"
        if owner.startswith("terminal:"):
            return "Local CLI"
        return owner

    def _clip(self, value: str) -> str:
        limit = max(1, self.settings.infoflow_summary_chars)
        if len(value) > limit:
            return value[: limit - 1].rstrip() + "…"
        return value

    def _results_url(self, program_code: str, branch_id: str) -> str:
        return (
            f"{self.settings.infoflow_public_base_url.rstrip('/')}"
            f"/programs/{program_code}/branches/{branch_id}/results"
        )

    def _finish_sent(self, kind: str, notification_id: str, message_id: str | None) -> None:
        now = datetime.now(UTC)
        notification_type = _notification_type(kind)
        with self.session_factory() as session:
            notification = session.get(notification_type, notification_id)
            if notification is None:
                return
            notification.status = "sent"
            notification.sent_at = now
            notification.provider_message_id = message_id
            notification.last_error = None
            notification.next_attempt_at = None
            if kind == "turn":
                assert isinstance(notification, models.TurnNotification)
                turn = session.get(models.CodexTurn, notification.turn_id)
                if turn is not None:
                    session.add(
                        models.ActivityEvent(
                            scope_type="branch",
                            scope_id=turn.branch_id,
                            event_type="notification.infoflow.sent",
                            actor="infoflow",
                            correlation_id=notification.id,
                            payload={
                                "turn_id": turn.id,
                                "recipient": notification.recipient,
                            },
                        )
                    )
            elif kind in {"goal", "check"}:
                assert isinstance(notification, (
                    models.NativeGoalNotification, models.ScheduledCheckNotification,
                ))
                session.add(
                    models.ActivityEvent(
                        scope_type="branch",
                        scope_id=notification.branch_id,
                        event_type=f"notification.infoflow.{kind}_sent",
                        actor="infoflow",
                        correlation_id=notification.id,
                        payload={"kind": notification.kind, "recipient": notification.recipient},
                    )
                )
            else:
                assert isinstance(notification, models.ConversationFailureNotification)
                message = session.get(models.ConversationMessage, notification.message_id)
                if message is not None:
                    session.add(
                        models.ActivityEvent(
                            scope_type="branch",
                            scope_id=message.branch_id,
                            event_type="notification.infoflow.failure_sent",
                            actor="infoflow",
                            correlation_id=notification.id,
                            payload={
                                "message_id": message.id,
                                "recipient": notification.recipient,
                            },
                        )
                    )
            session.commit()

    def _finish_failed(self, kind: str, notification_id: str, error: str) -> None:
        now = datetime.now(UTC)
        notification_type = _notification_type(kind)
        with self.session_factory() as session:
            notification = session.get(notification_type, notification_id)
            if notification is None:
                return
            delay_seconds = min(3_600, 30 * (2 ** min(notification.attempts - 1, 7)))
            notification.status = "failed"
            notification.last_error = error
            notification.next_attempt_at = now + timedelta(seconds=delay_seconds)
            session.commit()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
