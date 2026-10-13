from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, cast, distinct, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.services.codex_events import redact
from research_ops.services.core import ConflictError, NotFoundError

TERMINAL_UNIT_STATUSES = {"succeeded", "quarantined"}
CLAIMABLE_UNIT_STATUSES = {"pending", "retryable"}
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_TASK_FILE_BYTES = 32 * 1024 * 1024
MAX_JUDGE_FILE_BYTES = 8 * 1024 * 1024
SECRET_KEYS = {"authorization", "api_key", "app_key", "app_secret", "password", "secret", "token"}


class EvalHarnessService:
    """Durable, evaluator-agnostic scheduling for fine-grained agent eval units."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def create_run(self, payload: schemas.EvalRunCreate) -> models.EvalRun:
        _reject_secret_material(payload.config_json)
        config_hash = _json_digest(
            {
                "adapter": payload.adapter,
                "mode": payload.mode,
                "candidate_id": payload.candidate_id,
                "candidate_digest": payload.candidate_digest,
                "tool_parser": payload.tool_parser,
                "config": payload.config_json,
            }
        )
        with self.session_factory() as session:
            work_item = _work(session, payload.work_item_id)
            run = models.EvalRun(
                **payload.model_dump(exclude={"work_item_id", "name", "config_json"}),
                work_item_id=work_item.id,
                name=payload.name.strip(),
                config_json=_sanitize_json(payload.config_json),
                config_hash=config_hash,
            )
            session.add(run)
            session.flush()
            _event(
                session,
                run,
                "eval.run_created",
                {
                    "run_id": run.id,
                    "adapter": run.adapter,
                    "mode": run.mode,
                    "tool_parser": run.tool_parser,
                },
            )
            session.commit()
            return run

    def list_runs(
        self, *, work_item_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            query = select(models.EvalRun).order_by(models.EvalRun.created_at.desc())
            if work_item_id:
                work = _work(session, work_item_id)
                query = query.where(models.EvalRun.work_item_id == work.id)
            if status:
                query = query.where(models.EvalRun.status == status)
            runs = list(session.scalars(query.limit(200)))
            return [self._overview(session, run) for run in runs]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = _run(session, run_id)
            return self._overview(session, run)

    def list_units(
        self,
        run_id: str,
        *,
        status: str | None = None,
        error_category: str | None = None,
        case_key: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[models.EvalUnit]:
        with self.session_factory() as session:
            run = _run(session, run_id)
            query = (
                select(models.EvalUnit)
                .where(models.EvalUnit.run_id == run.id)
                .order_by(
                    models.EvalUnit.priority.desc(),
                    models.EvalUnit.case_key,
                    models.EvalUnit.rubric_key,
                )
            )
            if status:
                query = query.where(models.EvalUnit.status == status)
            if error_category:
                query = query.where(models.EvalUnit.error_category == error_category)
            if case_key:
                query = query.where(models.EvalUnit.case_key == case_key)
            return list(session.scalars(query.offset(offset).limit(limit)))

    def import_presentbench(
        self, run_id: str, payload: schemas.EvalPresentBenchImport
    ) -> dict[str, Any]:
        task_path = _existing_file(payload.task_file, "PresentBench task file")
        result_root = _existing_directory(payload.result_root, "PresentBench result root")
        records = _read_jsonl(task_path, limit=payload.limit)
        if not records:
            raise ConflictError("PresentBench task file contains no cases")

        with self.session_factory() as session:
            run = _run(session, run_id)
            if run.adapter != "presentbench":
                raise ConflictError("PresentBench import requires adapter='presentbench'")
            if run.status == "running":
                raise ConflictError(
                    "Pause the eval run before importing or refreshing its manifest"
                )
            if session.scalar(
                select(func.count())
                .select_from(models.EvalUnit)
                .where(models.EvalUnit.run_id == run.id, models.EvalUnit.status == "leased")
            ):
                raise ConflictError("Cannot import while eval units are leased")

            specs = _presentbench_specs(
                records,
                result_root=result_root,
                candidate_filename=payload.candidate_filename,
                config_hash=run.config_hash,
                max_attempts=run.max_attempts,
            )
            cache = self._cache_sources(session, run.id, {item["unit_key"] for item in specs})
            existing = {
                unit.rubric_key + "\0" + unit.case_key: unit
                for unit in session.scalars(
                    select(models.EvalUnit).where(models.EvalUnit.run_id == run.id)
                )
            }
            seen: set[str] = set()
            reused = 0
            quarantined = 0
            for spec in specs:
                identity = spec["rubric_key"] + "\0" + spec["case_key"]
                seen.add(identity)
                previous = existing.get(identity)
                if previous is None:
                    unit = models.EvalUnit(run_id=run.id, **spec)
                    session.add(unit)
                elif previous.unit_key == spec["unit_key"]:
                    unit = previous
                    unit.input_json = spec["input_json"]
                    unit.material_key = spec["material_key"]
                    unit.domain = spec["domain"]
                    unit.priority = spec["priority"]
                    unit.max_attempts = spec["max_attempts"]
                    if unit.status == "quarantined" and spec["status"] == "pending":
                        _reset_unit(unit)
                    elif spec["status"] == "succeeded" and unit.status != "succeeded":
                        _apply_seeded_result(unit, spec)
                else:
                    unit = previous
                    for key, value in spec.items():
                        setattr(unit, key, value)
                    if spec["status"] != "succeeded":
                        _reset_unit(unit, status=spec["status"])

                if spec["status"] == "quarantined":
                    quarantined += 1
                    continue
                cached = cache.get(spec["unit_key"])
                if cached is not None and unit.status != "succeeded":
                    unit.status = "succeeded"
                    unit.score = cached.score
                    unit.result_json = dict(cached.result_json or {})
                    unit.duration_ms = cached.duration_ms
                    unit.cache_hit = True
                    unit.cache_source_unit_id = cached.id
                    unit.completed_at = datetime.now(UTC)
                    unit.error_category = None
                    unit.error_message = None
                    reused += 1

            for identity, unit in existing.items():
                if identity not in seen:
                    session.delete(unit)

            run.source_uri = str(task_path)
            run.artifact_uri = str(result_root)
            run.expected_units = len(specs)
            session.flush()
            if payload.auto_start:
                self._set_running_or_terminal(session, run)
            else:
                run.status = "draft"
                run.completed_at = None
            _event(
                session,
                run,
                "eval.presentbench_imported",
                {
                    "run_id": run.id,
                    "cases": len({item["case_key"] for item in specs}),
                    "units": len(specs),
                    "cache_hits": reused,
                    "quarantined": quarantined,
                },
            )
            session.commit()
            return {
                "run": run,
                "imported_cases": len({item["case_key"] for item in specs}),
                "imported_units": len(specs),
                "reused_units": reused,
                "quarantined_units": quarantined,
                "summary": self._summary(session, run),
            }

    def import_units(
        self, run_id: str, payload: schemas.EvalUnitBatchImport
    ) -> dict[str, Any]:
        specs = _generic_specs(payload.units)
        with self.session_factory() as session:
            run = _run(session, run_id)
            if run.status == "running":
                raise ConflictError("Pause the eval run before importing units")
            if session.scalar(
                select(func.count())
                .select_from(models.EvalUnit)
                .where(models.EvalUnit.run_id == run.id, models.EvalUnit.status == "leased")
            ):
                raise ConflictError("Cannot import while eval units are leased")

            finalized_specs: list[dict[str, Any]] = []
            for spec in specs:
                candidate_digest = spec.pop("candidate_digest") or run.candidate_digest
                if not candidate_digest:
                    raise ConflictError(
                        "Generic eval units require candidate_digest on the run or each unit"
                    )
                input_json = spec["input_json"]
                unit_key = _json_digest(
                    {
                        "candidate_digest": candidate_digest,
                        "case_key": spec["case_key"],
                        "rubric_key": spec["rubric_key"],
                        "rubric_group": spec["rubric_group"],
                        "input_digest": _json_digest(input_json),
                        "config_hash": run.config_hash,
                    }
                )
                finalized_specs.append(
                    {
                        **spec,
                        "unit_key": unit_key,
                        "max_attempts": run.max_attempts,
                        "status": "pending",
                        "result_json": {},
                        "score": None,
                        "attempt_count": 0,
                        "lease_token": None,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "error_category": None,
                        "error_message": None,
                        "duration_ms": None,
                        "cache_hit": False,
                        "cache_source_unit_id": None,
                        "started_at": None,
                        "completed_at": None,
                    }
                )

            cache = self._cache_sources(
                session, run.id, {item["unit_key"] for item in finalized_specs}
            )
            existing = {
                unit.rubric_key + "\0" + unit.case_key: unit
                for unit in session.scalars(
                    select(models.EvalUnit).where(models.EvalUnit.run_id == run.id)
                )
            }
            seen: set[str] = set()
            reused = 0
            for spec in finalized_specs:
                identity = spec["rubric_key"] + "\0" + spec["case_key"]
                if identity in seen:
                    raise ConflictError(
                        f"Eval unit is duplicated in import: {spec['case_key']} / "
                        f"{spec['rubric_key']}"
                    )
                seen.add(identity)
                previous = existing.get(identity)
                if previous is None:
                    unit = models.EvalUnit(run_id=run.id, **spec)
                    session.add(unit)
                elif previous.unit_key == spec["unit_key"]:
                    unit = previous
                    unit.input_json = spec["input_json"]
                    unit.material_key = spec["material_key"]
                    unit.domain = spec["domain"]
                    unit.priority = spec["priority"]
                    unit.max_attempts = spec["max_attempts"]
                else:
                    unit = previous
                    for key, value in spec.items():
                        setattr(unit, key, value)
                    _reset_unit(unit)
                cached = cache.get(spec["unit_key"])
                if cached is not None and unit.status != "succeeded":
                    unit.status = "succeeded"
                    unit.score = cached.score
                    unit.result_json = dict(cached.result_json or {})
                    unit.duration_ms = cached.duration_ms
                    unit.cache_hit = True
                    unit.cache_source_unit_id = cached.id
                    unit.completed_at = datetime.now(UTC)
                    unit.error_category = None
                    unit.error_message = None
                    reused += 1

            if payload.replace:
                for identity, unit in existing.items():
                    if identity not in seen:
                        session.delete(unit)
            session.flush()
            run.expected_units = session.scalar(
                select(func.count())
                .select_from(models.EvalUnit)
                .where(models.EvalUnit.run_id == run.id)
            ) or 0
            if payload.auto_start:
                self._set_running_or_terminal(session, run)
            else:
                run.status = "draft"
                run.completed_at = None
            _event(
                session,
                run,
                "eval.units_imported",
                {
                    "run_id": run.id,
                    "units": len(finalized_specs),
                    "cache_hits": reused,
                    "replace": payload.replace,
                },
            )
            session.commit()
            return {
                "run": run,
                "imported_cases": len({item["case_key"] for item in finalized_specs}),
                "imported_units": len(finalized_specs),
                "reused_units": reused,
                "quarantined_units": 0,
                "summary": self._summary(session, run),
            }

    def start_run(self, run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = _run(session, run_id)
            if run.expected_units <= 0:
                raise ConflictError("Import eval units before starting the run")
            self._release_expired(session, run.id)
            self._set_running_or_terminal(session, run)
            _event(session, run, "eval.run_started", {"run_id": run.id})
            session.commit()
            return self._overview(session, run)

    def pause_run(self, run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            run = _run(session, run_id)
            if run.status == "completed":
                raise ConflictError("A completed eval run cannot be paused")
            run.status = "paused"
            run.completed_at = None
            _event(session, run, "eval.run_paused", {"run_id": run.id})
            session.commit()
            return self._overview(session, run)

    def claim(self, run_id: str, payload: schemas.EvalClaimRequest) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            run = _run(session, run_id)
            self._release_expired(session, run.id, now=now)
            if run.status != "running":
                raise ConflictError(f"Eval run is {run.status}, not running")
            limit = 1 if run.mode == "canonical" else min(payload.limit, run.batch_size)
            eligible = (
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status.in_(CLAIMABLE_UNIT_STATUSES),
                models.EvalUnit.attempt_count < models.EvalUnit.max_attempts,
            )
            candidate_ids = (
                select(models.EvalUnit.id)
                .where(*eligible)
                .order_by(
                    models.EvalUnit.priority.desc(),
                    models.EvalUnit.case_key,
                    models.EvalUnit.rubric_key,
                )
                .limit(limit)
            )
            if run.mode == "fast":
                first_material = (
                    select(models.EvalUnit.material_key)
                    .where(*eligible)
                    .order_by(
                        models.EvalUnit.priority.desc(),
                        models.EvalUnit.case_key,
                        models.EvalUnit.rubric_key,
                    )
                    .limit(1)
                    .scalar_subquery()
                )
                candidate_ids = (
                    select(models.EvalUnit.id)
                    .where(*eligible, models.EvalUnit.material_key == first_material)
                    .order_by(
                        models.EvalUnit.priority.desc(),
                        models.EvalUnit.case_key,
                        models.EvalUnit.rubric_key,
                    )
                    .limit(limit)
                )
            expires_at = now + timedelta(seconds=payload.lease_seconds)
            lease_token = str(uuid.uuid4())
            claimed_ids = list(
                session.scalars(
                    update(models.EvalUnit)
                    .where(
                        models.EvalUnit.id.in_(candidate_ids),
                        models.EvalUnit.status.in_(CLAIMABLE_UNIT_STATUSES),
                    )
                    .values(
                        status="leased",
                        lease_token=lease_token,
                        lease_owner=payload.worker_id,
                        lease_expires_at=expires_at,
                        heartbeat_at=now,
                        attempt_count=models.EvalUnit.attempt_count + 1,
                        started_at=func.coalesce(models.EvalUnit.started_at, now),
                        error_category=None,
                        error_message=None,
                    )
                    .returning(models.EvalUnit.id)
                )
            )
            claimed = list(
                session.scalars(
                    select(models.EvalUnit)
                    .where(models.EvalUnit.id.in_(claimed_ids))
                    .order_by(
                        models.EvalUnit.priority.desc(),
                        models.EvalUnit.case_key,
                        models.EvalUnit.rubric_key,
                    )
                )
            )
            if not claimed:
                self._finalize_if_terminal(session, run)
            session.commit()
            return {
                "run": {
                    "id": run.id,
                    "adapter": run.adapter,
                    "mode": run.mode,
                    "candidate_id": run.candidate_id,
                    "config_hash": run.config_hash,
                    "config_json": run.config_json,
                    "tool_parser": run.tool_parser,
                },
                "worker_id": payload.worker_id,
                "units": [
                    {
                        "id": unit.id,
                        "lease_token": unit.lease_token,
                        "case_key": unit.case_key,
                        "domain": unit.domain,
                        "rubric_key": unit.rubric_key,
                        "rubric_group": unit.rubric_group,
                        "material_key": unit.material_key,
                        "attempt_count": unit.attempt_count,
                        "lease_expires_at": unit.lease_expires_at,
                        "input_json": unit.input_json,
                    }
                    for unit in claimed
                ],
            }

    def complete_unit(
        self, unit_id: str, payload: schemas.EvalUnitComplete
    ) -> models.EvalUnit:
        _check_json_size(payload.result_json)
        now = datetime.now(UTC)
        with self.session_factory() as session:
            unit = _unit(session, unit_id)
            _validate_lease(unit, payload.lease_token, now)
            unit.status = "succeeded"
            unit.score = payload.score
            unit.result_json = _sanitize_json(payload.result_json)
            unit.duration_ms = payload.duration_ms
            unit.cache_hit = payload.cache_hit
            unit.completed_at = now
            unit.error_category = None
            unit.error_message = None
            _clear_lease(unit)
            run = _run(session, unit.run_id)
            self._finalize_if_terminal(session, run)
            session.commit()
            return unit

    def fail_unit(self, unit_id: str, payload: schemas.EvalUnitFailure) -> models.EvalUnit:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            unit = _unit(session, unit_id)
            _validate_lease(unit, payload.lease_token, now)
            can_retry = payload.retryable and unit.attempt_count < unit.max_attempts
            unit.status = "retryable" if can_retry else "quarantined"
            unit.error_category = payload.category.strip() or "worker_error"
            unit.error_message = redact(payload.message)[:8000]
            unit.duration_ms = payload.duration_ms
            unit.completed_at = None if can_retry else now
            _clear_lease(unit)
            run = _run(session, unit.run_id)
            self._finalize_if_terminal(session, run)
            session.commit()
            return unit

    def heartbeat_unit(
        self, unit_id: str, payload: schemas.EvalUnitHeartbeat
    ) -> models.EvalUnit:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            unit = _unit(session, unit_id)
            _validate_lease(unit, payload.lease_token, now)
            unit.heartbeat_at = now
            unit.lease_expires_at = now + timedelta(seconds=payload.lease_seconds)
            session.commit()
            return unit

    def requeue(self, run_id: str, payload: schemas.EvalRequeue) -> dict[str, Any]:
        with self.session_factory() as session:
            run = _run(session, run_id)
            query = select(models.EvalUnit).where(
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status == "quarantined",
            )
            if payload.unit_ids:
                query = query.where(models.EvalUnit.id.in_(payload.unit_ids))
            if payload.error_category:
                query = query.where(models.EvalUnit.error_category == payload.error_category)
            elif not payload.unit_ids:
                query = query.where(models.EvalUnit.error_category != "missing_artifact")
            units = list(session.scalars(query))
            for unit in units:
                _reset_unit(unit)
            if units:
                run.status = "running"
                run.completed_at = None
                run.started_at = run.started_at or datetime.now(UTC)
            _event(
                session,
                run,
                "eval.units_requeued",
                {"run_id": run.id, "units": len(units)},
            )
            session.commit()
            overview = self._overview(session, run)
            overview["requeued_units"] = len(units)
            return overview

    def audit(self, run_id: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            run = _run(session, run_id)
            summary = self._summary(session, run, now=now)
            issues: list[dict[str, Any]] = []
            if summary["total_units"] != run.expected_units:
                issues.append(
                    {
                        "severity": "error",
                        "code": "unit_count_mismatch",
                        "expected": run.expected_units,
                        "actual": summary["total_units"],
                    }
                )
            if run.expected_units <= 0:
                issues.append({"severity": "error", "code": "empty_run"})
            elif summary["completed_units"] != run.expected_units:
                issues.append(
                    {
                        "severity": "error",
                        "code": "incomplete_units",
                        "expected": run.expected_units,
                        "completed": summary["completed_units"],
                        "remaining": max(
                            run.expected_units - summary["completed_units"], 0
                        ),
                    }
                )
            invalid_scores = session.scalar(
                select(func.count())
                .select_from(models.EvalUnit)
                .where(
                    models.EvalUnit.run_id == run.id,
                    models.EvalUnit.status == "succeeded",
                    (
                        models.EvalUnit.score.is_(None)
                        | (models.EvalUnit.score < 0)
                        | (models.EvalUnit.score > 1)
                    ),
                )
            ) or 0
            if invalid_scores:
                issues.append(
                    {
                        "severity": "error",
                        "code": "invalid_scores",
                        "count": invalid_scores,
                    }
                )
            expired = session.scalar(
                select(func.count())
                .select_from(models.EvalUnit)
                .where(
                    models.EvalUnit.run_id == run.id,
                    models.EvalUnit.status == "leased",
                    models.EvalUnit.lease_expires_at <= now,
                )
            ) or 0
            if expired:
                issues.append(
                    {"severity": "error", "code": "expired_leases", "count": expired}
                )
            if summary["quarantined_units"]:
                issues.append(
                    {
                        "severity": "error",
                        "code": "quarantined_units",
                        "count": summary["quarantined_units"],
                        "categories": summary["error_counts"],
                    }
                )
            if run.adapter == "presentbench" and not run.tool_parser:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "tool_parser_disabled",
                        "message": "PresentBench run was configured without tool_parser",
                    }
                )
            return {
                "run_id": run.id,
                "valid": not any(issue["severity"] == "error" for issue in issues),
                "issues": issues,
                "summary": summary,
            }

    def _overview(self, session: Session, run: models.EvalRun) -> dict[str, Any]:
        return {"run": run, "summary": self._summary(session, run)}

    def _summary(
        self, session: Session, run: models.EvalRun, *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        status_counts = {
            str(status): int(count)
            for status, count in session.execute(
                select(models.EvalUnit.status, func.count())
                .where(models.EvalUnit.run_id == run.id)
                .group_by(models.EvalUnit.status)
            )
        }
        error_counts = {
            str(category): int(count)
            for category, count in session.execute(
                select(models.EvalUnit.error_category, func.count())
                .where(
                    models.EvalUnit.run_id == run.id,
                    models.EvalUnit.error_category.is_not(None),
                )
                .group_by(models.EvalUnit.error_category)
            )
        }
        aggregate = session.execute(
            select(
                func.count(models.EvalUnit.id),
                func.count(distinct(models.EvalUnit.case_key)),
                func.coalesce(func.sum(models.EvalUnit.attempt_count), 0),
                func.coalesce(func.sum(cast(models.EvalUnit.cache_hit, Integer)), 0),
                func.avg(models.EvalUnit.score),
            ).where(models.EvalUnit.run_id == run.id)
        ).one()
        active_workers = session.scalar(
            select(func.count(distinct(models.EvalUnit.lease_owner))).where(
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status == "leased",
                models.EvalUnit.lease_expires_at > now,
            )
        ) or 0
        total = int(aggregate[0] or 0)
        completed = status_counts.get("succeeded", 0)
        elapsed_minutes: float | None = None
        if run.started_at is not None:
            elapsed_minutes = max((_as_utc(now) - _as_utc(run.started_at)).total_seconds() / 60, 0)
        throughput = (
            completed / elapsed_minutes
            if elapsed_minutes is not None and elapsed_minutes > 0 and completed > 0
            else None
        )
        remaining = max(run.expected_units - completed, 0)
        eta_seconds = int(remaining / throughput * 60) if throughput and remaining else None
        mean_score = float(aggregate[4]) if aggregate[4] is not None else None
        if run.adapter == "presentbench":
            aggregate_score, aggregate_cases = self._presentbench_aggregate(session, run.id)
        else:
            aggregate_score = mean_score
            aggregate_cases = int(
                session.scalar(
                    select(func.count(distinct(models.EvalUnit.case_key))).where(
                        models.EvalUnit.run_id == run.id,
                        models.EvalUnit.status == "succeeded",
                    )
                )
                or 0
            )
        return {
            "total_units": total,
            "expected_units": run.expected_units,
            "total_cases": int(aggregate[1] or 0),
            "status_counts": status_counts,
            "error_counts": error_counts,
            "completed_units": completed,
            "quarantined_units": status_counts.get("quarantined", 0),
            "active_workers": int(active_workers),
            "cache_hits": int(aggregate[3] or 0),
            "attempts": int(aggregate[2] or 0),
            "completion_ratio": completed / run.expected_units if run.expected_units else 0.0,
            "mean_score": mean_score,
            "aggregate_score": aggregate_score,
            "aggregate_cases": aggregate_cases,
            "throughput_units_per_minute": throughput,
            "eta_seconds": eta_seconds,
        }

    @staticmethod
    def _presentbench_aggregate(session: Session, run_id: str) -> tuple[float | None, int]:
        totals = list(
            session.execute(
                select(
                    models.EvalUnit.case_key,
                    models.EvalUnit.rubric_group,
                    func.count(),
                )
                .where(models.EvalUnit.run_id == run_id)
                .group_by(models.EvalUnit.case_key, models.EvalUnit.rubric_group)
            )
        )
        successes = {
            (str(case_key), str(rubric_group)): (int(count), float(mean_score))
            for case_key, rubric_group, count, mean_score in session.execute(
                select(
                    models.EvalUnit.case_key,
                    models.EvalUnit.rubric_group,
                    func.count(),
                    func.avg(models.EvalUnit.score),
                )
                .where(
                    models.EvalUnit.run_id == run_id,
                    models.EvalUnit.status == "succeeded",
                    models.EvalUnit.score.is_not(None),
                )
                .group_by(models.EvalUnit.case_key, models.EvalUnit.rubric_group)
            )
        }
        case_groups: dict[str, list[float] | None] = {}
        for case_key, rubric_group, total in totals:
            key = str(case_key)
            groups = case_groups.setdefault(key, [])
            success = successes.get((key, str(rubric_group)))
            if groups is None:
                continue
            if success is None or success[0] != int(total):
                case_groups[key] = None
                continue
            groups.append(success[1])
        case_scores = [sum(groups) / len(groups) for groups in case_groups.values() if groups]
        return (
            sum(case_scores) / len(case_scores) if case_scores else None,
            len(case_scores),
        )

    def _release_expired(
        self, session: Session, run_id: str, *, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(UTC)
        units = list(
            session.scalars(
                select(models.EvalUnit).where(
                    models.EvalUnit.run_id == run_id,
                    models.EvalUnit.status == "leased",
                    models.EvalUnit.lease_expires_at <= now,
                )
            )
        )
        for unit in units:
            if unit.attempt_count < unit.max_attempts:
                unit.status = "retryable"
                unit.completed_at = None
            else:
                unit.status = "quarantined"
                unit.completed_at = now
            unit.error_category = "lease_expired"
            unit.error_message = "Worker lease expired before a terminal result was recorded"
            _clear_lease(unit)
        return len(units)

    def _set_running_or_terminal(self, session: Session, run: models.EvalRun) -> None:
        self._finalize_if_terminal(session, run)
        nonterminal = session.scalar(
            select(func.count())
            .select_from(models.EvalUnit)
            .where(
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status.not_in(TERMINAL_UNIT_STATUSES),
            )
        ) or 0
        if nonterminal:
            run.status = "running"
            run.started_at = run.started_at or datetime.now(UTC)
            run.completed_at = None

    def _finalize_if_terminal(self, session: Session, run: models.EvalRun) -> None:
        session.flush()
        nonterminal = session.scalar(
            select(func.count())
            .select_from(models.EvalUnit)
            .where(
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status.not_in(TERMINAL_UNIT_STATUSES),
            )
        ) or 0
        if nonterminal or run.expected_units <= 0:
            return
        quarantined = session.scalar(
            select(func.count())
            .select_from(models.EvalUnit)
            .where(
                models.EvalUnit.run_id == run.id,
                models.EvalUnit.status == "quarantined",
            )
        ) or 0
        run.status = "needs_review" if quarantined else "completed"
        run.completed_at = datetime.now(UTC)

    @staticmethod
    def _cache_sources(
        session: Session, run_id: str, unit_keys: set[str]
    ) -> dict[str, models.EvalUnit]:
        cached: dict[str, models.EvalUnit] = {}
        keys = sorted(unit_keys)
        for start in range(0, len(keys), 500):
            rows = session.scalars(
                select(models.EvalUnit)
                .where(
                    models.EvalUnit.run_id != run_id,
                    models.EvalUnit.status == "succeeded",
                    models.EvalUnit.unit_key.in_(keys[start : start + 500]),
                )
                .order_by(models.EvalUnit.completed_at.desc())
            )
            for row in rows:
                cached.setdefault(row.unit_key, row)
        return cached


def _presentbench_specs(
    records: list[dict[str, Any]],
    *,
    result_root: Path,
    candidate_filename: str,
    config_hash: str,
    max_attempts: int,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    for record in records:
        case_key = str(record.get("task_id") or "").strip()
        if not case_key:
            raise ConflictError("PresentBench case is missing task_id")
        if case_key in seen_cases:
            raise ConflictError(f"PresentBench task_id is duplicated: {case_key}")
        seen_cases.add(case_key)
        domain_values = record.get("domains") or []
        domain = str(domain_values[0]) if domain_values else case_key.split("/", 1)[0]
        query_path = str(record.get("query_path") or "")
        case_dir_value = record.get("presentbench_case_dir")
        if not case_dir_value and query_path:
            case_dir_value = str(Path(query_path).expanduser().resolve().parent.parent)
        case_dir = _existing_directory(
            str(case_dir_value or ""),
            f"PresentBench case directory for {case_key}",
        )
        judge_path = _existing_file(
            str(case_dir / "generation_task" / "judge_prompt.json"),
            f"PresentBench judge prompt for {case_key}",
        )
        if judge_path.stat().st_size > MAX_JUDGE_FILE_BYTES:
            raise ConflictError(f"PresentBench judge prompt is too large: {judge_path}")
        try:
            judge = json.loads(judge_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConflictError(f"Invalid PresentBench judge prompt: {judge_path}") from exc
        if not isinstance(judge, dict):
            raise ConflictError(f"PresentBench judge prompt must be an object: {judge_path}")
        common_path = _presentbench_common_prompt(case_dir, domain)
        if common_path.stat().st_size > MAX_JUDGE_FILE_BYTES:
            raise ConflictError(f"PresentBench common judge prompt is too large: {common_path}")
        try:
            common = json.loads(common_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConflictError(f"Invalid PresentBench common judge prompt: {common_path}") from exc
        if not isinstance(common, dict):
            raise ConflictError(
                f"PresentBench common judge prompt must be an object: {common_path}"
            )
        output_dir = Path(str(record.get("output_dir") or ""))
        if output_dir.is_absolute() or ".." in output_dir.parts:
            raise ConflictError(f"PresentBench output_dir must be relative: {case_key}")
        candidate_path = (result_root / output_dir / candidate_filename).resolve()
        try:
            candidate_path.relative_to(result_root)
        except ValueError as exc:
            raise ConflictError(f"PresentBench candidate escapes result root: {case_key}") from exc
        candidate_digest = _stable_file_digest(candidate_path) if candidate_path.is_file() else None
        rendered_path = candidate_path.with_name("slides.pdf")
        rendered_digest = _stable_file_digest(rendered_path) if rendered_path.is_file() else None
        references = [str(item) for item in record.get("reference_paths") or []]
        slide_count = _pptx_slide_count(candidate_path) if candidate_digest is not None else None
        raw_slide_checks = common.get("material_dependent_checklist_3")
        slide_checks = raw_slide_checks if isinstance(raw_slide_checks, list) else []
        if slide_count is not None and slide_checks:
            slide_checks = slide_checks[:slide_count]
        material_key = _json_digest(
            {
                "case": case_key,
                "query": _path_fingerprint(query_path),
                "references": [_path_fingerprint(path) for path in references],
                "judge": _stable_file_digest(judge_path),
                "common_judge": _stable_file_digest(common_path),
                "candidate": candidate_digest,
                "rendered": rendered_digest,
            }
        )
        checklists: list[tuple[str, list[Any], str, str]] = [
            (
                "material_independent_checklist_1",
                common.get("material_independent_checklist_1", []),
                str(common.get("material_independent_prefix") or ""),
                "material_independent",
            ),
            (
                "material_independent_checklist_2",
                common.get("material_independent_checklist_2", []),
                str(common.get("material_independent_prefix") or ""),
                "material_independent",
            ),
            (
                "material_dependent_checklist_1",
                judge.get("material_dependent_checklist_1", []),
                str(common.get("material_dependent_prefix") or ""),
                "material_dependent",
            ),
            (
                "material_dependent_checklist_2",
                judge.get("material_dependent_checklist_2", []),
                str(common.get("material_dependent_prefix") or ""),
                "material_dependent",
            ),
            (
                "material_dependent_checklist_3",
                slide_checks,
                str(common.get("material_dependent_prefix") or ""),
                "material_dependent",
            ),
        ]
        if not all(
            isinstance(questions, list)
            for _key, questions, _prefix, _scope in checklists
        ):
            raise ConflictError(f"PresentBench judge checklists must be arrays: {case_key}")
        if not any(questions for _key, questions, _prefix, _scope in checklists):
            raise ConflictError(f"PresentBench case has no material-dependent rubrics: {case_key}")
        for checklist, questions, prefix, rubric_scope in checklists:
            for index, raw_question in enumerate(questions):
                valid_text = isinstance(raw_question, str) and bool(raw_question.strip())
                valid_callable = isinstance(raw_question, dict) and bool(raw_question)
                if not valid_text and not valid_callable:
                    raise ConflictError(
                        f"PresentBench rubric {checklist}[{index}] is invalid: {case_key}"
                    )
                rubric_key = f"{checklist}:{index:04d}"
                unit_key = _json_digest(
                    {
                        "candidate_digest": candidate_digest,
                        "case_key": case_key,
                        "rubric_key": rubric_key,
                        "rubric_digest": _json_digest(raw_question),
                        "material_digest": material_key,
                        "config_hash": config_hash,
                    }
                )
                missing = candidate_digest is None or rendered_digest is None
                missing_paths = [
                    str(path)
                    for path, digest in (
                        (candidate_path, candidate_digest),
                        (rendered_path, rendered_digest),
                    )
                    if digest is None
                ]
                seeded = (
                    _presentbench_callable_result(raw_question, slide_count)
                    if valid_callable and not missing
                    else None
                )
                status = "quarantined" if missing else "succeeded" if seeded else "pending"
                specs.append(
                    {
                        "case_key": case_key,
                        "domain": domain,
                        "rubric_key": rubric_key,
                        "rubric_group": checklist,
                        "unit_key": unit_key,
                        "material_key": material_key,
                        "priority": 50,
                        "status": status,
                        "input_json": {
                            "task_id": case_key,
                            "source": record.get("source"),
                            "query_path": query_path,
                            "reference_paths": references,
                            "case_dir": str(case_dir),
                            "candidate_uri": str(candidate_path),
                            "candidate_digest": candidate_digest,
                            "rendered_uri": str(rendered_path),
                            "rendered_digest": rendered_digest,
                            "checklist": checklist,
                            "rubric_index": index,
                            "rubric_scope": rubric_scope,
                            "rubric_kind": "code" if valid_callable else "llm",
                            "prompt_prefix": prefix,
                            "rubric": raw_question,
                        },
                        "result_json": seeded[1] if seeded else {},
                        "score": seeded[0] if seeded else None,
                        "attempt_count": 0,
                        "max_attempts": max_attempts,
                        "lease_token": None,
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "error_category": "missing_artifact" if missing else None,
                        "error_message": (
                            f"Candidate artifacts do not exist: {', '.join(missing_paths)}"
                            if missing
                            else None
                        ),
                        "duration_ms": 0 if seeded else None,
                        "cache_hit": False,
                        "cache_source_unit_id": None,
                        "started_at": None,
                        "completed_at": (
                            datetime.now(UTC) if status in TERMINAL_UNIT_STATUSES else None
                        ),
                    }
                )
    return specs


def _presentbench_common_prompt(case_dir: Path, domain: str) -> Path:
    for directory in (case_dir, *case_dir.parents):
        if directory.name != domain:
            continue
        candidate = directory / "common_judge_prompt.json"
        if candidate.is_file():
            return candidate.resolve()
    raise ConflictError(
        f"PresentBench common judge prompt does not exist for domain {domain}: {case_dir}"
    )


def _pptx_slide_count(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as archive:
            count = sum(
                bool(re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
                for name in archive.namelist()
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConflictError(f"PresentBench candidate is not a readable PPTX: {path}") from exc
    if count <= 0:
        raise ConflictError(f"PresentBench candidate has no slides: {path}")
    return count


def _presentbench_callable_result(
    rubric: dict[str, Any], slide_count: int | None
) -> tuple[float, dict[str, Any]] | None:
    if slide_count is None or str(rubric.get("func") or "").split(".")[-1] != "check_slide_count":
        return None
    keywords = rubric.get("keywords") if isinstance(rubric.get("keywords"), dict) else {}
    minimum = int(keywords.get("min_count", 0))
    maximum = int(keywords.get("max_count", 2**31 - 1))
    passed = minimum <= slide_count <= maximum
    return (
        1.0 if passed else 0.0,
        {
            "answer": "yes" if passed else "no",
            "explanation": (
                f"[judged by harness code] slide_count={slide_count}, "
                f"required_range=[{minimum}, {maximum}]"
            ),
            "evaluator": "presentbench.check_slide_count",
        },
    )


def _path_fingerprint(value: str) -> dict[str, str | None]:
    if not value.strip():
        return {"uri": value, "sha256": None}
    path = Path(value).expanduser().resolve()
    return {
        "uri": str(path),
        "sha256": _stable_file_digest(path) if path.is_file() else None,
    }


def _generic_specs(units: list[schemas.EvalUnitSeed]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for unit in units:
        _reject_secret_material(unit.input_json)
        input_json = _sanitize_json(unit.input_json)
        specs.append(
            {
                "case_key": unit.case_key.strip(),
                "domain": unit.domain.strip() if unit.domain else None,
                "rubric_key": unit.rubric_key.strip(),
                "rubric_group": (
                    unit.rubric_group.strip()
                    if unit.rubric_group
                    else unit.rubric_key.split(":", 1)[0].strip()
                ),
                "candidate_digest": unit.candidate_digest,
                "material_key": _json_digest(unit.material_key or unit.case_key),
                "priority": unit.priority,
                "input_json": input_json,
            }
        )
    return specs


def _read_jsonl(path: Path, *, limit: int | None) -> list[dict[str, Any]]:
    if path.stat().st_size > MAX_TASK_FILE_BYTES:
        raise ConflictError(f"PresentBench task file is too large: {path}")
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ConflictError(
                        f"PresentBench task line {line_number} must contain an object"
                    )
                records.append(value)
                if limit is not None and len(records) >= limit:
                    break
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConflictError(f"Invalid PresentBench task file: {path}") from exc
    return records


def _work(session: Session, identity: str) -> models.WorkItem:
    work = session.get(models.WorkItem, identity)
    if work is None:
        work = session.scalar(
            select(models.WorkItem).where(models.WorkItem.human_id == identity.upper())
        )
    if work is None:
        raise NotFoundError("Work item not found")
    return work


def _run(session: Session, run_id: str) -> models.EvalRun:
    run = session.get(models.EvalRun, run_id)
    if run is None:
        raise NotFoundError("Eval run not found")
    return run


def _unit(session: Session, unit_id: str) -> models.EvalUnit:
    unit = session.get(models.EvalUnit, unit_id)
    if unit is None:
        raise NotFoundError("Eval unit not found")
    return unit


def _existing_file(value: str, label: str) -> Path:
    if not value.strip():
        raise ConflictError(f"{label} is required")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ConflictError(f"{label} does not exist: {path}")
    return path


def _existing_directory(value: str, label: str) -> Path:
    if not value.strip():
        raise ConflictError(f"{label} is required")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ConflictError(f"{label} does not exist: {path}")
    return path


def _json_digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_digest(path: Path) -> str:
    stat = path.stat()
    return _cached_file_digest(
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@lru_cache(maxsize=32_768)
def _cached_file_digest(
    path: str,
    _size: int,
    _mtime_ns: int,
    _ctime_ns: int,
) -> str:
    return _file_digest(Path(path))


def _validate_lease(unit: models.EvalUnit, lease_token: str, now: datetime) -> None:
    if unit.status != "leased" or unit.lease_token != lease_token:
        raise ConflictError("Eval unit lease is no longer active")
    if unit.lease_expires_at is None or _as_utc(unit.lease_expires_at) <= _as_utc(now):
        raise ConflictError("Eval unit lease has expired")


def _clear_lease(unit: models.EvalUnit) -> None:
    unit.lease_token = None
    unit.lease_owner = None
    unit.lease_expires_at = None
    unit.heartbeat_at = None


def _reset_unit(unit: models.EvalUnit, *, status: str = "pending") -> None:
    unit.status = status
    unit.result_json = {}
    unit.score = None
    unit.attempt_count = 0
    unit.error_category = None if status == "pending" else unit.error_category
    unit.error_message = None if status == "pending" else unit.error_message
    unit.duration_ms = None
    unit.cache_hit = False
    unit.cache_source_unit_id = None
    unit.started_at = None
    unit.completed_at = datetime.now(UTC) if status == "quarantined" else None
    _clear_lease(unit)


def _apply_seeded_result(unit: models.EvalUnit, spec: dict[str, Any]) -> None:
    unit.status = "succeeded"
    unit.result_json = dict(spec["result_json"])
    unit.score = spec["score"]
    unit.attempt_count = 0
    unit.error_category = None
    unit.error_message = None
    unit.duration_ms = 0
    unit.cache_hit = False
    unit.cache_source_unit_id = None
    unit.started_at = None
    unit.completed_at = spec["completed_at"] or datetime.now(UTC)
    _clear_lease(unit)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _check_json_size(value: dict[str, Any]) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode())
    except (TypeError, ValueError) as exc:
        raise ConflictError("Eval result must be JSON serializable") from exc
    if size > MAX_RESULT_BYTES:
        raise ConflictError(f"Eval result exceeds {MAX_RESULT_BYTES} bytes")


def _reject_secret_material(value: Any, *, key: str | None = None) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            normalized = str(raw_key).lower().replace("-", "_")
            if (
                normalized in SECRET_KEYS
                and not normalized.endswith("_env")
                and item is not None
                and item != ""
            ):
                raise ConflictError(
                    "Eval config must reference credentials through environment variables"
                )
            _reject_secret_material(item, key=normalized)
        return
    if isinstance(value, list):
        for item in value:
            _reject_secret_material(item, key=key)
        return
    if isinstance(value, str) and redact(value) != value:
        raise ConflictError("Eval config contains credential-like material")


def _sanitize_json(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, item in value.items():
            normalized = str(raw_key).lower().replace("-", "_")
            sanitized[str(raw_key)] = (
                "[REDACTED]"
                if normalized in SECRET_KEYS and not normalized.endswith("_env")
                else _sanitize_json(item, key=normalized)
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_json(item, key=key) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _event(
    session: Session,
    run: models.EvalRun,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        models.ActivityEvent(
            scope_type="work",
            scope_id=run.work_item_id,
            event_type=event_type,
            actor="eval-harness",
            payload=payload,
        )
    )
