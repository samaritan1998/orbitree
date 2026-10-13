from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.codex.reflection import ReflectionAgent
from research_ops.infoflow import InfoflowSendResult
from research_ops.services.codex_events import redact, sanitize
from research_ops.settings import Settings


class ReflectionSender(Protocol):
    @property
    def configured(self) -> bool: ...

    def send_text(self, recipient: str, content: str) -> InfoflowSendResult: ...


class DailyReflectionService:
    """Collect one day of evidence, run a bounded analyst, and retain its report."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        agent: ReflectionAgent,
        sender: ReflectionSender,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.agent = agent
        self.sender = sender
        self.timezone = ZoneInfo(settings.daily_reflection_timezone)
        self._run_lock = asyncio.Lock()

    def list(self, limit: int = 31) -> list[models.DailyReflection]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(models.DailyReflection)
                    .order_by(models.DailyReflection.reflection_date.desc())
                    .limit(max(1, min(limit, 366)))
                )
            )

    def target_date(self, now: datetime | None = None) -> date:
        current = _as_utc(now or datetime.now(UTC)).astimezone(self.timezone)
        return current.date() - timedelta(days=1)

    def queue(
        self,
        reflection_date: date | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> models.DailyReflection:
        current = _as_utc(now or datetime.now(UTC))
        day = reflection_date or self.target_date(current)
        if day >= current.astimezone(self.timezone).date():
            raise ValueError("Daily reflection requires a completed local calendar day")
        start, end = reflection_window(day, self.timezone)
        with self.session_factory() as session:
            reflection = session.scalar(
                select(models.DailyReflection).where(
                    models.DailyReflection.reflection_date == day,
                    models.DailyReflection.timezone == self.settings.daily_reflection_timezone,
                )
            )
            if reflection is None:
                reflection = models.DailyReflection(
                    reflection_date=day,
                    timezone=self.settings.daily_reflection_timezone,
                    status="queued",
                    source_started_at=start,
                    source_ended_at=end,
                    notification_status="pending" if self._notification_enabled else "disabled",
                )
                session.add(reflection)
            elif force:
                if reflection.status == "running":
                    raise ValueError("Daily reflection is already running")
                notification_was_sent = reflection.notification_status == "sent"
                reflection.status = "queued"
                reflection.next_attempt_at = None
                reflection.error = None
                if not notification_was_sent:
                    reflection.notification_status = (
                        "pending" if self._notification_enabled else "disabled"
                    )
                    reflection.notification_next_attempt_at = None
                    reflection.notification_error = None
            elif reflection.status == "running":
                stale_after = timedelta(
                    seconds=max(1_800, self.settings.daily_reflection_timeout_seconds * 2)
                )
                if reflection.started_at and _as_utc(reflection.started_at) < current - stale_after:
                    reflection.status = "failed"
                    reflection.error = "Reflection worker stopped before completing"
                    reflection.next_attempt_at = current
            session.commit()
            return reflection

    async def run_due(self, now: datetime | None = None) -> models.DailyReflection | None:
        current = _as_utc(now or datetime.now(UTC))
        local = current.astimezone(self.timezone)
        scheduled = time(
            self.settings.daily_reflection_hour,
            self.settings.daily_reflection_minute,
        )
        if local.time() < scheduled:
            return None
        reflection = self.queue(self.target_date(current), now=current)
        if reflection.status == "completed":
            return reflection
        if reflection.next_attempt_at and _as_utc(reflection.next_attempt_at) > current:
            return reflection
        return await self.run(reflection.id)

    async def run(self, reflection_id: str) -> models.DailyReflection:
        async with self._run_lock:
            now = datetime.now(UTC)
            with self.session_factory() as session:
                reflection = session.get(models.DailyReflection, reflection_id)
                if reflection is None:
                    raise LookupError("Daily reflection not found")
                if reflection.status == "completed":
                    return reflection
                if reflection.status == "running" and reflection.started_at:
                    return reflection
                reflection.status = "running"
                reflection.attempts += 1
                reflection.started_at = now
                reflection.completed_at = None
                reflection.error = None
                reflection.next_attempt_at = None
                session.commit()
                day = reflection.reflection_date
                notification_was_sent = reflection.notification_status == "sent"
            try:
                evidence = await asyncio.to_thread(self._collect_evidence, day)
                with self.session_factory() as session:
                    reflection = session.get(models.DailyReflection, reflection_id)
                    assert reflection is not None
                    reflection.source_counts = evidence["coverage"]
                    reflection.evidence_refs = evidence["evidence_refs"]
                    session.commit()
                report = await self.agent.analyze(evidence)
                report = _filter_report_evidence(report, set(evidence["evidence_refs"]))
                markdown = render_report(day, report)
                with self.session_factory() as session:
                    reflection = session.get(models.DailyReflection, reflection_id)
                    assert reflection is not None
                    reflection.status = "completed"
                    reflection.source_counts = evidence["coverage"]
                    reflection.evidence_refs = evidence["evidence_refs"]
                    reflection.report_json = report.model_dump(mode="json")
                    reflection.report_markdown = markdown
                    reflection.agent_model = self.settings.codex_model
                    reflection.agent_provider = self.settings.codex_model_provider
                    reflection.completed_at = datetime.now(UTC)
                    reflection.next_attempt_at = None
                    reflection.error = None
                    reflection.notification_status = (
                        "sent"
                        if notification_was_sent
                        else "pending"
                        if self._notification_enabled
                        else "disabled"
                    )
                    session.add(
                        models.ActivityEvent(
                            scope_type="daily_reflection",
                            scope_id=reflection.id,
                            event_type="daily_reflection.completed",
                            actor="reflection-agent",
                            correlation_id=reflection.id,
                            payload={
                                "reflection_date": day.isoformat(),
                                "records": len(evidence["records"]),
                            },
                        )
                    )
                    session.commit()
                    return reflection
            except asyncio.CancelledError:
                self._fail(reflection_id, "Reflection worker was cancelled")
                raise
            except Exception as exc:
                return self._fail(reflection_id, redact(str(exc) or type(exc).__name__))

    def dispatch_notifications(self) -> list[str]:
        if not self._notification_enabled or not self.sender.configured:
            return []
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=10)
        with self.session_factory() as session:
            stale = list(
                session.scalars(
                    select(models.DailyReflection).where(
                        models.DailyReflection.notification_status == "sending",
                        models.DailyReflection.updated_at < cutoff,
                    )
                )
            )
            for reflection in stale:
                reflection.notification_status = "failed"
                reflection.notification_error = "Notification worker stopped before confirming"
                reflection.notification_next_attempt_at = now
            due_ids = list(
                session.scalars(
                    select(models.DailyReflection.id)
                    .where(
                        models.DailyReflection.status == "completed",
                        models.DailyReflection.notification_status.in_(("pending", "failed")),
                        or_(
                            models.DailyReflection.notification_next_attempt_at.is_(None),
                            models.DailyReflection.notification_next_attempt_at <= now,
                        ),
                    )
                    .order_by(models.DailyReflection.reflection_date)
                    .limit(self.settings.infoflow_dispatch_batch_size)
                )
            )
            if stale:
                session.commit()
        delivered: list[str] = []
        for reflection_id in due_ids:
            reflection = self._claim_notification(reflection_id)
            if reflection is None:
                continue
            try:
                result = self.sender.send_text(
                    self.settings.infoflow_recipient,
                    self._notification_message(reflection),
                )
            except Exception as exc:
                self._fail_notification(reflection_id, redact(str(exc) or type(exc).__name__))
                continue
            with self.session_factory() as session:
                stored = session.get(models.DailyReflection, reflection_id)
                if stored is None:
                    continue
                stored.notification_status = "sent"
                stored.notification_sent_at = datetime.now(UTC)
                stored.notification_message_id = result.message_id
                stored.notification_error = None
                stored.notification_next_attempt_at = None
                session.add(
                    models.ActivityEvent(
                        scope_type="daily_reflection",
                        scope_id=stored.id,
                        event_type="daily_reflection.notification_sent",
                        actor="infoflow",
                        correlation_id=stored.id,
                        payload={"recipient": self.settings.infoflow_recipient},
                    )
                )
                session.commit()
            delivered.append(reflection_id)
        return delivered

    @property
    def _notification_enabled(self) -> bool:
        return bool(
            self.settings.daily_reflection_infoflow_enabled
            and self.settings.infoflow_notifications_enabled
        )

    def _collect_evidence(self, day: date) -> dict[str, Any]:
        start, end = reflection_window(day, self.timezone)
        records: list[dict[str, Any]] = []
        raw_counts: defaultdict[str, int] = defaultdict(int)
        item_limit = self.settings.daily_reflection_item_chars

        def add(
            kind: str,
            identity: tuple[str, str, str, str, str, str] | None,
            source_id: str,
            occurred_at: datetime,
            summary: str,
            details: str = "",
            priority: int = 50,
        ) -> None:
            program, work_id, work_title, branch_id, branch_name, scope = identity or (
                "",
                "",
                "",
                "",
                "",
                "system",
            )
            records.append(
                {
                    "evidence_id": f"{kind}:{source_id}",
                    "kind": kind,
                    "occurred_at": _as_utc(occurred_at).isoformat(),
                    "program": program,
                    "work_id": work_id,
                    "work_title": work_title,
                    "branch_id": branch_id,
                    "branch_name": branch_name,
                    "scope": scope,
                    "summary": _clip(summary, item_limit),
                    "details": _clip(details, item_limit),
                    "_priority": priority,
                }
            )

        with self.session_factory() as session:
            identity_rows = session.execute(
                select(models.ExecutionBranch, models.WorkItem, models.Program)
                .join(models.WorkItem, models.ExecutionBranch.work_item_id == models.WorkItem.id)
                .join(models.Program, models.WorkItem.program_id == models.Program.id)
            ).all()
            identities = {
                branch.id: (
                    program.code,
                    work.human_id,
                    work.title,
                    branch.id,
                    branch.name,
                    "branch",
                )
                for branch, work, program in identity_rows
            }
            work_identities = {
                work.id: (
                    program.code,
                    work.human_id,
                    work.title,
                    "",
                    "",
                    "work",
                )
                for _branch, work, program in identity_rows
            }

            turn_rows = session.execute(
                select(models.CodexTurn)
                .where(
                    models.CodexTurn.completed_at >= start,
                    models.CodexTurn.completed_at < end,
                )
                .order_by(models.CodexTurn.completed_at)
            ).scalars().all()
            raw_counts["completed_turns"] = len(turn_rows)
            for turn in turn_rows:
                add(
                    "turn",
                    identities.get(turn.branch_id),
                    turn.id,
                    turn.completed_at,
                    f"User request: {_clip(turn.prompt or '(prompt unavailable)', 900)}",
                    f"Final result: {_clip(turn.final_answer, item_limit)}; status={turn.status}",
                    100,
                )

            message_rows = session.execute(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.created_at >= start,
                    models.ConversationMessage.created_at < end,
                    models.ConversationMessage.role.in_(("user", "assistant")),
                )
                .order_by(models.ConversationMessage.created_at)
            ).scalars().all()
            raw_counts["conversation_messages"] = len(message_rows)
            assistant_by_branch: defaultdict[
                str, list[models.ConversationMessage]
            ] = defaultdict(list)
            for message in message_rows:
                if message.role == "assistant":
                    assistant_by_branch[message.branch_id].append(message)
                    continue
                status = str(message.metadata_json.get("send_status") or "recorded")
                add(
                    "message",
                    identities.get(message.branch_id),
                    message.id,
                    message.created_at,
                    f"User request ({status}): {message.content}",
                    str(message.metadata_json.get("error") or ""),
                    85 if status in {"failed", "interrupted"} else 70,
                )
            for branch_messages in assistant_by_branch.values():
                for message in branch_messages[-3:]:
                    add(
                        "update",
                        identities.get(message.branch_id),
                        message.id,
                        message.created_at,
                        "Latest in-progress assistant update",
                        message.content,
                        45,
                    )

            session_rows = session.execute(
                select(models.SessionAttempt).where(
                    or_(
                        (
                            (models.SessionAttempt.started_at >= start)
                            & (models.SessionAttempt.started_at < end)
                        ),
                        (
                            (models.SessionAttempt.completed_at >= start)
                            & (models.SessionAttempt.completed_at < end)
                        ),
                    )
                )
            ).scalars().all()
            raw_counts["sessions"] = len(session_rows)
            for attempt in session_rows:
                if not attempt.error and not attempt.handoff:
                    continue
                add(
                    "session",
                    identities.get(attempt.branch_id),
                    attempt.id,
                    attempt.completed_at or attempt.started_at or attempt.created_at,
                    f"Session ended with status={attempt.status}",
                    _compact_json({"error": attempt.error, "handoff": attempt.handoff}),
                    90 if attempt.error else 75,
                )

            job_rows = session.execute(
                select(models.ExternalJob).where(
                    or_(
                        (
                            (models.ExternalJob.created_at >= start)
                            & (models.ExternalJob.created_at < end)
                        ),
                        (
                            (models.ExternalJob.updated_at >= start)
                            & (models.ExternalJob.updated_at < end)
                        ),
                        (
                            (models.ExternalJob.last_observed_at >= start)
                            & (models.ExternalJob.last_observed_at < end)
                        ),
                    )
                )
            ).scalars().all()
            raw_counts["external_jobs"] = len(job_rows)
            for job in job_rows:
                add(
                    "job",
                    work_identities.get(job.work_item_id),
                    job.id,
                    job.last_observed_at or job.updated_at,
                    f"{job.provider} job {job.remote_job_id}: {job.status}",
                    _compact_json(job.metadata_json),
                    90,
                )

            result_rows = session.execute(
                select(models.BranchResult).where(
                    models.BranchResult.created_at >= start,
                    models.BranchResult.created_at < end,
                )
            ).scalars().all()
            raw_counts["branch_results"] = len(result_rows)
            for result in result_rows:
                add(
                    "result",
                    identities.get(result.branch_id),
                    result.id,
                    result.created_at,
                    f"Branch result: {result.outcome}",
                    _compact_json(
                        {
                            "conclusion": result.conclusion,
                            "acceptance_results": result.acceptance_results,
                            "next_action": result.next_action,
                        }
                    ),
                    100,
                )

            memory_rows = session.execute(
                select(models.MemoryEntry).where(
                    or_(
                        (
                            (models.MemoryEntry.created_at >= start)
                            & (models.MemoryEntry.created_at < end)
                        ),
                        (
                            (models.MemoryEntry.updated_at >= start)
                            & (models.MemoryEntry.updated_at < end)
                        ),
                    )
                )
            ).scalars().all()
            raw_counts["memory_entries"] = len(memory_rows)
            for memory in memory_rows:
                add(
                    "memory",
                    identities.get(memory.source_branch_id or ""),
                    memory.id,
                    memory.updated_at,
                    f"Memory {memory.status}/{memory.kind}: {memory.title}",
                    memory.content,
                    80,
                )

            event_rows = session.execute(
                select(models.ActivityEvent)
                .where(
                    models.ActivityEvent.created_at >= start,
                    models.ActivityEvent.created_at < end,
                    ~models.ActivityEvent.event_type.in_(
                        (
                            "conversation.user_message",
                            "codex.turn_completed",
                            "session.starting",
                            "session.started",
                            "notification.infoflow.sent",
                        )
                    ),
                )
                .order_by(models.ActivityEvent.created_at.desc())
                .limit(180)
            ).scalars().all()
            raw_counts["activity_events"] = len(event_rows)
            for event in reversed(event_rows):
                identity = identities.get(event.scope_id) if event.scope_type == "branch" else None
                add(
                    "event",
                    identity,
                    event.id,
                    event.created_at,
                    event.event_type,
                    _compact_json(event.payload),
                    85 if "error" in event.event_type or "failed" in event.event_type else 40,
                )

            window_rows = session.execute(
                select(models.CodexWindow).where(
                    models.CodexWindow.last_activity_at >= start,
                    models.CodexWindow.last_activity_at < end,
                    models.CodexWindow.ignored.is_(False),
                )
            ).scalars().all()
            raw_counts["codex_windows"] = len(window_rows)
            unassigned = 0
            for window in window_rows:
                if window.branch_id is None:
                    unassigned += 1
                add(
                    "window",
                    identities.get(window.branch_id or ""),
                    window.id,
                    window.last_activity_at,
                    f"Codex window: {window.display_name or window.thread_id}",
                    f"managed={window.branch_id is not None}; live={window.live}; cwd={window.cwd}",
                    35,
                )
            raw_counts["unassigned_windows"] = unassigned

        ordered = sorted(
            records,
            key=lambda item: (-int(item["_priority"]), item["occurred_at"]),
        )
        selected: list[dict[str, Any]] = []
        total_chars = 0
        for record in ordered:
            clean = {key: value for key, value in record.items() if key != "_priority"}
            size = len(json.dumps(clean, ensure_ascii=False))
            if len(selected) >= self.settings.daily_reflection_max_records:
                break
            if selected and total_chars + size > self.settings.daily_reflection_max_prompt_chars:
                continue
            selected.append(clean)
            total_chars += size
        selected.sort(key=lambda item: item["occurred_at"])
        programs = sorted({str(item["program"]) for item in selected if item["program"]})
        work_items = sorted({str(item["work_id"]) for item in selected if item["work_id"]})
        coverage = {
            **raw_counts,
            "included_records": len(selected),
            "omitted_records": max(0, len(records) - len(selected)),
            "included_characters": total_chars,
            "programs": programs,
            "work_items": work_items,
            "work_item_titles": [
                {
                    "id": work_id,
                    "title": next(
                        str(item["work_title"])
                        for item in selected
                        if item["work_id"] == work_id
                    ),
                    "program": next(
                        str(item["program"])
                        for item in selected
                        if item["work_id"] == work_id
                    ),
                }
                for work_id in work_items
            ],
            "limitations": [
                "Only activity registered or discovered by Research Ops is visible.",
                "Unassigned Codex windows contribute identity/activity metadata, not full turns.",
                "External systems are represented only by their registered job snapshots.",
            ],
        }
        return {
            "schema_version": "1.0",
            "reflection_date": day.isoformat(),
            "timezone": self.settings.daily_reflection_timezone,
            "window_utc": {"start": start.isoformat(), "end": end.isoformat()},
            "coverage": coverage,
            "evidence_refs": [item["evidence_id"] for item in selected],
            "records": selected,
        }

    def _fail(self, reflection_id: str, error: str) -> models.DailyReflection:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            reflection = session.get(models.DailyReflection, reflection_id)
            if reflection is None:
                raise LookupError("Daily reflection not found")
            delay = min(3_600, 60 * (2 ** min(max(0, reflection.attempts - 1), 5)))
            reflection.status = "failed"
            reflection.error = error[:4_000]
            reflection.completed_at = now
            reflection.next_attempt_at = now + timedelta(seconds=delay)
            session.add(
                models.ActivityEvent(
                    scope_type="daily_reflection",
                    scope_id=reflection.id,
                    event_type="daily_reflection.failed",
                    actor="reflection-agent",
                    correlation_id=reflection.id,
                    payload={"error": error[:1_000]},
                )
            )
            session.commit()
            return reflection

    def _claim_notification(self, reflection_id: str) -> models.DailyReflection | None:
        with self.session_factory() as session:
            reflection = session.get(models.DailyReflection, reflection_id)
            if reflection is None or reflection.notification_status not in {"pending", "failed"}:
                return None
            reflection.notification_status = "sending"
            reflection.notification_attempts += 1
            reflection.notification_next_attempt_at = None
            session.commit()
            return reflection

    def _notification_message(self, reflection: models.DailyReflection) -> str:
        report = schemas.DailyReflectionReport.model_validate(reflection.report_json)
        tasks = "；".join(
            f"{item.work_id} {item.task_title}" for item in report.workstreams[:5]
        ) or "当天没有可归属的任务活动"
        shortcoming = (
            report.shortcomings[0].issue
            if report.shortcomings
            else "未发现有充分证据的问题"
        )
        next_action = (
            report.tomorrow_focus[0].action
            if report.tomorrow_focus
            else "查看完整复盘后确定"
        )
        url = f"{self.settings.infoflow_public_base_url.rstrip('/')}/memory"
        return "\n".join(
            (
                "Research Ops · 每日总结与反思",
                f"日期：{reflection.reflection_date.isoformat()}",
                f"任务：{_clip(tasks, 800)}",
                f"总结：{_clip(report.executive_summary, 600)}",
                f"首要不足：{_clip(shortcoming, 300)}",
                f"下一步：{_clip(next_action, 300)}",
                f"查看：{url}",
            )
        )

    def _fail_notification(self, reflection_id: str, error: str) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            reflection = session.get(models.DailyReflection, reflection_id)
            if reflection is None:
                return
            delay = min(
                3_600,
                30 * (2 ** min(max(0, reflection.notification_attempts - 1), 7)),
            )
            reflection.notification_status = "failed"
            reflection.notification_error = error[:1_000]
            reflection.notification_next_attempt_at = now + timedelta(seconds=delay)
            session.commit()


def reflection_window(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone).astimezone(UTC)
    return start, end


def render_report(day: date, report: schemas.DailyReflectionReport) -> str:
    lines = [f"# {day.isoformat()} 每日总结与反思", "", report.executive_summary]

    def section(title: str, items: list[tuple[str, str, list[str]]]) -> None:
        lines.extend(("", f"## {title}", ""))
        if not items:
            lines.append("- 暂无有充分证据的内容。")
            return
        for heading, body, refs in items:
            evidence = " ".join(f"`{ref}`" for ref in refs) or "`inference`"
            lines.append(f"### {heading}")
            lines.append("")
            lines.append(body)
            lines.append("")
            lines.append(f"证据：{evidence}")
            lines.append("")

    state_labels = {
        "completed": "已完成",
        "in_progress": "进行中",
        "waiting_external": "等待外部结果",
        "blocked": "阻塞",
        "failed": "失败",
        "unclear": "状态不明确",
    }
    section(
        "任务总览",
        [
            (
                f"{item.work_id} · {item.task_title}",
                f"方向：{item.program}\n\n状态：{state_labels[item.state]}\n\n"
                f"{item.summary}\n\n下一步：{item.next_action}",
                item.evidence_refs,
            )
            for item in report.workstreams
        ],
    )
    section(
        "今日完成",
        [
            (item.title, f"{item.outcome}\n\n价值：{item.significance}", item.evidence_refs)
            for item in report.accomplishments
        ],
    )
    section(
        "经验沉淀候选",
        [
            (
                item.title,
                f"{item.insight}\n\nSkill 候选：{'是' if item.reusable_as_skill else '否'}",
                item.evidence_refs,
            )
            for item in report.lessons
        ],
    )
    section(
        "不足与反思",
        [
            (item.issue, f"证据判断：{item.evidence}\n\n影响：{item.impact}", item.evidence_refs)
            for item in report.shortcomings
        ],
    )
    section(
        "优化动作",
        [
            (
                f"{item.priority} · {item.action}",
                f"原因：{item.rationale}\n\n验收信号：{item.success_signal}",
                item.evidence_refs,
            )
            for item in report.optimizations
        ],
    )
    section(
        "明日焦点",
        [
            (item.action, f"验收信号：{item.success_signal}", item.evidence_refs)
            for item in report.tomorrow_focus
        ],
    )
    lines.extend(("", "## 覆盖说明", ""))
    lines.extend(f"- {item}" for item in report.coverage_notes)
    return "\n".join(lines).strip() + "\n"


def _filter_report_evidence(
    report: schemas.DailyReflectionReport,
    valid_refs: set[str],
) -> schemas.DailyReflectionReport:
    payload = report.model_dump(mode="python")
    for section in (
        "workstreams",
        "accomplishments",
        "lessons",
        "shortcomings",
        "optimizations",
        "tomorrow_focus",
    ):
        for item in payload[section]:
            item["evidence_refs"] = [
                ref for ref in item.get("evidence_refs", []) if ref in valid_refs
            ]
    return schemas.DailyReflectionReport.model_validate(payload)


def _clip(value: str, limit: int) -> str:
    normalized = redact(" ".join(str(value or "").split()))
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _compact_json(value: Any) -> str:
    return redact(
        json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"), default=str)
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
