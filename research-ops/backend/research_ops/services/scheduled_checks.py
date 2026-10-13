from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.codex.claude_execution import claude_execution_snapshot
from research_ops.codex.identity import CodexIdentityService
from research_ops.db import run_db
from research_ops.events import EventHub
from research_ops.settings import Settings

from .codex_events import redact
from .core import ConflictError, NotFoundError, ResearchOpsService
from .external_jobs import external_job_phase

logger = logging.getLogger(__name__)

DEFAULT_CHECK_PROMPT = """看看【填写明确的任务名或 Job ID】跑得怎么样。
报错、卡住、进入下一阶段或跑完了就告诉我，普通进度简单说一声就好。
这次只看一下，不改任务，也不用一直等。

最后一行写「检查结果：无变化/有进展/失败/完成/需要决定」，选一项。
"""

OUTCOMES = {
    "无变化": "unchanged",
    "有进展": "changed",
    "失败": "failed",
    "完成": "completed",
    "需要决定": "needs_input",
}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def check_outcome(answer: str) -> str:
    # Only the final answer's explicit last-line verdict controls notification
    # filtering. Tool logs and arbitrary prose never stop a schedule.
    line = answer.strip().splitlines()[-1] if answer.strip() else ""
    match = re.fullmatch(r"检查结果[：:]\s*(无变化|有进展|失败|完成|需要决定)[。.]?", line.strip())
    return OUTCOMES[match[1]] if match else "unclassified"


class ScheduledCheckService:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self.factory = factory
        self.settings = settings

    def detail(self, branch_id: str) -> dict[str, Any]:
        with self.factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            check = session.scalar(
                select(models.ScheduledCheck).where(models.ScheduledCheck.branch_id == branch_id)
            )
            runs = (
                list(
                    session.scalars(
                        select(models.ScheduledCheckRun)
                        .where(models.ScheduledCheckRun.check_id == check.id)
                        .order_by(models.ScheduledCheckRun.created_at.desc())
                        .limit(20)
                    )
                )
                if check
                else []
            )
            jobs = list(
                session.scalars(
                    select(models.ExternalJob)
                    .where(models.ExternalJob.work_item_id == branch.work_item_id)
                    .order_by(models.ExternalJob.updated_at.desc())
                    .limit(100)
                )
            )
            notice = session.scalar(
                select(models.ScheduledCheckNotification)
                .where(models.ScheduledCheckNotification.branch_id == branch_id)
                .order_by(models.ScheduledCheckNotification.created_at.desc())
                .limit(1)
            )
            return {
                "check": schemas.ScheduledCheckRead.model_validate(check).model_dump(mode="json")
                if check
                else None,
                "runs": [
                    schemas.ScheduledCheckRunRead.model_validate(run).model_dump(mode="json")
                    for run in runs
                ],
                "jobs": [
                    {
                        "id": job.id,
                        "name": job.remote_job_id,
                        "status": job.status,
                        "provider": job.provider,
                    }
                    for job in jobs
                ],
                "default_prompt": DEFAULT_CHECK_PROMPT,
                "notification": {"status": notice.status, "error": notice.last_error}
                if notice
                else None,
            }

    def save(self, branch_id: str, payload: schemas.ScheduledCheckSave) -> None:
        now = datetime.now(UTC)
        if payload.stop_at is not None and utc(payload.stop_at) <= now:
            raise ConflictError("Stop time must be in the future")
        if redact(payload.prompt) != payload.prompt:
            raise ConflictError("Keep credentials out of the scheduled prompt")
        with self.factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.hidden or branch.mode == "capture" or branch.status != "active":
                raise ConflictError("Only an active, visible Branch can run scheduled checks")
            if payload.job_id:
                job = session.get(models.ExternalJob, payload.job_id)
                if job is None or job.work_item_id != branch.work_item_id:
                    raise ConflictError("Choose a job belonging to this work item")
            check = session.scalar(
                select(models.ScheduledCheck).where(models.ScheduledCheck.branch_id == branch_id)
            )
            if check is not None and check.active_run_id:
                raise ConflictError(
                    "A check is in progress; pause future checks or edit after it ends"
                )
            if check is None:
                check = models.ScheduledCheck(branch_id=branch_id, prompt=payload.prompt)
                session.add(check)
            else:
                check.revision += 1
            for key, value in payload.model_dump().items():
                setattr(check, key, value)
            check.execution = (
                CodexIdentityService(self.settings).snapshot(branch.execution_preferences)
                if branch.runtime == "codex"
                else {}
            )
            if branch.runtime == "claude":
                check.execution = claude_execution_snapshot(
                    self.settings, branch.execution_preferences
                )
            check.status = "active"
            check.phase = "scheduled"
            check.reason = None
            check.consecutive_errors = 0
            check.next_run_at = now + timedelta(minutes=payload.interval_minutes)
            session.commit()

    def control(self, branch_id: str, action: str) -> None:
        now = datetime.now(UTC)
        with self.factory() as session:
            check = session.scalar(
                select(models.ScheduledCheck).where(models.ScheduledCheck.branch_id == branch_id)
            )
            if check is None:
                raise NotFoundError("No scheduled check exists")
            if action in {"pause", "stop"}:
                check.status = "paused" if action == "pause" else "stopped"
                check.next_run_at = None
                check.reason = (
                    "Future checks paused" if action == "pause" else "Future checks stopped"
                )
                if not check.active_run_id:
                    check.phase = check.status
            else:
                branch = session.get(models.ExecutionBranch, branch_id)
                if not branch or branch.hidden or branch.status != "active":
                    raise ConflictError("Restore the active Branch before resuming checks")
                if check.stop_mode == "job" and (
                    not check.job_id or session.get(models.ExternalJob, check.job_id) is None
                ):
                    raise ConflictError("Choose a linked job before resuming checks")
                if check.active_run_id:
                    raise ConflictError("A check is already in progress")
                if check.stop_at and utc(check.stop_at) <= now:
                    raise ConflictError("The stop time has passed; edit the schedule first")
                if action == "run_now" and check.status != "active":
                    raise ConflictError("Resume the schedule before running a check")
                check.status = "active"
                check.phase = "scheduled"
                check.reason = None
                check.consecutive_errors = 0
                check.next_run_at = (
                    now
                    if action == "run_now"
                    else (now + timedelta(minutes=check.interval_minutes))
                )
            session.commit()

    def claim_due(self) -> list[str]:
        now = datetime.now(UTC)
        result = []
        with self.factory() as session:
            for check in session.scalars(
                select(models.ScheduledCheck)
                .where(
                    models.ScheduledCheck.status == "active",
                    models.ScheduledCheck.active_run_id.is_(None),
                    models.ScheduledCheck.next_run_at <= now,
                )
                .order_by(models.ScheduledCheck.next_run_at)
                .limit(50)
            ):
                run_id = models.new_id()
                claimed = session.execute(
                    update(models.ScheduledCheck)
                    .where(
                        models.ScheduledCheck.id == check.id,
                        models.ScheduledCheck.status == "active",
                        models.ScheduledCheck.active_run_id.is_(None),
                        models.ScheduledCheck.next_run_at <= now,
                    )
                    .values(
                        active_run_id=run_id, next_run_at=None, phase="dispatching", reason=None
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    continue
                session.add(
                    models.ScheduledCheckRun(
                        id=run_id,
                        check_id=check.id,
                        revision=check.revision,
                        prompt=check.prompt,
                        execution=dict(check.execution),
                    )
                )
                result.append(run_id)
            session.commit()
        return result

    def claimed(self, run_id: str) -> tuple[models.ScheduledCheck, models.ScheduledCheckRun]:
        with self.factory() as session:
            run = session.get(models.ScheduledCheckRun, run_id)
            if run is None:
                raise NotFoundError("Check run not found")
            check = session.get(models.ScheduledCheck, run.check_id)
            assert check
            return check, run

    def defer(self, run_id: str, reason: str) -> None:
        with self.factory() as session:
            run = session.get(models.ScheduledCheckRun, run_id)
            if run is None or run.status != "claimed":
                return
            check = session.get(models.ScheduledCheck, run.check_id)
            assert check
            run.status = "deferred"
            run.summary = redact(reason)[:2000]
            run.completed_at = datetime.now(UTC)
            if check.active_run_id == run.id:
                check.active_run_id = None
                check.phase = "waiting_session" if check.status == "active" else check.status
                check.reason = run.summary
                check.next_run_at = (
                    datetime.now(UTC) + timedelta(seconds=60)
                    if (check.status == "active")
                    else None
                )
            session.commit()

    def reconcile(self) -> list[str]:
        now = datetime.now(UTC)
        changed: set[str] = set()
        with self.factory() as session:
            checks = list(
                session.scalars(
                    select(models.ScheduledCheck).where(
                        (models.ScheduledCheck.status == "active")
                        | models.ScheduledCheck.active_run_id.is_not(None)
                    )
                )
            )
            for check in checks:
                if check.status == "active":
                    branch = session.get(models.ExecutionBranch, check.branch_id)
                    job_missing = check.stop_mode == "job" and (
                        not check.job_id or session.get(models.ExternalJob, check.job_id) is None
                    )
                    if not branch or branch.hidden or branch.status != "active" or job_missing:
                        check.status = "paused"
                        check.next_run_at = None
                        check.reason = (
                            "Linked job is no longer registered; choose a job before resuming"
                            if job_missing
                            else "Branch is no longer active; checks paused"
                        )
                        if not check.active_run_id:
                            check.phase = "paused"
                        self._notify(
                            session,
                            check,
                            "error",
                            check.reason,
                            f"unavailable:{check.id}:{check.revision}",
                        )
                        changed.add(check.branch_id)
                    reason = self._stop_reason(session, check, now)
                    if reason and check.status == "active":
                        check.status = "completed"
                        check.next_run_at = None
                        check.reason = reason
                        if not check.active_run_id:
                            check.phase = "completed"
                        self._notify(
                            session,
                            check,
                            "schedule_ended",
                            reason,
                            f"stop:{check.id}:{check.revision}",
                        )
                        changed.add(check.branch_id)
                if not check.active_run_id:
                    continue
                run = session.get(models.ScheduledCheckRun, check.active_run_id)
                if run is None:
                    check.active_run_id = None
                    check.status = check.phase = "paused"
                    check.reason = "Missing check run; review before resuming"
                    changed.add(check.branch_id)
                    continue
                message = (
                    session.get(models.ConversationMessage, run.message_id)
                    if (run.message_id)
                    else None
                )
                if message is None:
                    if now - utc(run.created_at) > timedelta(minutes=5):
                        self._finish(
                            session,
                            check,
                            run,
                            "error",
                            "Check dispatch was not confirmed; review before resuming",
                            pause=True,
                        )
                        changed.add(check.branch_id)
                    continue
                send_status = message.metadata_json.get("send_status")
                if send_status in {"failed", "interrupted"}:
                    self._finish(
                        session,
                        check,
                        run,
                        "interrupted" if send_status == "interrupted" else "error",
                        str(message.metadata_json.get("error") or send_status),
                        pause=send_status == "interrupted",
                    )
                    changed.add(check.branch_id)
                    continue
                if send_status != "submitted":
                    continue
                answer = self._answer(session, message, run)
                if answer:
                    self._finish(session, check, run, check_outcome(answer), answer)
                    changed.add(check.branch_id)
                elif now - utc(message.updated_at) > timedelta(minutes=5):
                    self._finish(
                        session,
                        check,
                        run,
                        "error",
                        "No final answer was recorded; review before resuming",
                        pause=True,
                    )
                    changed.add(check.branch_id)
            session.commit()
        return sorted(changed)

    @staticmethod
    def _answer(
        session: Session, message: models.ConversationMessage, run: models.ScheduledCheckRun
    ) -> str | None:
        turn = session.scalar(
            select(models.CodexTurn)
            .where(
                models.CodexTurn.branch_id == message.branch_id,
                models.CodexTurn.metadata_json["scheduled_check_run_id"].as_string() == run.id,
            )
            .limit(1)
        )
        if turn:
            return turn.final_answer
        if message.turn_id:
            answer = session.scalar(
                select(models.ConversationMessage.content)
                .where(
                    models.ConversationMessage.branch_id == message.branch_id,
                    models.ConversationMessage.turn_id == message.turn_id,
                    models.ConversationMessage.role == "assistant",
                    models.ConversationMessage.created_at >= message.created_at,
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            return answer or None
        return None

    def _stop_reason(
        self, session: Session, check: models.ScheduledCheck, now: datetime
    ) -> str | None:
        if check.stop_mode == "deadline" and check.stop_at and utc(check.stop_at) <= now:
            return "已到设定的停止时间，后续检查已停止。外部任务不受影响。"
        if check.stop_mode == "job":
            job = session.get(models.ExternalJob, check.job_id) if check.job_id else None
            if job is None:
                return None
            status, observed = job.status, job.last_observed_at
            if job.provider == "ltp":
                observation = session.scalar(
                    select(models.LtpMonitoredJob)
                    .where(
                        models.LtpMonitoredJob.job_name == job.remote_job_id,
                        models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users),
                    )
                    .order_by(models.LtpMonitoredJob.last_observed_at.desc())
                    .limit(1)
                )
                if observation and (
                    not observed or utc(observation.last_observed_at) > utc(observed)
                ):
                    status, observed = observation.state, observation.last_observed_at
            if (
                observed
                and (now - utc(observed) < timedelta(minutes=15))
                and (
                    external_job_phase(status) in {"completed", "failed", "cancelled"}
                    or status.casefold() == "terminated"
                )
            ):
                return f"任务 {job.remote_job_id} 状态为 {status}，后续检查已停止。"
        return None

    def fail_dispatch(self, run_id: str, error: str) -> None:
        with self.factory() as session:
            run = session.get(models.ScheduledCheckRun, run_id)
            if run is None or run.status != "claimed":
                return
            check = session.get(models.ScheduledCheck, run.check_id)
            assert check
            self._finish(session, check, run, "error", error)
            session.commit()

    def _finish(
        self,
        session: Session,
        check: models.ScheduledCheck,
        run: models.ScheduledCheckRun,
        outcome: str,
        summary: str,
        *,
        pause: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        run.status = (
            "failed"
            if outcome == "error"
            else ("interrupted" if outcome == "interrupted" else "completed")
        )
        run.outcome = outcome
        run.summary = redact(summary)[:12000]
        run.completed_at = now
        check.active_run_id = None
        check.last_completed_at = now
        check.last_outcome = outcome
        check.last_summary = run.summary
        check.consecutive_errors = check.consecutive_errors + 1 if outcome == "error" else 0
        if check.status == "active":
            if pause or check.consecutive_errors >= 3:
                check.status = check.phase = "paused"
                check.next_run_at = None
                check.reason = "Checks paused; review the last result before resuming"
            else:
                check.phase = "retry_scheduled" if outcome == "error" else "scheduled"
                check.reason = run.summary[:1000] if outcome == "error" else None
                delay = check.interval_minutes * min(2**check.consecutive_errors, 8)
                check.next_run_at = now + timedelta(minutes=delay)
        else:
            check.phase = check.status
        self._notify(session, check, outcome, run.summary, f"run:{run.id}")

    def _notify(
        self, session: Session, check: models.ScheduledCheck, outcome: str, summary: str, key: str
    ) -> None:
        if check.notification_mode == "none" or not self.settings.infoflow_notifications_enabled:
            return
        fingerprint = hashlib.sha256(
            f"{outcome}:{check.status}:{' '.join(summary.split())}".encode()
        ).hexdigest()
        if check.notification_mode == "important" and (
            outcome == "unchanged" or fingerprint == check.last_notice_fingerprint
        ):
            return
        if session.scalar(
            select(models.ScheduledCheckNotification.id).where(
                models.ScheduledCheckNotification.event_key == key,
            )
        ):
            return
        session.add(
            models.ScheduledCheckNotification(
                branch_id=check.branch_id,
                event_key=key,
                kind=outcome,
                recipient=self.settings.infoflow_recipient,
                payload={
                    "summary": redact(summary)[:4000],
                    "check_status": check.status,
                    "execution": check.execution,
                },
            )
        )
        check.last_notice_fingerprint = fingerprint


class ScheduledCheckWorker:
    def __init__(
        self, checks: ScheduledCheckService, branches: ResearchOpsService, events: EventHub
    ) -> None:
        self.checks = checks
        self.branches = branches
        self.events = events
        self.started_at = datetime.now(UTC)
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        enabled = self.checks.settings.scheduled_checks_enabled
        stale_after = max(60.0, self.checks.settings.scheduled_check_tick_seconds * 3)
        elapsed = (now - utc(self.last_tick_at or self.started_at)).total_seconds()
        if not enabled:
            status = "disabled"
        elif self.last_error:
            status = "error"
        elif elapsed > stale_after:
            status = "stale"
        else:
            status = "healthy" if self.last_tick_at else "starting"
        return {
            "enabled": enabled,
            "status": status,
            "checked_at": now.isoformat(),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "stale_after_seconds": stale_after,
            "error": self.last_error,
        }

    async def tick(self) -> None:
        changed = await run_db(self.checks.reconcile)
        for branch_id in changed:
            await self.publish(branch_id)
        for run_id in await run_db(self.checks.claim_due):
            task = asyncio.create_task(self._start(run_id), name=f"scheduled-check:{run_id}")
            self.tasks[run_id] = task
            task.add_done_callback(lambda done, key=run_id: self._done(key, done))
        self.last_tick_at = datetime.now(UTC)
        self.last_error = None

    def _done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self.tasks.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()):
            self.last_error = type(error).__name__
            logger.error("Scheduled check dispatch failed: %s", redact(str(error))[:2000])

    async def _start(self, run_id: str) -> None:
        check, run = await run_db(self.checks.claimed, run_id)
        try:
            state = await run_db(self.branches.branch_conversation_state, check.branch_id)
            if not state["can_send"]:
                await run_db(self.checks.defer, run_id, state["reason"] or "Session is busy")
                return
            await self.branches.queue_web_message(
                check.branch_id,
                schemas.ConversationSend(content=run.prompt),
                metadata={
                    "scheduled_check_id": check.id,
                    "scheduled_check_run_id": run.id,
                    "execution": run.execution,
                },
                supersede_pending=False,
            )
        except ConflictError as exc:
            await run_db(self.checks.defer, run_id, str(exc))
        except asyncio.CancelledError:
            # Never replay an ambiguous accepted request after restart.
            raise
        except Exception as exc:
            await run_db(self.checks.fail_dispatch, run_id, redact(str(exc))[:2000])
        finally:
            await self.publish(check.branch_id)

    async def publish(self, branch_id: str) -> None:
        await self.events.publish(branch_id, {"type": "scheduled_check.updated"})

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
