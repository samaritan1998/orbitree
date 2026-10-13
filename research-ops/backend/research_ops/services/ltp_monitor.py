"""Independent, read-only LTP task monitoring and group-notification outbox.

The monitor deliberately talks to ``ltp-cli`` rather than Cluster Control.
Cluster Control is an aggregate scheduling dashboard; LTP CLI is the source of
truth for a user's exact lifecycle status.  The monitor never submits,
resubmits, deletes, or otherwise mutates a remote task.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.infoflow import InfoflowDeliveryBlockedError, InfoflowSendResult
from research_ops.services.codex_events import redact
from research_ops.settings import Settings

TERMINAL_STATES = frozenset({"failed", "completed", "cancelled"})
_STATUS_LINE = re.compile(r"(?im)(?:^|\b)(?:status|state)\s*[:=]\s*([^\r\n]+)")
_TABLE_DIVIDER = re.compile(r"^[\s+|│┌┐└┘├┤┬┴┼─━-]+$")


class LtpMonitorSourceError(RuntimeError):
    """A sanitized read-only LTP CLI source failure."""


@dataclass(frozen=True)
class LtpCliJob:
    user: str
    job_name: str
    cluster: str | None
    state: str
    provider_status: str | None
    state_group: str | None
    category: str | None
    metadata_json: dict[str, Any]
    source: str


class GroupTextSender(Protocol):
    @property
    def configured(self) -> bool: ...

    def send_group_text(self, group_id: str, content: str) -> InfoflowSendResult: ...


class LtpCliTaskSource:
    """Bounded ``ltp-cli list/show`` adapter with tolerant output parsing."""

    def __init__(
        self,
        *,
        command: str,
        api_url: str | None,
        timeout_seconds: float,
        runner: Any = subprocess.run,
    ) -> None:
        self.command = command
        self.api_url = api_url.strip() if api_url else None
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def list_jobs(self, user: str) -> list[LtpCliJob]:
        output = self._run("list", "--user", user, "--refresh")
        records = _parse_list_output(output)
        return [
            # ltp-cli list output is an identity inventory, not a trustworthy
            # lifecycle feed. The same CLI's show command supplies status.
            _job_from_record(
                record,
                default_user=user,
                source="ltp_cli_list",
                trust_status=False,
            )
            for record in records
        ]

    def show_job(self, user: str, job_name: str) -> LtpCliJob | None:
        try:
            output = self._run("show", job_name, "--user", user)
        except LtpMonitorSourceError as exc:
            if _looks_not_found(str(exc)):
                return None
            raise
        record = _parse_show_output(output)
        if record is None:
            return None
        return _job_from_record(
            record,
            default_user=user,
            default_job_name=job_name,
            source="ltp_cli_show",
        )

    def _run(self, *args: str) -> str:
        environment = os.environ.copy()
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            environment.pop(key, None)
        argv = [self.command]
        if self.api_url:
            argv.extend(("--api-url", self.api_url))
        argv.extend(args)
        try:
            completed = self.runner(
                argv,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise LtpMonitorSourceError("LTP CLI 查询不可用") from exc
        output = "\n".join(
            value.strip()
            for value in (completed.stdout or "", completed.stderr or "")
            if value.strip()
        )
        if completed.returncode != 0:
            raise LtpMonitorSourceError(_safe_cli_error(output) or "LTP CLI 查询失败")
        if _looks_cli_failure(output):
            raise LtpMonitorSourceError(_safe_cli_error(output) or "LTP CLI 查询失败")
        return output


@dataclass(frozen=True)
class LtpMonitorDelivery:
    notification_id: str
    status: str
    error: str | None = None


class LtpMonitorService:
    """Persist ltp-cli state transitions and deliver configured group alerts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        source: LtpCliTaskSource,
        sender: GroupTextSender,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.source = source
        self.sender = sender

    def sync(self) -> dict[str, int]:
        if not self.settings.ltp_monitor_enabled:
            return {"observed": 0, "transitions": 0, "errors": 0}

        now = datetime.now(UTC)
        discovered: list[LtpCliJob] = []
        errors: list[str] = []
        for user in self.settings.ltp_monitor_users:
            try:
                discovered.extend(self.source.list_jobs(user))
            except LtpMonitorSourceError as exc:
                errors.append(f"{user}: {str(exc)}")

        if errors:
            self._record_sync_failure(now, errors)
            transitions = self._probe_unresolved_jobs(set(), now)
            return {"observed": 0, "transitions": transitions, "errors": len(errors)}

        # A user can have at most one current LTP identity with a given name.
        current = {(job.user, job.job_name): job for job in discovered}
        current = dict(list(current.items())[: self.settings.ltp_monitor_max_jobs])
        transitions = self._persist_list(current, now)
        transitions += self._probe_unresolved_jobs(set(current), now)
        return {"observed": len(current), "transitions": transitions, "errors": 0}

    def track_job(
        self,
        *,
        job_name: str,
        user: str | None = None,
        cluster: str | None = None,
    ) -> models.LtpMonitoredJob:
        """Register one known LTP identity when list discovery is unavailable."""
        selected_user = (user or self.settings.ltp_monitor_users[0]).strip()
        normalized_name = job_name.strip()
        if selected_user not in self.settings.ltp_monitor_users:
            raise ValueError("LTP monitor only tracks configured users")
        if not normalized_name or any(character.isspace() for character in normalized_name):
            raise ValueError("LTP job name must be one non-empty value")
        now = datetime.now(UTC)
        with self.session_factory() as session:
            job = session.scalar(
                select(models.LtpMonitoredJob).where(
                    models.LtpMonitoredJob.user == selected_user,
                    models.LtpMonitoredJob.job_name == normalized_name,
                )
            )
            if job is None:
                job = models.LtpMonitoredJob(
                    user=selected_user,
                    job_name=normalized_name,
                    cluster=cluster.strip() if cluster else None,
                    state="unknown",
                    first_observed_at=now,
                    last_observed_at=now,
                    last_status_at=now,
                    metadata_json={"origin": "manual"},
                )
                session.add(job)
            elif cluster and cluster.strip():
                job.cluster = cluster.strip()
            session.commit()
            return job

    def overview(self, *, job_limit: int = 200, event_limit: int = 200) -> dict[str, Any]:
        with self.session_factory() as session:
            state = session.get(models.LtpMonitorState, "default")
            jobs = list(
                session.scalars(
                    select(models.LtpMonitoredJob)
                    .where(models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users))
                    .order_by(
                        models.LtpMonitoredJob.state.in_(TERMINAL_STATES),
                        models.LtpMonitoredJob.last_observed_at.desc(),
                    )
                    .limit(job_limit)
                )
            )
            events = list(
                session.scalars(
                    select(models.LtpMonitorEvent)
                    .join(
                        models.LtpMonitoredJob,
                        models.LtpMonitorEvent.job_id == models.LtpMonitoredJob.id,
                    )
                    .where(models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users))
                    .order_by(models.LtpMonitorEvent.observed_at.desc())
                    .limit(event_limit)
                )
            )
            notifications = list(
                session.scalars(
                    select(models.LtpMonitorNotification)
                    .join(
                        models.LtpMonitorEvent,
                        models.LtpMonitorNotification.event_id == models.LtpMonitorEvent.id,
                    )
                    .join(
                        models.LtpMonitoredJob,
                        models.LtpMonitorEvent.job_id == models.LtpMonitoredJob.id,
                    )
                    .where(models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users))
                    .order_by(models.LtpMonitorNotification.created_at.desc())
                    .limit(event_limit)
                )
            )
        return {
            "enabled": self.settings.ltp_monitor_enabled,
            "users": self.settings.ltp_monitor_users,
            "notifications_enabled": self.notifications_ready,
            "group_id": self.settings.ltp_monitor_group_id,
            "state": state,
            "jobs": jobs,
            "events": events,
            "notifications": notifications,
        }

    @property
    def notifications_ready(self) -> bool:
        return bool(
            self.settings.ltp_monitor_notifications_enabled
            and self.settings.ltp_monitor_group_id
            and self.sender.configured
        )

    def dispatch_notifications(self) -> list[LtpMonitorDelivery]:
        if not self.notifications_ready:
            return []
        now = datetime.now(UTC)
        self._recover_stale_claims(now)
        with self.session_factory() as session:
            queued = list(
                session.scalars(
                    select(models.LtpMonitorNotification)
                    .where(
                        models.LtpMonitorNotification.status.in_(("pending", "failed")),
                        or_(
                            models.LtpMonitorNotification.next_attempt_at.is_(None),
                            models.LtpMonitorNotification.next_attempt_at <= now,
                        ),
                    )
                    .order_by(models.LtpMonitorNotification.created_at)
                    .limit(20)
                )
            )

        results: list[LtpMonitorDelivery] = []
        for row in queued:
            claimed = self._claim_notification(row.id)
            if claimed is None:
                continue
            try:
                message = self._notification_message(claimed)
                sent = self.sender.send_group_text(claimed.target, message)
            except InfoflowDeliveryBlockedError as exc:
                error = _safe_cli_error(str(exc) or type(exc).__name__)
                self._finish_notification_blocked(claimed.id, error)
                results.append(LtpMonitorDelivery(claimed.id, "blocked", error))
                continue
            except Exception as exc:
                error = _safe_cli_error(str(exc) or type(exc).__name__)
                self._finish_notification_failed(claimed.id, error)
                results.append(LtpMonitorDelivery(claimed.id, "failed", error))
                continue
            self._finish_notification_sent(claimed.id, sent.message_id)
            results.append(LtpMonitorDelivery(claimed.id, "sent"))
        return results

    def _persist_list(self, current: dict[tuple[str, str], LtpCliJob], now: datetime) -> int:
        with self.session_factory() as session:
            control = self._control_row(session)
            is_bootstrap = control.initialized_at is None
            existing = {
                (job.user, job.job_name): job
                for job in session.scalars(
                    select(models.LtpMonitoredJob).where(
                        models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users)
                    )
                )
            }
            transitions = 0
            for identity, observed in current.items():
                job = existing.get(identity)
                if job is None:
                    metadata = dict(observed.metadata_json)
                    if is_bootstrap:
                        metadata["monitor_baseline_pending"] = True
                    job = models.LtpMonitoredJob(
                        user=observed.user,
                        job_name=observed.job_name,
                        cluster=observed.cluster,
                        state=observed.state,
                        provider_status=observed.provider_status,
                        state_group=observed.state_group,
                        category=observed.category,
                        first_observed_at=now,
                        last_observed_at=now,
                        last_status_at=now,
                        terminal_at=now if observed.state in TERMINAL_STATES else None,
                        metadata_json=metadata,
                    )
                    session.add(job)
                    session.flush()
                    if observed.state != "unknown":
                        self._event(
                            session,
                            job,
                            previous_state=None,
                            observed=observed,
                            event_type="discovered",
                            observed_at=now,
                            notify=not is_bootstrap,
                        )
                        transitions += 1
                    continue
                if self._apply_observation(
                    session,
                    job,
                    observed,
                    now,
                    notify=True,
                ):
                    transitions += 1
            control.initialized_at = control.initialized_at or now
            control.last_sync_at = now
            control.last_success_at = now
            control.last_error = None
            control.source_json = {
                "kind": "ltp_cli",
                "users": self.settings.ltp_monitor_users,
                "observed_jobs": len(current),
            }
            session.commit()
            return transitions

    def _probe_unresolved_jobs(self, current: set[tuple[str, str]], now: datetime) -> int:
        if not self.settings.ltp_monitor_status_probe_enabled:
            return 0
        with self.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(models.LtpMonitoredJob)
                    .where(
                        models.LtpMonitoredJob.user.in_(self.settings.ltp_monitor_users),
                        models.LtpMonitoredJob.state.not_in(TERMINAL_STATES),
                    )
                    .order_by(
                        models.LtpMonitoredJob.last_probe_at,
                        models.LtpMonitoredJob.last_observed_at,
                    )
                )
            )
            # Fresh list identities get priority, then retained active jobs.
            candidates.sort(key=lambda job: self._probe_priority(job, current))
            candidates = candidates[: self.settings.ltp_monitor_max_status_probes_per_sync]

        def probe(candidate: models.LtpMonitoredJob) -> tuple[
            models.LtpMonitoredJob,
            LtpCliJob | None,
            LtpMonitorSourceError | None,
        ]:
            try:
                return (
                    candidate,
                    self.source.show_job(candidate.user, candidate.job_name),
                    None,
                )
            except LtpMonitorSourceError as exc:
                return candidate, None, exc

        workers = min(self.settings.ltp_monitor_status_probe_concurrency, len(candidates))
        if workers:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ltp-monitor") as pool:
                observations = list(pool.map(probe, candidates))
        else:
            observations = []

        transitions = 0
        errors: list[str] = []
        for candidate, observed, error in observations:
            if error is not None:
                exc = error
                errors.append(f"{candidate.job_name}: {str(exc)}")
                continue
            with self.session_factory() as session:
                job = session.get(models.LtpMonitoredJob, candidate.id)
                if job is None or job.state in TERMINAL_STATES:
                    continue
                job.last_probe_at = now
                if observed is None:
                    job.missing_since = job.missing_since or now
                else:
                    suppress_baseline = bool(
                        (job.metadata_json or {}).get("monitor_baseline_pending")
                    )
                    if self._apply_observation(
                        session,
                        job,
                        observed,
                        now,
                        notify=not suppress_baseline,
                    ):
                        transitions += 1
                session.commit()
        if errors:
            self._record_sync_failure(now, errors, preserve_success=True)
        return transitions

    def _apply_observation(
        self,
        session: Session,
        job: models.LtpMonitoredJob,
        observed: LtpCliJob,
        now: datetime,
        *,
        notify: bool,
    ) -> bool:
        # A list observation can confirm that a task still exists but must not
        # overwrite the last exact status returned by `ltp-cli show`.
        if observed.state == "unknown":
            job.cluster = observed.cluster or job.cluster
            job.last_observed_at = now
            job.missing_since = None
            return False
        previous_state = job.state
        changed = observed.state != previous_state
        job.cluster = observed.cluster or job.cluster
        job.provider_status = observed.provider_status or job.provider_status
        job.state_group = observed.state_group or job.state_group
        job.category = observed.category or job.category
        job.last_observed_at = now
        job.last_probe_at = now if observed.source == "ltp_cli_show" else job.last_probe_at
        job.missing_since = None
        job.metadata_json = observed.metadata_json
        if not changed:
            return False
        job.state = observed.state
        job.last_status_at = now
        job.terminal_at = now if observed.state in TERMINAL_STATES else None
        self._event(
            session,
            job,
            previous_state=previous_state,
            observed=observed,
            event_type="state_changed",
            observed_at=now,
            notify=notify,
        )
        return True

    @staticmethod
    def _probe_priority(
        job: models.LtpMonitoredJob,
        current: set[tuple[str, str]],
    ) -> tuple[int, bool, datetime]:
        metadata = job.metadata_json or {}
        list_hint = normalize_ltp_state(
            _text(metadata.get("list_status")) or _text(metadata.get("last_known_status"))
        )
        if job.state in {"created", "pending", "running"} or list_hint in {
            "created",
            "pending",
            "running",
        }:
            state_priority = 0
        elif job.state == "unknown" and list_hint in TERMINAL_STATES:
            state_priority = 2
        else:
            state_priority = 1
        return (
            state_priority,
            (job.user, job.job_name) not in current,
            job.last_probe_at or job.first_observed_at,
        )

    def _event(
        self,
        session: Session,
        job: models.LtpMonitoredJob,
        *,
        previous_state: str | None,
        observed: LtpCliJob,
        event_type: str,
        observed_at: datetime,
        notify: bool,
    ) -> models.LtpMonitorEvent:
        event = models.LtpMonitorEvent(
            job_id=job.id,
            event_type=event_type,
            previous_state=previous_state,
            state=observed.state,
            provider_status=observed.provider_status,
            source=observed.source,
            observed_at=observed_at,
            detail_json={
                "cluster": observed.cluster,
                "state_group": observed.state_group,
                "category": observed.category,
                "metadata": observed.metadata_json,
            },
        )
        session.add(event)
        session.flush()
        if (
            notify
            and self.settings.ltp_monitor_notifications_enabled
            and self.settings.ltp_monitor_group_id
            and observed.state in self.settings.ltp_monitor_notify_states
        ):
            session.add(
                models.LtpMonitorNotification(
                    event_id=event.id,
                    channel="infoflow_group",
                    target=self.settings.ltp_monitor_group_id,
                )
            )
        return event

    def _record_sync_failure(
        self,
        now: datetime,
        errors: list[str],
        *,
        preserve_success: bool = False,
    ) -> None:
        with self.session_factory() as session:
            control = self._control_row(session)
            control.last_sync_at = now
            if not preserve_success:
                control.last_success_at = control.last_success_at
            control.last_error = "; ".join(errors)[:1_000]
            control.source_json = {"kind": "ltp_cli", "errors": len(errors)}
            session.commit()

    @staticmethod
    def _control_row(session: Session) -> models.LtpMonitorState:
        row = session.get(models.LtpMonitorState, "default")
        if row is None:
            row = models.LtpMonitorState(id="default")
            session.add(row)
            session.flush()
        return row

    def _recover_stale_claims(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=10)
        with self.session_factory() as session:
            stale = list(
                session.scalars(
                    select(models.LtpMonitorNotification).where(
                        models.LtpMonitorNotification.status == "sending",
                        models.LtpMonitorNotification.updated_at < cutoff,
                    )
                )
            )
            for notification in stale:
                notification.status = "failed"
                notification.last_error = "Delivery worker stopped before confirming the send"
                notification.next_attempt_at = now
            if stale:
                session.commit()

    def _claim_notification(self, notification_id: str) -> models.LtpMonitorNotification | None:
        with self.session_factory() as session:
            row = session.get(models.LtpMonitorNotification, notification_id)
            if row is None or row.status not in {"pending", "failed"}:
                return None
            row.status = "sending"
            row.attempts += 1
            row.next_attempt_at = None
            session.commit()
            return row

    def _notification_message(self, notification: models.LtpMonitorNotification) -> str:
        with self.session_factory() as session:
            event = session.get(models.LtpMonitorEvent, notification.event_id)
            if event is None:
                raise RuntimeError("LTP monitor event no longer exists")
            job = session.get(models.LtpMonitoredJob, event.job_id)
            if job is None:
                raise RuntimeError("LTP monitored job no longer exists")
            observed = event.observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
            state = _display_state(event.state)
            transition = (
                f"{_display_state(event.previous_state)} → {state}"
                if event.previous_state and event.previous_state != "unknown"
                else state
            )
            return "\n".join(
                (
                    "LTP Monitor · 任务状态更新",
                    f"任务：{job.job_name}",
                    f"用户：{job.user}",
                    f"集群：{job.cluster or '未知'}",
                    f"状态：{transition}",
                    f"LTP 原始状态：{event.provider_status or '未知'}",
                    f"观测时间：{observed:%Y-%m-%d %H:%M:%S} (Shanghai)",
                    f"查看：{self.settings.infoflow_public_base_url.rstrip('/')}/ltp-monitor",
                )
            )

    def _finish_notification_sent(self, notification_id: str, message_id: str | None) -> None:
        with self.session_factory() as session:
            notification = session.get(models.LtpMonitorNotification, notification_id)
            if notification is None:
                return
            notification.status = "sent"
            notification.sent_at = datetime.now(UTC)
            notification.provider_message_id = message_id
            notification.last_error = None
            notification.next_attempt_at = None
            session.commit()

    def _finish_notification_failed(self, notification_id: str, error: str) -> None:
        with self.session_factory() as session:
            notification = session.get(models.LtpMonitorNotification, notification_id)
            if notification is None:
                return
            notification.status = "failed"
            delay = min(900, 10 * (2 ** min(notification.attempts, 6)))
            notification.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            notification.last_error = error[:1_000]
            session.commit()

    def _finish_notification_blocked(self, notification_id: str, error: str) -> None:
        with self.session_factory() as session:
            notification = session.get(models.LtpMonitorNotification, notification_id)
            if notification is None:
                return
            notification.status = "blocked"
            notification.next_attempt_at = None
            notification.last_error = error[:1_000]
            session.commit()


def _parse_list_output(output: str) -> list[dict[str, Any]]:
    json_records = _json_records(output)
    if json_records is not None:
        return json_records
    if _looks_empty(output):
        return []
    records = _table_records(output)
    if records:
        return records
    blocks = _labelled_blocks(output)
    if blocks:
        return blocks
    raise LtpMonitorSourceError("LTP CLI list 输出格式无法识别")


def _parse_show_output(output: str) -> dict[str, Any] | None:
    if _looks_empty(output) or _looks_not_found(output):
        return None
    json_records = _json_records(output)
    if json_records:
        return json_records[0]
    labelled = _labelled_blocks(output)
    if labelled:
        return labelled[0]
    match = _STATUS_LINE.search(output)
    if not match:
        raise LtpMonitorSourceError("LTP CLI show 输出缺少当前 status")
    return {"status": match.group(1).strip()}


def _json_records(output: str) -> list[dict[str, Any]] | None:
    stripped = output.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return None
    for key in ("jobs", "items", "data", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("jobs", "items", "data", "result"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            return [value]
    return [payload]


def _table_records(output: str) -> list[dict[str, Any]]:
    rows: list[list[str]] = []
    for raw in output.splitlines():
        if "|" not in raw and "│" not in raw:
            continue
        if _TABLE_DIVIDER.match(raw):
            continue
        delimiter = "│" if "│" in raw else "|"
        cells = [cell.strip() for cell in raw.strip().strip("|│").split(delimiter)]
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return []
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(_field_name(cell) == "job_name" for cell in row)
            and any(_field_name(cell) == "status" for cell in row)
        ),
        None,
    )
    if header_index is None:
        return []
    headers = [_field_name(cell) for cell in rows[header_index]]
    records: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        if len(row) != len(headers):
            continue
        record = {key: value for key, value in zip(headers, row, strict=True) if key and value}
        if record.get("job_name"):
            records.append(record)
    return records


def _labelled_blocks(output: str) -> list[dict[str, Any]]:
    record: dict[str, Any] = {}
    for raw in output.splitlines():
        if ":" not in raw and "：" not in raw:
            continue
        delimiter = "：" if "：" in raw else ":"
        label, value = raw.split(delimiter, 1)
        key = _field_name(label)
        value = value.strip()
        if key and value:
            record[key] = value
    if record.get("status"):
        return [record]
    return []


def _field_name(value: str) -> str:
    normalized = re.sub(r"[\s_\-]+", "", value).casefold()
    if normalized in {"job", "jobname", "name", "任务", "任务名", "任务名称"}:
        return "job_name"
    if normalized in {"user", "username", "用户", "用户名"}:
        return "user"
    if normalized in {"cluster", "集群"}:
        return "cluster"
    if normalized in {"status", "state", "状态"}:
        return "status"
    if normalized in {"stategroup", "状态组"}:
        return "state_group"
    if normalized in {"category", "分类", "类型"}:
        return "category"
    return ""


def _job_from_record(
    record: Mapping[str, Any],
    *,
    default_user: str,
    source: str,
    default_job_name: str | None = None,
    trust_status: bool = True,
) -> LtpCliJob:
    canonical = {_field_name(str(key)) or str(key): value for key, value in record.items()}
    user = _text(canonical.get("user")) or default_user
    job_name = _text(canonical.get("job_name")) or default_job_name
    if not job_name:
        raise LtpMonitorSourceError("LTP CLI 输出缺少任务名称")
    listed_status = _text(canonical.get("status"))
    provider_status = listed_status if trust_status else None
    state_group = _text(canonical.get("state_group"))
    state = normalize_ltp_state(provider_status, state_group) if trust_status else "unknown"
    metadata = {
        key: _scalar(value)
        for key, value in canonical.items()
        if key not in {"user", "job_name", "cluster", "status", "state_group", "category"}
        and _scalar(value) is not None
    }
    if not trust_status and listed_status:
        metadata["list_status"] = listed_status
    return LtpCliJob(
        user=user,
        job_name=job_name,
        cluster=_text(canonical.get("cluster")),
        state=state,
        provider_status=provider_status,
        state_group=state_group,
        category=_text(canonical.get("category")),
        metadata_json=metadata,
        source=source,
    )


def normalize_ltp_state(status: str | None, state_group: str | None = None) -> str:
    normalized = re.sub(r"[\s\-]+", "_", (status or "").strip().casefold())
    if normalized.startswith("created"):
        return "created"
    if normalized.startswith(("pending", "queued", "waiting", "submitted", "scheduling")):
        return "pending"
    if normalized.startswith(("running", "active", "starting", "initializing")):
        return "running"
    if normalized.startswith(("failed", "error", "terminated", "preempted", "evicted")):
        return "failed"
    if normalized.startswith(("succeeded", "success", "completed", "complete", "finished", "done")):
        return "completed"
    if normalized.startswith(("cancelled", "canceled", "deleted", "stopped")):
        return "cancelled"
    group = re.sub(r"[\s\-]+", "_", (state_group or "").strip().casefold())
    if group in {"active", "running"}:
        return "running"
    if group in {"created", "pending", "queued", "waiting"}:
        return "pending"
    return "unknown"


def _looks_empty(value: str) -> bool:
    normalized = value.casefold()
    return not normalized.strip() or any(
        token in normalized
        for token in ("no jobs", "no task", "暂无任务", "没有任务", "任务列表为空")
    )


def _looks_not_found(value: str) -> bool:
    normalized = value.casefold()
    return any(token in normalized for token in ("not found", "不存在", "未找到", "没有找到"))


def _looks_cli_failure(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith((
        "获取任务列表失败",
        "获取任务详情失败",
        "任务列表失败",
        "任务详情失败",
        "error:",
        "错误：",
    ))


def _safe_cli_error(value: str) -> str:
    compact = " ".join(redact(value).split())
    return compact[:1_000]


def _text(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _scalar(value: Any) -> str | int | float | bool | None:
    return value if isinstance(value, (str, int, float, bool)) else None


def _display_state(value: str | None) -> str:
    labels = {
        "created": "Created",
        "pending": "Pending",
        "running": "Running",
        "failed": "Failed",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "unknown": "Unknown",
    }
    return labels.get(value or "", value or "Unknown")
