from __future__ import annotations

import math
import threading
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from research_ops.ltp import SnapshotEnvelope, SnapshotSourceError


class ClusterOverviewUnavailable(RuntimeError):
    """A secret-safe cluster overview failure suitable for an HTTP response."""


class ClusterSnapshotSource(Protocol):
    def fetch(self) -> SnapshotEnvelope: ...


class ClusterOverviewService:
    def __init__(
        self,
        source: ClusterSnapshotSource,
        *,
        cache_seconds: float = 30.0,
        stale_seconds: float = 180.0,
    ) -> None:
        self.source = source
        self.cache_seconds = cache_seconds
        self.stale_seconds = stale_seconds
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] | None = None

    def overview(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._cached is not None and now - self._cached_at < self.cache_seconds:
            return self._cache_view(self._cached)
        with self._lock:
            now = time.monotonic()
            if (
                not refresh
                and self._cached is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cache_view(self._cached)
            try:
                envelope = self.source.fetch()
                overview = normalize_cluster_snapshot(
                    envelope,
                    stale_seconds=self.stale_seconds,
                )
            except (SnapshotSourceError, ValueError, TypeError, KeyError) as exc:
                if self._cached is None:
                    message = (
                        str(exc)
                        if isinstance(exc, SnapshotSourceError)
                        else "集群快照校验失败。"
                    )
                    raise ClusterOverviewUnavailable(message) from exc
                overview = self._cache_view(self._cached)
                overview["source"]["cache_hit"] = True
                overview["source"]["degraded"] = True
                overview["source"]["refresh_error"] = "实时刷新失败，当前显示最近成功快照。"
                overview["warnings"] = _unique(
                    [
                        *overview.get("warnings", []),
                        "实时刷新失败，当前显示最近成功快照。",
                    ]
                )
                return overview
            self._cached = overview
            self._cached_at = time.monotonic()
            return deepcopy(overview)

    def _cache_view(self, value: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(value)
        result["source"]["cache_hit"] = True
        current = datetime.now(UTC)
        age_seconds = max(
            0.0,
            (
                current
                - min(result["resources_observed_at"], result["jobs_observed_at"])
            ).total_seconds(),
        )
        result["source"]["age_seconds"] = age_seconds
        if age_seconds > self.stale_seconds:
            result["source"]["stale"] = True
            result["source"]["degraded"] = True
            result["warnings"] = _unique(
                [
                    *result.get("warnings", []),
                    "集群快照已超过新鲜度阈值，请先刷新后再据此决策。",
                ]
            )
        return result


def normalize_cluster_snapshot(
    envelope: SnapshotEnvelope,
    *,
    stale_seconds: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    payload = envelope.payload
    observed_at = _timestamp(payload.get("observed_at"), "observed_at")
    resources_observed_at = _timestamp(
        payload.get("resources_observed_at"), "resources_observed_at"
    )
    jobs_observed_at = _timestamp(payload.get("jobs_observed_at"), "jobs_observed_at")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    age_seconds = max(0.0, (current - min(resources_observed_at, jobs_observed_at)).total_seconds())
    stale = age_seconds > stale_seconds

    coverage_raw = _mapping(payload.get("coverage"), "coverage")
    failed_users_raw = coverage_raw.get("failed_users", {})
    failed_users = len(failed_users_raw) if isinstance(failed_users_raw, (dict, list)) else 0
    failed_user_values = (
        failed_users_raw.keys()
        if isinstance(failed_users_raw, dict)
        else failed_users_raw
        if isinstance(failed_users_raw, list)
        else []
    )
    failed_user_names = sorted(
        str(value)
        for value in failed_user_values
        if isinstance(value, str) and value.strip()
    )
    coverage = {
        "source": _optional_text(coverage_raw.get("source")),
        "complete": bool(coverage_raw.get("complete")) and failed_users == 0,
        "requested_users": _nonnegative_int(coverage_raw.get("requested_users"), default=0),
        "successful_users": _nonnegative_int(coverage_raw.get("successful_users"), default=0),
        "failed_users": failed_users,
        "failed_user_names": failed_user_names,
    }

    resources = [_normalize_resource(item) for item in _list(payload.get("pools"), "pools")]
    resource_by_name = {item["name"]: item for item in resources}
    decisions = _decision_index(_list(payload.get("decisions", []), "decisions"))
    jobs = [
        _normalize_job(item, decisions, observed_at)
        for item in _list(payload.get("jobs"), "jobs")
    ]
    jobs.sort(key=lambda item: (_status_order(item["state"]), item["user"], item["name"]))

    jobs_by_cluster: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_cluster.setdefault(job["cluster"], []).append(job)
    cluster_names = set(resource_by_name) | set(jobs_by_cluster)
    queue_rows = {
        str(item.get("cluster")): item
        for item in _list(payload.get("queues", []), "queues")
        if isinstance(item, dict) and item.get("cluster")
    }
    clusters = []
    for name in sorted(cluster_names, key=lambda item: _cluster_sort_key(item, resource_by_name)):
        selected = jobs_by_cluster.get(name, [])
        running = [item for item in selected if item["state"] == "running"]
        queued = [item for item in selected if item["state"] == "queued"]
        queue = queue_rows.get(name, {})
        clusters.append(
            {
                "name": name,
                "resource": resource_by_name.get(name),
                "running_jobs": running,
                "queued_jobs": queued,
                "running_nodes": sum(item["nodes"] or 0 for item in running),
                "queued_nodes": sum(item["nodes"] or 0 for item in queued),
                "running_nodes_known_jobs": sum(item["nodes"] is not None for item in running),
                "queued_nodes_known_jobs": sum(item["nodes"] is not None for item in queued),
                "profiled_jobs": _nonnegative_int(
                    queue.get("profiled_jobs"),
                    default=sum(item["profiled"] for item in selected),
                ),
            }
        )

    domain_resources: dict[str, dict[str, Any]] = {}
    for resource in resources:
        domain = resource["resource_domain"] or resource["resource_pool"] or resource["name"]
        existing = domain_resources.get(domain)
        if existing is None or (
            resource["free_nodes"], resource["available_cards"]
        ) > (
            existing["free_nodes"], existing["available_cards"]
        ):
            domain_resources[domain] = resource
    estimated_jobs = sum(job["eta_seconds"] is not None for job in jobs)
    checkpoint_jobs = sum(job["checkpoint_interval_seconds"] is not None for job in jobs)
    warnings = [
        text
        for value in _list(payload.get("warnings", []), "warnings")
        if (text := _optional_text(value)) is not None
    ]
    if envelope.refresh_error:
        warnings.append(envelope.refresh_error)
    if stale:
        warnings.append("集群快照已超过新鲜度阈值，请先刷新后再据此决策。")

    return {
        "schema_version": 1,
        "mode": _optional_text(payload.get("mode")) or "shadow",
        "observed_at": observed_at,
        "resources_observed_at": resources_observed_at,
        "jobs_observed_at": jobs_observed_at,
        "source": {
            "kind": envelope.source,
            "fetched_at": envelope.fetched_at.astimezone(UTC),
            "cache_hit": False,
            "degraded": envelope.refresh_error is not None or stale or not coverage["complete"],
            "stale": stale,
            "age_seconds": age_seconds,
            "refresh_error": envelope.refresh_error,
        },
        "coverage": coverage,
        "summary": {
            "clusters": len(clusters),
            "resource_pools": len(resources),
            "resource_domains": len(domain_resources),
            "available_cards": sum(item["available_cards"] for item in domain_resources.values()),
            "free_nodes": sum(item["free_nodes"] for item in domain_resources.values()),
            "running_jobs": sum(job["state"] == "running" for job in jobs),
            "queued_jobs": sum(job["state"] == "queued" for job in jobs),
            "running_nodes": sum(job["nodes"] or 0 for job in jobs if job["state"] == "running"),
            "queued_nodes": sum(job["nodes"] or 0 for job in jobs if job["state"] == "queued"),
            "profiled_jobs": sum(job["profiled"] for job in jobs),
            "estimated_jobs": estimated_jobs,
            "checkpoint_interval_jobs": checkpoint_jobs,
        },
        "categories": _normalize_categories(payload.get("categories", {})),
        "clusters": clusters,
        "warnings": _unique(warnings),
    }


def _normalize_resource(raw: Any) -> dict[str, Any]:
    item = _mapping(raw, "pool")
    forecast_raw = item.get("forecast")
    forecast = _mapping(forecast_raw, "pool.forecast") if forecast_raw is not None else {}
    available_cards = _nonnegative_number(item.get("available_cards"), default=0.0)
    gpus_per_node = _positive_int(item.get("gpus_per_node"), default=8)
    free_nodes = _nonnegative_int(
        item.get("free_nodes"), default=int(available_cards // gpus_per_node)
    )
    return {
        "name": _required_text(item.get("name"), "pool.name"),
        "resource_pool": _optional_text(item.get("resource_pool")),
        "resource_domain": _optional_text(item.get("resource_domain")),
        "hardware": _optional_text(item.get("hardware")) or "unknown",
        "scheduling_class": _optional_text(item.get("scheduling_class")) or "unknown",
        "quota_cards": _nonnegative_number(item.get("quota_cards"), default=0.0),
        "used_cards": _nonnegative_number(item.get("used_cards"), default=0.0),
        "available_cards": available_cards,
        "gpus_per_node": gpus_per_node,
        "free_nodes": free_nodes,
        "fragment_cards": _nonnegative_number(item.get("fragment_cards"), default=0.0),
        "utilization_pct": _bounded_number(item.get("utilization_pct"), 0.0, 100.0, 0.0),
        "stable_window_seconds": _nonnegative_number(
            forecast.get("stable_window_seconds"), default=0.0
        ),
        "volatility": _bounded_number(forecast.get("volatility"), 0.0, 1.0, 0.0),
        "forecast_source": _optional_text(forecast.get("source")),
        "forecast_samples": _nonnegative_int(forecast.get("sample_count"), default=0),
        "available_formula": _optional_text(item.get("available_formula")),
    }


def _normalize_job(
    raw: Any,
    decisions: dict[str, dict[str, Any]],
    observed_at: datetime,
) -> dict[str, Any]:
    item = _mapping(raw, "job")
    user = _required_text(item.get("user"), "job.user")
    name = _required_text(item.get("job_name"), "job.job_name")
    identity = f"{user}/{name}"
    decision = decisions.get(identity) or decisions.get(name) or {}
    state_group = (_optional_text(item.get("state_group")) or "").casefold()
    state = "running" if state_group == "active" else "queued"
    category = _optional_text(item.get("category")) or "unknown"
    eta_remaining = _optional_nonnegative_number(
        item.get("estimated_remaining_seconds", decision.get("estimated_remaining_seconds"))
    )
    eta_ready = _optional_nonnegative_number(
        item.get("estimated_ready_seconds", decision.get("estimated_ready_seconds"))
    )
    if eta_remaining is not None:
        eta_kind = "remaining"
        eta_seconds = eta_remaining
    elif eta_ready is not None:
        eta_kind = "ready"
        eta_seconds = eta_ready
    else:
        eta_kind = None
        eta_seconds = None
    checkpoint_interval = _optional_nonnegative_number(
        item.get("checkpoint_interval_seconds", decision.get("checkpoint_interval_seconds"))
    )
    last_checkpoint_at = _optional_timestamp(item.get("last_checkpoint_at"))
    next_checkpoint_at = _optional_timestamp(item.get("next_checkpoint_at"))
    if next_checkpoint_at is None and last_checkpoint_at is not None and checkpoint_interval:
        next_checkpoint_at = datetime.fromtimestamp(
            last_checkpoint_at.timestamp() + checkpoint_interval,
            tz=UTC,
        )
    next_checkpoint_seconds = (
        max(0.0, (next_checkpoint_at - observed_at).total_seconds())
        if next_checkpoint_at is not None
        else None
    )
    confidence = _optional_bounded_number(
        item.get("estimate_confidence", decision.get("estimate_confidence")), 0.0, 1.0
    )
    profile_rule = _optional_text(item.get("profile_rule"))
    return {
        "id": identity,
        "name": name,
        "user": user,
        "cluster": _required_text(item.get("cluster"), "job.cluster"),
        "state": state,
        "provider_status": _optional_text(item.get("status")) or state,
        "category": category,
        "nodes": _optional_positive_int(item.get("total_nodes")),
        "worker_nodes": _optional_nonnegative_int(item.get("worker_num")),
        "age": _optional_text(item.get("age")),
        "submission_time": _optional_text(item.get("submission_time")),
        "last_status_update": _optional_text(item.get("last_status_update")),
        "profiled": bool(item.get("schedulable")),
        "profile_rule": profile_rule,
        "classification_source": _optional_text(item.get("classification_source")),
        "blocked_reason": _optional_text(item.get("blocked_reason")),
        "eta_kind": eta_kind,
        "eta_seconds": eta_seconds,
        "estimate_confidence": confidence,
        "estimate_source": (
            _optional_text(item.get("estimate_source"))
            or ("scheduler_decision" if decision else None)
            or profile_rule
        ),
        "checkpoint_interval_seconds": checkpoint_interval,
        "checkpoint_source": (
            _optional_text(item.get("checkpoint_source"))
            or ("scheduler_decision" if decision and checkpoint_interval is not None else None)
        ),
        "last_checkpoint_at": last_checkpoint_at,
        "next_checkpoint_at": next_checkpoint_at,
        "next_checkpoint_seconds": next_checkpoint_seconds,
    }


def _decision_index(raw: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in raw:
        if not isinstance(value, dict):
            continue
        job_id = _optional_text(value.get("job_id"))
        if job_id:
            result[job_id] = value
    return result


def _normalize_categories(raw: Any) -> dict[str, dict[str, int]]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for name, counts in raw.items():
        if not isinstance(name, str) or not isinstance(counts, dict):
            continue
        result[name] = {
            "running": _nonnegative_int(counts.get("running"), default=0),
            "queued": _nonnegative_int(counts.get("queued"), default=0),
        }
    return result


def _cluster_sort_key(name: str, resources: dict[str, dict[str, Any]]) -> tuple[int, str]:
    priority = {
        "cluster-a": 0,
        "cluster-b": 1,
        "cluster-c": 2,
        "cluster-reserved-a": 3,
        "cluster-reserved-b": 4,
        "cluster-reserved-c": 5,
        "cluster-d": 6,
    }
    if name in priority:
        return priority[name], name
    return (7 if name in resources else 8), name


def _status_order(value: str) -> int:
    return 0 if value == "running" else 1


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _required_text(value: Any, field: str) -> str:
    result = _optional_text(value)
    if result is None:
        raise ValueError(f"{field} must be text")
    return result


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    result = float(value)
    return result if math.isfinite(result) else default


def _nonnegative_number(value: Any, *, default: float) -> float:
    return max(0.0, _number(value, default))


def _optional_nonnegative_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _bounded_number(value: Any, minimum: float, maximum: float, default: float) -> float:
    return min(maximum, max(minimum, _number(value, default)))


def _optional_bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    result = _optional_nonnegative_number(value)
    if result is None or not minimum <= result <= maximum:
        return None
    return result


def _nonnegative_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return default


def _positive_int(value: Any, *, default: int) -> int:
    result = _nonnegative_int(value, default=default)
    return result if result > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    result = _nonnegative_int(value, default=-1)
    return result if result > 0 else None


def _optional_nonnegative_int(value: Any) -> int | None:
    result = _nonnegative_int(value, default=-1)
    return result if result >= 0 else None


def _timestamp(value: Any, field: str) -> datetime:
    result = _optional_timestamp(value)
    if result is None:
        raise ValueError(f"{field} must be an ISO timestamp")
    return result


def _optional_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
