from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models


class DagJobSync:
    """Mirror submitted nodes from registered DAG state files into ExternalJob."""

    def __init__(self, session_factory: sessionmaker[Session], *, max_bytes: int) -> None:
        self.session_factory = session_factory
        self.max_bytes = max_bytes

    def sync_all(self) -> int:
        with self.session_factory() as session:
            dags = list(
                session.scalars(
                    select(models.ExternalJob).where(
                        models.ExternalJob.provider == "dag-runner"
                    )
                )
            )
            changed = 0
            for dag in dags:
                changed += self._sync_dag(session, dag)
            if changed:
                session.commit()
            return changed

    def _sync_dag(self, session: Session, dag: models.ExternalJob) -> int:
        state_path = _metadata_path(dag.metadata_json, "state_path")
        if state_path is None:
            return 0
        try:
            stat = state_path.stat()
        except OSError:
            return 0
        if stat.st_size > self.max_bytes:
            return 0
        if dag.metadata_json.get("state_mtime_ns") == stat.st_mtime_ns:
            return 0
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 0
        if not isinstance(state, dict) or not isinstance(state.get("jobs"), dict):
            return 0

        definition = _read_definition(dag.metadata_json)
        specs = definition.get("jobs") if isinstance(definition.get("jobs"), dict) else {}
        now = datetime.now(UTC)
        changed = 0
        dag_status = _status(str(state.get("status", "unknown")))
        dag_metadata = {
            **dag.metadata_json,
            "state_mtime_ns": stat.st_mtime_ns,
            "state_observed_at": now.isoformat(),
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "log_dir": state.get("log_dir") or dag.metadata_json.get("log_dir"),
        }
        if dag.status != dag_status or dag.metadata_json != dag_metadata:
            dag.status = dag_status
            dag.metadata_json = dag_metadata
            dag.last_observed_at = now
            changed += 1

        for name, raw_job in state["jobs"].items():
            if not isinstance(name, str) or not isinstance(raw_job, dict):
                continue
            if not _materialized(raw_job):
                continue
            spec = specs.get(name) if isinstance(specs.get(name), dict) else {}
            cluster = str(spec.get("cluster") or definition.get("cluster") or "").strip()
            executor = str(raw_job.get("executor") or spec.get("executor") or "external")
            provider = f"ltp/{cluster}" if executor == "ltp" and cluster else f"dag/{executor}"
            existing = session.scalar(
                select(models.ExternalJob)
                .where(
                    models.ExternalJob.work_item_id == dag.work_item_id,
                    models.ExternalJob.remote_job_id == name,
                )
                .order_by(models.ExternalJob.created_at)
                .limit(1)
            )
            metadata = _job_metadata(
                raw_job,
                spec,
                dag_name=str(state.get("dag_name") or dag.remote_job_id),
                state_path=state_path,
                cluster=cluster,
                executor=executor,
            )
            normalized_status = _status(str(raw_job.get("status", "unknown")))
            if existing is None:
                session.add(
                    models.ExternalJob(
                        work_item_id=dag.work_item_id,
                        provider=provider,
                        remote_job_id=name,
                        status=normalized_status,
                        metadata_json=metadata,
                        last_observed_at=now,
                    )
                )
                changed += 1
                continue
            merged_metadata = {**existing.metadata_json, **metadata}
            if (
                existing.provider != provider
                or existing.status != normalized_status
                or existing.metadata_json != merged_metadata
            ):
                existing.provider = provider
                existing.status = normalized_status
                existing.metadata_json = merged_metadata
                existing.last_observed_at = now
                changed += 1
        return changed


def _metadata_path(metadata: dict[str, Any], key: str) -> Path | None:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else None


def _read_definition(metadata: dict[str, Any]) -> dict[str, Any]:
    path = _metadata_path(metadata, "dag_path")
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _materialized(job: dict[str, Any]) -> bool:
    status = str(job.get("status", "pending")).lower()
    return status != "pending" or any(
        job.get(key) for key in ("job_id", "submit_time", "batch_task_id")
    )


def _status(value: str) -> str:
    normalized = value.strip().lower()
    return {
        "success": "completed",
        "succeeded": "completed",
        "completed": "completed",
        "running": "running",
        "submitted": "queued",
        "created": "queued",
        "pending": "queued",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(normalized, normalized or "unknown")


def _job_metadata(
    state: dict[str, Any],
    spec: dict[str, Any],
    *,
    dag_name: str,
    state_path: Path,
    cluster: str,
    executor: str,
) -> dict[str, Any]:
    keys = (
        "job_id",
        "submit_time",
        "complete_time",
        "retries",
        "error",
        "job_log_dir",
        "batch_task_id",
        "last_remote_status",
        "last_dynamic_concurrency",
        "last_deploy_completed",
        "dynamic_last_error",
    )
    return {
        "dag_name": dag_name,
        "dag_state_path": str(state_path),
        "cluster": cluster or None,
        "executor": executor,
        "config": spec.get("config"),
        "run": spec.get("run"),
        **{key: state.get(key) for key in keys if state.get(key) is not None},
    }
