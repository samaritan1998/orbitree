from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.ltp import (
    BundleInspector,
    BundleSpec,
    BundleValidationError,
    LtpSubmissionError,
    LtpSubmissionUncertain,
    LtpSubmitAdapter,
)
from research_ops.settings import Settings

from .cluster_overview import ClusterOverviewService, ClusterOverviewUnavailable
from .core import ConflictError, NotFoundError

REGULAR_B_POOLS = ("cluster-a", "cluster-b", "cluster-c")
BOOK_B_POOLS = (
    "cluster-reserved-a",
    "cluster-reserved-b",
    "cluster-reserved-c",
)
H100_POOLS = ("cluster-d",)
logger = logging.getLogger(__name__)


class SchedulingUnavailable(RuntimeError):
    """A retryable failure to obtain trustworthy placement evidence."""


def _resource_hardware(value: object) -> str:
    normalized = str(value or "").strip().casefold().replace("-", "")
    if normalized.startswith("h100") or normalized in {"h", "hcard"}:
        return "h100"
    if normalized.startswith("b"):
        return "b"
    return "unknown"


def select_hidden_cluster(
    overview: dict[str, Any], *, hardware: str, required_cards: int
) -> tuple[str, dict[str, Any]]:
    """Choose a compatible pool while keeping its identity out of public schemas."""
    if required_cards <= 0:
        raise ValueError("required_cards must be positive")
    source = overview.get("source") or {}
    if source.get("stale"):
        raise SchedulingUnavailable("集群容量快照已过期，系统将稍后重试。")
    requested_hardware = hardware.casefold()
    supported = H100_POOLS if requested_hardware == "h100" else (*REGULAR_B_POOLS, *BOOK_B_POOLS)
    by_name: dict[str, dict[str, Any]] = {}
    for cluster in overview.get("clusters") or []:
        if not isinstance(cluster, dict) or cluster.get("name") not in supported:
            continue
        resource = cluster.get("resource")
        if not isinstance(resource, dict):
            continue
        if _resource_hardware(resource.get("hardware")) != requested_hardware:
            continue
        by_name[str(cluster["name"])] = resource
    candidates = [name for name in supported if name in by_name]
    if not candidates:
        raise SchedulingUnavailable("没有可核验的同硬件资源池，系统将稍后重试。")

    def available(name: str) -> int:
        return max(0, int(float(by_name[name].get("available_cards") or 0)))

    if requested_hardware == "h100":
        selected = "cluster-d"
    else:
        covering_regular = [
            name
            for name in REGULAR_B_POOLS
            if name in by_name and available(name) >= required_cards
        ]
        covering_book = [
            name for name in BOOK_B_POOLS if name in by_name and available(name) >= required_cards
        ]
        if covering_regular:
            selected = covering_regular[0]
        elif covering_book:
            selected = max(
                covering_book,
                key=lambda name: (available(name), -BOOK_B_POOLS.index(name)),
            )
        else:
            stable_order = (*REGULAR_B_POOLS, *BOOK_B_POOLS)
            selected = max(
                candidates,
                key=lambda name: (available(name), -stable_order.index(name)),
            )
    selected_available = available(selected)
    evidence = {
        "observed_at": str(overview.get("resources_observed_at") or overview.get("observed_at")),
        "required_cards": required_cards,
        "selected_cluster": selected,
        "available_cards": selected_available,
        "shortfall_cards": max(required_cards - selected_available, 0),
        "capacity_sufficient": selected_available >= required_cards,
        "policy": "hardware-compatible-regular-first-v1",
        "candidates": [
            {
                "cluster": name,
                "hardware": requested_hardware,
                "scheduling_class": by_name[name].get("scheduling_class"),
                "available_cards": available(name),
            }
            for name in candidates
        ],
    }
    return selected, evidence


def _overview_tasks(overview: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    tasks: list[tuple[str, dict[str, Any]]] = []
    for cluster in overview.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        cluster_name = str(cluster.get("name") or "")
        for bucket in ("running_jobs", "queued_jobs"):
            for task in cluster.get(bucket) or []:
                if isinstance(task, dict):
                    tasks.append((cluster_name, task))
    return tasks


def _matching_names(
    overview: dict[str, Any],
    *,
    submitter: str,
    cluster: str,
    job_name_base: str,
) -> set[str]:
    return {
        str(task.get("name") or "")
        for cluster_name, task in _overview_tasks(overview)
        if cluster_name == cluster
        and str(task.get("user") or "") == submitter
        and (
            str(task.get("name") or "") == job_name_base
            or str(task.get("name") or "").startswith(f"{job_name_base}-")
        )
    }


class ManagedJobService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        cluster_overview: ClusterOverviewService,
        *,
        inspector: BundleInspector | None = None,
        submit_adapter: LtpSubmitAdapter | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cluster_overview = cluster_overview
        self.inspector = inspector or BundleInspector(
            allowed_roots=settings.managed_job_allowed_roots,
            check_script=settings.managed_job_bundle_check_script,
            training_preflight_script=settings.managed_job_training_preflight_script,
        )
        self.submit_adapter = submit_adapter or LtpSubmitAdapter(
            command=settings.managed_job_ltp_cli,
            timeout_seconds=settings.managed_job_submit_timeout_seconds,
        )
        self._dispatch_lock = threading.Lock()

    def submit(self, payload: schemas.ManagedJobSubmit) -> dict[str, Any]:
        if not self.settings.managed_jobs_enabled:
            raise ConflictError("统一任务入口尚未启用。")
        bundle = self._inspect_payload(payload)
        request_fingerprint = self._request_fingerprint(payload, bundle)
        with self.session_factory() as session:
            work_item = self._work(session, payload.work_item_id)
            existing = session.scalar(
                select(models.ManagedJob).where(
                    models.ManagedJob.submitter == payload.submitter,
                    models.ManagedJob.idempotency_key == payload.idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ConflictError("幂等键已用于不同的任务请求。")
                return self._public(session, existing)
            job = models.ManagedJob(
                work_item_id=work_item.id,
                submitter=payload.submitter,
                display_name=payload.display_name or bundle.job_name,
                job_kind=payload.job_kind,
                hardware=payload.hardware,
                status="accepted",
                idempotency_key=payload.idempotency_key,
                request_fingerprint=request_fingerprint,
                config_path=str(bundle.config_path),
                run_path=str(bundle.run_path),
                train_path=str(bundle.train_path) if bundle.train_path else None,
                preflight_receipt_path=(
                    str(bundle.preflight_receipt_path)
                    if bundle.preflight_receipt_path
                    else None
                ),
                config_sha256=bundle.config_sha256,
                run_sha256=bundle.run_sha256,
                train_sha256=bundle.train_sha256,
                requested_nodes=bundle.requested_nodes,
                gpus_per_node=bundle.gpus_per_node,
                requested_cards=bundle.requested_cards,
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(models.ManagedJob).where(
                        models.ManagedJob.submitter == payload.submitter,
                        models.ManagedJob.idempotency_key == payload.idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if existing.request_fingerprint != request_fingerprint:
                    raise ConflictError("幂等键已用于不同的任务请求。") from None
                return self._public(session, existing)
            return self._public(session, job)

    def get(self, job_id: str, *, submitter: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(models.ManagedJob, job_id)
            if job is None or job.submitter != submitter:
                raise NotFoundError("Logical job not found")
            return self._public(session, job)

    def list(self, *, submitter: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            jobs = list(
                session.scalars(
                    select(models.ManagedJob)
                    .where(models.ManagedJob.submitter == submitter)
                    .order_by(models.ManagedJob.created_at.desc())
                    .limit(limit)
                )
            )
            return [self._public(session, job) for job in jobs]

    def recover_interrupted_dispatches(self) -> int:
        """Never replay a dispatch whose external side effect is unknown."""
        with self.session_factory() as session:
            jobs = list(
                session.scalars(
                    select(models.ManagedJob).where(models.ManagedJob.status == "dispatching")
                )
            )
            for job in jobs:
                job.status = "needs_review"
                job.error_code = "dispatch_interrupted"
                job.error_message = "调度进程在提交期间中断，系统不会自动重试。"
            if jobs:
                session.commit()
            return len(jobs)

    def dispatch_one(self) -> int:
        if not self.settings.managed_job_submit_enabled:
            return 0
        if not self._dispatch_lock.acquire(blocking=False):
            return 0
        try:
            with self.session_factory() as session:
                job = session.scalar(
                    select(models.ManagedJob)
                    .where(models.ManagedJob.status == "accepted")
                    .order_by(models.ManagedJob.created_at)
                    .limit(1)
                )
                if job is None:
                    return 0
                job.status = "dispatching"
                job.error_code = None
                job.error_message = None
                session.commit()
                job_id = job.id
            try:
                self._dispatch(job_id)
            except Exception:
                logger.exception("Managed job dispatch failed after claim: %s", job_id)
                try:
                    self._mark(
                        job_id,
                        status="needs_review",
                        code="dispatch_persistence_unknown",
                        message="外部提交后的状态保存失败，系统不会自动重试。",
                    )
                except Exception:
                    logger.exception("Managed job recovery mark failed: %s", job_id)
            return 1
        finally:
            self._dispatch_lock.release()

    def _dispatch(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(models.ManagedJob, job_id)
            if job is None or job.status != "dispatching":
                return
        try:
            bundle = self.inspector.inspect(
                config_path=job.config_path,
                run_path=job.run_path,
                train_path=job.train_path,
                preflight_receipt_path=job.preflight_receipt_path,
                job_kind=job.job_kind,
            )
            self._assert_bundle_unchanged(job, bundle)
            overview = self.cluster_overview.overview(refresh=True)
            cluster, selection = select_hidden_cluster(
                overview,
                hardware=job.hardware,
                required_cards=job.requested_cards,
            )
            baseline_job_names = sorted(
                _matching_names(
                    overview,
                    submitter=job.submitter,
                    cluster=cluster,
                    job_name_base=bundle.job_name,
                )
            )
            receipt = self.submit_adapter.submit(
                bundle=bundle,
                submitter=job.submitter,
                cluster=cluster,
            )
        except SchedulingUnavailable as exc:
            self._mark(job_id, status="accepted", code="capacity_unavailable", message=str(exc))
            return
        except ClusterOverviewUnavailable:
            self._mark(
                job_id,
                status="accepted",
                code="capacity_unavailable",
                message="无法取得可信容量快照，系统将稍后重试。",
            )
            return
        except BundleValidationError as exc:
            self._mark(job_id, status="failed", code=exc.code, message=str(exc))
            return
        except LtpSubmissionUncertain as exc:
            self._mark(job_id, status="needs_review", code=exc.code, message=str(exc))
            return
        except LtpSubmissionError as exc:
            self._mark(job_id, status="failed", code=exc.code, message=str(exc))
            return
        except Exception:
            logger.exception("Managed job dispatch failed: %s", job_id)
            self._mark(
                job_id,
                status="needs_review",
                code="dispatch_unknown",
                message="提交结果不确定，系统不会自动重试。",
            )
            return

        now = datetime.now(UTC)
        with self.session_factory() as session:
            job = session.get(models.ManagedJob, job_id)
            if job is None:
                return
            job.status = "verifying"
            job.selection_receipt = selection
            job.dispatch_receipt = {
                "receipt": "submit_accepted",
                "reported_job_name": receipt.reported_job_name,
                "submitted_job_base": receipt.submitted_job_base,
                "bundle_fingerprint": receipt.bundle_fingerprint,
                "baseline_job_names": baseline_job_names,
            }
            job.dispatched_at = now
            job.error_code = None
            job.error_message = None
            work_item = session.get(models.WorkItem, job.work_item_id)
            if work_item is not None:
                work_item.lifecycle = "active"
            session.commit()

    def reconcile(self) -> int:
        with self.session_factory() as session:
            pending = list(
                session.scalars(
                    select(models.ManagedJob).where(
                        models.ManagedJob.status.in_({"verifying", "queued", "running"})
                    )
                )
            )
        if not pending:
            return 0
        try:
            overview = self.cluster_overview.overview(refresh=True)
        except ClusterOverviewUnavailable:
            return 0
        index: dict[tuple[str, str, str], dict[str, Any]] = {}
        for cluster_name, task in _overview_tasks(overview):
            key = (
                str(task.get("user") or ""),
                str(task.get("name") or ""),
                cluster_name,
            )
            index[key] = task
        now = datetime.now(UTC)
        changed = 0
        with self.session_factory() as session:
            for candidate in pending:
                job = session.get(models.ManagedJob, candidate.id)
                external = (
                    session.get(models.ExternalJob, job.external_job_id)
                    if job is not None and job.external_job_id
                    else None
                )
                if job is None:
                    continue
                cluster = str(
                    ((external.metadata_json if external else None) or {}).get("cluster")
                    or (job.selection_receipt or {}).get("selected_cluster")
                    or ""
                )
                task = (
                    index.get((job.submitter, external.remote_job_id, cluster))
                    if external is not None
                    else None
                )
                if external is None and job.status == "verifying":
                    task = self._materialize_external_job(
                        session,
                        job=job,
                        cluster=cluster,
                        index=index,
                        now=now,
                    )
                    if job.status == "needs_review":
                        changed += 1
                        continue
                if task is not None:
                    provider_state = str(task.get("state") or "queued")
                    job.status = "running" if provider_state == "running" else "queued"
                    job.verified_at = job.verified_at or now
                    job.error_code = None
                    job.error_message = None
                    external = session.get(models.ExternalJob, job.external_job_id)
                    if external is not None:
                        external.status = str(task.get("provider_status") or provider_state)
                        external.last_observed_at = now
                    changed += 1
                    continue
                if (
                    job.status == "verifying"
                    and job.dispatched_at is not None
                    and now - job.dispatched_at
                    >= timedelta(seconds=self.settings.managed_job_verification_timeout_seconds)
                ):
                    job.status = "needs_review"
                    job.error_code = "materialization_unverified"
                    job.error_message = "LTP 已接受提交，但任务尚未在实时清单中核验。"
                    changed += 1
            if changed:
                session.commit()
        return changed

    @staticmethod
    def _materialize_external_job(
        session: Session,
        *,
        job: models.ManagedJob,
        cluster: str,
        index: dict[tuple[str, str, str], dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any] | None:
        receipt = job.dispatch_receipt or {}
        base = str(receipt.get("submitted_job_base") or "")
        reported = str(receipt.get("reported_job_name") or "")
        baseline = {
            str(value)
            for value in receipt.get("baseline_job_names") or []
            if isinstance(value, str)
        }
        candidates = [
            (name, task)
            for (user, name, task_cluster), task in index.items()
            if user == job.submitter
            and task_cluster == cluster
            and name not in baseline
            and (name == base or name.startswith(f"{base}-"))
            and (reported == base or name == reported)
        ]
        if len(candidates) > 1:
            job.status = "needs_review"
            job.error_code = "materialization_ambiguous"
            job.error_message = "LTP 提交后出现多个同名候选任务，系统不会猜测实际身份。"
            return None
        if not candidates:
            return None
        remote_job_id, task = candidates[0]
        duplicate = session.scalar(
            select(models.ExternalJob).where(
                models.ExternalJob.provider == "ltp",
                models.ExternalJob.remote_job_id == remote_job_id,
            )
        )
        owner = (
            str((duplicate.metadata_json or {}).get("managed_job_id") or "")
            if duplicate
            else ""
        )
        if duplicate is not None and owner != job.id:
            job.status = "needs_review"
            job.error_code = "remote_identity_conflict"
            job.error_message = "LTP 实际任务身份与其他逻辑任务冲突。"
            return None
        external = duplicate or models.ExternalJob(
            work_item_id=job.work_item_id,
            provider="ltp",
            remote_job_id=remote_job_id,
            status="submitted",
            metadata_json={},
        )
        external.metadata_json = {
            "managed_job_id": job.id,
            "submitter": job.submitter,
            "cluster": cluster,
            "submitted_job_base": base,
            "bundle_fingerprint": receipt.get("bundle_fingerprint"),
        }
        external.last_observed_at = now
        if duplicate is None:
            session.add(external)
            session.flush()
        job.external_job_id = external.id
        return task

    def _inspect_payload(self, payload: schemas.ManagedJobSubmit) -> BundleSpec:
        return self.inspector.inspect(
            config_path=payload.config_path,
            run_path=payload.run_path,
            train_path=payload.train_path,
            preflight_receipt_path=payload.preflight_receipt_path,
            job_kind=payload.job_kind,
        )

    @staticmethod
    def _assert_bundle_unchanged(job: models.ManagedJob, bundle: BundleSpec) -> None:
        if (
            job.config_sha256 != bundle.config_sha256
            or job.run_sha256 != bundle.run_sha256
            or job.train_sha256 != bundle.train_sha256
            or job.requested_nodes != bundle.requested_nodes
            or job.gpus_per_node != bundle.gpus_per_node
        ):
            raise BundleValidationError(
                "bundle 在进入系统队列后发生变化，请使用新的幂等键重新提交。",
                "bundle_changed",
            )

    @staticmethod
    def _request_fingerprint(payload: schemas.ManagedJobSubmit, bundle: BundleSpec) -> str:
        value = {
            "work_item_id": payload.work_item_id.upper(),
            "submitter": payload.submitter,
            "display_name": payload.display_name or bundle.job_name,
            "job_kind": payload.job_kind,
            "hardware": payload.hardware,
            "bundle": bundle.fingerprint,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _mark(self, job_id: str, *, status: str, code: str, message: str) -> None:
        with self.session_factory() as session:
            job = session.get(models.ManagedJob, job_id)
            if job is None:
                return
            job.status = status
            job.error_code = code
            job.error_message = message
            session.commit()

    @staticmethod
    def _work(session: Session, identity: str) -> models.WorkItem:
        work_item = session.get(models.WorkItem, identity)
        if work_item is None:
            work_item = session.scalar(
                select(models.WorkItem).where(models.WorkItem.human_id == identity.upper())
            )
        if work_item is None:
            raise NotFoundError("Work item not found")
        return work_item

    @staticmethod
    def _public(session: Session, job: models.ManagedJob) -> dict[str, Any]:
        work_item = session.get(models.WorkItem, job.work_item_id)
        state = job.status
        if state == "dispatching":
            state = "accepted"
        messages = {
            "accepted": "系统已接收任务，等待统一调度。",
            "verifying": "系统已提交任务，正在核验运行队列。",
            "queued": "任务已进入运行队列。",
            "running": "任务正在运行。",
            "completed": "任务已完成。",
            "failed": job.error_message or "任务提交失败。",
            "needs_review": job.error_message or "任务需要系统管理员核验。",
        }
        return {
            "id": job.id,
            "work_id": work_item.human_id if work_item is not None else job.work_item_id,
            "name": job.display_name,
            "submitter": job.submitter,
            "job_kind": job.job_kind,
            "hardware": job.hardware,
            "state": state,
            "requested_nodes": job.requested_nodes,
            "requested_cards": job.requested_cards,
            "message": messages.get(state, "系统正在处理任务。"),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
