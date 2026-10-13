"""Durable AgentOS/Datawheel job submission and status synchronization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.agentos import AgentOSClient, AgentOSClientError
from research_ops.settings import Settings


class AgentOSSubmissionConflict(RuntimeError):
    """The local idempotency key already owns an AgentOS execution."""


_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "secret",
    "token",
)


def _redact(value: Any, *, key: str | None = None) -> Any:
    """Redact secret-like values before they enter durable local state."""

    normalized = (key or "").lower().replace("-", "_")
    if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def payload_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep useful routing metadata without persisting prompts or credentials."""

    safe_payload = _redact(payload)
    try:
        canonical = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(safe_payload)
    snapshot: dict[str, Any] = {
        "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "task_name": payload.get("taskName") or payload.get("task_name"),
        "query_mode": payload.get("queryMode") or payload.get("query_mode"),
        "pipeline": payload.get("selectedPipeline") or payload.get("pipeline"),
        "environment": _environment_value(payload.get("env")),
        "concurrency": payload.get("concurrency"),
    }
    records = payload.get("records")
    if isinstance(records, list):
        snapshot["record_count"] = len(records)
        snapshot["record_ids"] = [
            str(item.get("id"))
            for item in records[:100]
            if isinstance(item, Mapping) and item.get("id") is not None
        ]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        # Keep model/provider routing, but strip keys such as api_key/token.
        safe_metadata = _redact(metadata)
        if isinstance(safe_metadata, dict):
            snapshot["metadata"] = safe_metadata
    return snapshot


def _environment_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value") or value.get("label")
    return value


def _status(value: str | None) -> str:
    normalized = str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"success", "succeeded", "complete", "completed", "done"}:
        return "completed"
    if normalized in {"failed", "error", "failure"} or normalized.startswith(
        ("failed_", "error_")
    ):
        return "failed"
    if normalized in {"cancelled", "canceled", "stopped", "superseded"}:
        return "cancelled"
    return normalized or "unknown"


def _terminal(status: str) -> bool:
    return status in {"completed", "failed", "cancelled"}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _work(session: Session, identity: str) -> models.WorkItem:
    item = session.get(models.WorkItem, identity)
    if item is None:
        item = session.scalar(
            select(models.WorkItem).where(models.WorkItem.human_id == identity.upper())
        )
    if item is None:
        raise ValueError(f"Work item not found: {identity}")
    return item


class AgentOSJobSync:
    """Own the local durable mirror of Datawheel batch state."""

    provider = AgentOSClient.provider

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        client: AgentOSClient | None = None,
        now: Callable[[], datetime] | None = None,
        reconcile_work: Callable[[set[str]], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client
        if self.client is None and settings.agentos_datawheel_base_url:
            self.client = AgentOSClient(
                settings.agentos_datawheel_base_url,
                trajectory_base_url=settings.agentos_trajectory_base_url,
                token_env=settings.agentos_token_env,
                timeout_seconds=settings.agentos_timeout_seconds,
                verify=settings.agentos_verify_tls,
                trust_env=settings.agentos_trust_env,
            )
        self._now = now or (lambda: datetime.now(UTC))
        self._reconcile_work = reconcile_work

    @property
    def configured(self) -> bool:
        return self.client is not None

    def submit_batch(
        self,
        *,
        work_item_id: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        execution_type: str = "agentos_rollout",
    ) -> tuple[models.Execution, models.ExternalJob]:
        """Submit once and persist an execution/job pair.

        The remote request is intentionally made outside the database
        transaction.  A failed request marks the local execution as failed;
        a caller can retry with a new idempotency key after inspecting the
        provider state.
        """

        if self.client is None or not self.settings.agentos_enabled:
            raise AgentOSClientError("AgentOS integration is not configured")
        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key must not be empty")
        if not isinstance(payload, Mapping):
            raise TypeError("AgentOS payload must be a mapping")

        snapshot = payload_snapshot(payload)
        with self.session_factory() as session:
            work_item = _work(session, work_item_id)
            existing_execution = session.scalar(
                select(models.Execution).where(models.Execution.idempotency_key == key)
            )
            if existing_execution is not None:
                if (
                    existing_execution.work_item_id != work_item.id
                    or existing_execution.config_snapshot.get("payload_sha256")
                    != snapshot.get("payload_sha256")
                ):
                    raise AgentOSSubmissionConflict(
                        "idempotency key is already bound to a different AgentOS request"
                    )
                existing_job = session.scalar(
                    select(models.ExternalJob).where(
                        models.ExternalJob.execution_id == existing_execution.id
                    )
                )
                if existing_job is not None:
                    return existing_execution, existing_job
                raise AgentOSSubmissionConflict(
                    f"idempotency key already has execution {existing_execution.id}"
                )
            execution = models.Execution(
                work_item_id=work_item.id,
                execution_type=execution_type,
                status="submitting",
                idempotency_key=key,
                config_snapshot=snapshot,
            )
            session.add(execution)
            session.commit()
            execution_id = execution.id

        try:
            remote = self.client.submit_batch(payload, idempotency_key=key)
        except Exception as exc:
            with self.session_factory() as session:
                failed = session.get(models.Execution, execution_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.completed_at = self._now()
                    failed.config_snapshot = {
                        **failed.config_snapshot,
                        "submission_error_type": type(exc).__name__,
                    }
                    session.commit()
            raise

        now = self._now()
        remote_status = _status(remote.status)
        with self.session_factory() as session:
            execution = session.get(models.Execution, execution_id)
            if execution is None:  # pragma: no cover - defensive against manual DB edits
                raise RuntimeError(f"Execution disappeared during AgentOS submit: {execution_id}")
            existing_job = session.scalar(
                select(models.ExternalJob).where(
                    models.ExternalJob.provider == self.provider,
                    models.ExternalJob.remote_job_id == remote.batch_task_id,
                )
            )
            metadata = {
                "batch_task_id": remote.batch_task_id,
                "idempotency_key": key,
                "status_counts": remote.status_counts,
                "total_size": remote.total_size,
                "payload_snapshot": snapshot,
            }
            if existing_job is None:
                job = models.ExternalJob(
                    work_item_id=execution.work_item_id,
                    execution_id=execution.id,
                    provider=self.provider,
                    remote_job_id=remote.batch_task_id,
                    status=remote_status,
                    metadata_json=metadata,
                    last_observed_at=now,
                    next_poll_at=now,
                )
                session.add(job)
            else:
                job = existing_job
                if job.execution_id not in {None, execution.id}:
                    execution.status = "failed"
                    execution.completed_at = now
                    session.commit()
                    raise AgentOSSubmissionConflict(
                        f"remote batch {remote.batch_task_id} is already linked"
                    )
                job.execution_id = execution.id
                job.status = remote_status
                job.metadata_json = {**job.metadata_json, **metadata}
                job.last_observed_at = now
                job.next_poll_at = now
            execution.status = remote_status
            execution.started_at = now
            if _terminal(remote_status):
                execution.completed_at = now
            work_item = session.get(models.WorkItem, execution.work_item_id)
            if work_item is not None and not _terminal(remote_status):
                work_item.flow = "running"
                work_item.lifecycle = "active"
            session.commit()
            return execution, job

    def sync_all(self) -> int:
        """Poll active jobs and persist provider status/counts.

        This method is safe to call from the existing Research Ops polling
        thread.  It never downloads signed result URLs automatically.
        """

        if self.client is None or not self.settings.agentos_enabled:
            return 0
        now = self._now()
        with self.session_factory() as session:
            jobs = list(
                session.scalars(
                    select(models.ExternalJob)
                    .where(
                        models.ExternalJob.provider == self.provider,
                        models.ExternalJob.status.not_in(
                            ["completed", "failed", "cancelled"]
                        ),
                    )
                    .order_by(models.ExternalJob.created_at)
                    .limit(max(1, self.settings.agentos_job_sync_max_jobs))
                )
            )

        changed = 0
        changed_work_ids: set[str] = set()
        for job in jobs:
            if job.next_poll_at is not None and _as_utc(job.next_poll_at) > _as_utc(now):
                continue
            try:
                snapshot = self.client.get_batch(job.remote_job_id)
            except Exception as exc:
                with self.session_factory() as session:
                    stored = session.get(models.ExternalJob, job.id)
                    if stored is not None:
                        changed_work_ids.add(stored.work_item_id)
                        stored.consecutive_failures += 1
                        stored.last_observed_at = now
                        stored.next_poll_at = now + timedelta(
                            seconds=self.settings.agentos_job_sync_interval_seconds
                        )
                        stored.metadata_json = {
                            **stored.metadata_json,
                            "last_poll_error_type": type(exc).__name__,
                        }
                        session.commit()
                changed += 1
                continue

            status = _status(snapshot.status)
            with self.session_factory() as session:
                stored = session.get(models.ExternalJob, job.id)
                if stored is None:
                    continue
                changed_work_ids.add(stored.work_item_id)
                stored.status = status
                stored.last_observed_at = now
                stored.next_poll_at = (
                    None
                    if _terminal(status)
                    else now + timedelta(seconds=self.settings.agentos_job_sync_interval_seconds)
                )
                stored.consecutive_failures = 0
                stored.metadata_json = {
                    **stored.metadata_json,
                    "status_counts": snapshot.status_counts,
                    "total_size": snapshot.total_size,
                    "provider_status": snapshot.status,
                }
                if stored.execution_id:
                    execution = session.get(models.Execution, stored.execution_id)
                    if execution is not None:
                        execution.status = status
                        if _terminal(status):
                            execution.completed_at = now
                session.commit()
            changed += 1
        if self._reconcile_work:
            self._reconcile_work(changed_work_ids)
        return changed

    def get_local_batch(
        self, batch_task_id: str
    ) -> tuple[models.Execution | None, models.ExternalJob | None]:
        """Return the durable local records for a remote batch id."""

        identifier = str(batch_task_id).strip()
        if not identifier:
            raise ValueError("batch_task_id must not be empty")
        with self.session_factory() as session:
            job = session.scalar(
                select(models.ExternalJob).where(
                    models.ExternalJob.provider == self.provider,
                    models.ExternalJob.remote_job_id == identifier,
                )
            )
            if job is None:
                return None, None
            execution = (
                session.get(models.Execution, job.execution_id) if job.execution_id else None
            )
            return execution, job

    def trajectory(self, app_id: str, *, environment: str = "evaluation") -> Any:
        if self.client is None:
            raise AgentOSClientError("AgentOS integration is not configured")
        return self.client.get_trajectory(app_id, environment=environment)
