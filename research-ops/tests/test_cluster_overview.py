from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from research_ops.api import create_app
from research_ops.ltp import LtpControlLiveAdapter, SnapshotEnvelope, SnapshotSourceError
from research_ops.services.cluster_overview import (
    ClusterOverviewService,
    normalize_cluster_snapshot,
)
from research_ops.settings import Settings


def live_payload() -> dict:
    return {
        "schema_version": 1,
        "mode": "shadow",
        "observed_at": "2026-08-20T10:00:00Z",
        "resources_observed_at": "2026-08-20T10:00:00Z",
        "jobs_observed_at": "2026-08-20T10:00:02Z",
        "coverage": {
            "source": "ltp_user_directory",
            "complete": True,
            "requested_users": 129,
            "successful_users": 129,
            "failed_users": {},
        },
        "pools": [
            {
                "name": "cluster-a",
                "resource_pool": "physical-pool-a",
                "resource_domain": "physical-pool-a",
                "hardware": "B",
                "scheduling_class": "regular",
                "quota_cards": 64,
                "used_cards": 32,
                "available_cards": 32,
                "gpus_per_node": 8,
                "free_nodes": 4,
                "fragment_cards": 0,
                "utilization_pct": 50,
                "forecast": {
                    "stable_window_seconds": 3600,
                    "volatility": 0.2,
                    "source": "history",
                    "sample_count": 24,
                },
            },
            {
                "name": "cluster-reserved-b",
                "resource_pool": "physical-pool-a",
                "resource_domain": "physical-pool-a",
                "hardware": "B",
                "scheduling_class": "book",
                "quota_cards": 256,
                "used_cards": 40,
                "available_cards": 24,
                "gpus_per_node": 8,
                "free_nodes": 3,
                "fragment_cards": 0,
                "utilization_pct": 15.625,
                "forecast": {
                    "stable_window_seconds": 1200,
                    "volatility": 0.8,
                    "source": "history",
                    "sample_count": 24,
                },
            },
        ],
        "queues": [
            {
                "cluster": "cluster-a",
                "profiled_jobs": 1,
            }
        ],
        "categories": {
            "large_training": {"running": 1, "queued": 0},
            "unknown": {"running": 0, "queued": 1},
        },
        "jobs": [
            {
                "job_name": "glm-train-a",
                "user": "user-a",
                "cluster": "cluster-a",
                "state_group": "active",
                "status": "running",
                "category": "large_training",
                "total_nodes": 4,
                "worker_num": 3,
                "age": "2h",
                "schedulable": True,
                "profile_rule": "glm-training",
                "classification_source": "profile",
                "last_checkpoint_at": "2026-08-20T09:58:00Z",
            },
            {
                "job_name": "opaque-job",
                "user": "user-b",
                "cluster": "cpu_cluster",
                "state_group": "queued",
                "status": "created",
                "category": "unknown",
                "total_nodes": 1,
                "worker_num": 0,
                "age": "8m",
                "schedulable": False,
                "blocked_reason": "未命中显式任务画像",
            },
        ],
        "decisions": [
            {
                "job_id": "user-a/glm-train-a",
                "estimated_remaining_seconds": 7200,
                "estimated_ready_seconds": None,
                "checkpoint_interval_seconds": 300,
                "estimate_confidence": 0.78,
            }
        ],
        "warnings": [],
    }


def envelope(payload: dict | None = None) -> SnapshotEnvelope:
    return SnapshotEnvelope(
        payload=payload or live_payload(),
        source="live",
        fetched_at=datetime(2026, 8, 20, 10, 0, 3, tzinfo=UTC),
    )


def test_normalizes_live_jobs_without_inventing_missing_estimates() -> None:
    overview = normalize_cluster_snapshot(
        envelope(),
        stale_seconds=180,
        now=datetime(2026, 8, 20, 10, 0, 30, tzinfo=UTC),
    )

    assert overview["summary"]["available_cards"] == 32
    assert overview["summary"]["free_nodes"] == 4
    assert overview["summary"]["running_jobs"] == 1
    assert overview["summary"]["queued_jobs"] == 1
    assert overview["coverage"]["complete"] is True
    assert overview["coverage"]["failed_user_names"] == []

    cluster_a = next(item for item in overview["clusters"] if item["name"] == "cluster-a")
    job = cluster_a["running_jobs"][0]
    assert job["eta_kind"] == "remaining"
    assert job["eta_seconds"] == 7200
    assert job["checkpoint_interval_seconds"] == 300
    assert job["next_checkpoint_seconds"] == 180

    cpu = next(item for item in overview["clusters"] if item["name"] == "cpu_cluster")
    unknown = cpu["queued_jobs"][0]
    assert unknown["eta_seconds"] is None
    assert unknown["checkpoint_interval_seconds"] is None
    assert unknown["next_checkpoint_at"] is None


def test_service_returns_last_successful_snapshot_when_refresh_fails() -> None:
    class Source:
        calls = 0

        def fetch(self) -> SnapshotEnvelope:
            self.calls += 1
            if self.calls > 1:
                raise SnapshotSourceError("secret-safe source error")
            now = datetime.now(UTC)
            payload = live_payload()
            for key in ("observed_at", "resources_observed_at", "jobs_observed_at"):
                payload[key] = now.isoformat()
            return SnapshotEnvelope(payload=payload, source="live", fetched_at=now)

    source = Source()
    service = ClusterOverviewService(source, cache_seconds=30, stale_seconds=180)
    first = service.overview()
    fallback = service.overview(refresh=True)

    assert first["source"]["degraded"] is False
    assert fallback["source"]["degraded"] is True
    assert fallback["source"]["cache_hit"] is True
    assert fallback["source"]["refresh_error"] == "实时刷新失败，当前显示最近成功快照。"


def test_snapshot_file_adapter_and_http_route(
    tmp_path: Path,
    settings: Settings,
    fake_codex,
) -> None:
    snapshot = tmp_path / "latest.json"
    snapshot.write_text(json.dumps(live_payload()), encoding="utf-8")
    adapter = LtpControlLiveAdapter(
        enabled=False,
        command="ltp-control",
        python_command=None,
        source_root=None,
        snapshot_path=snapshot,
        directory_user="bob",
        allow_partial_users=True,
        state_dir=tmp_path / "state",
        profiles_file=None,
        query_timeout_seconds=15,
        process_timeout_seconds=90,
        max_response_bytes=1024 * 1024,
    )
    assert adapter.fetch().source == "snapshot_file"

    settings.cluster_dashboard_enabled = False
    settings.ltp_control_snapshot_path = snapshot
    app = create_app(settings, fake_codex)
    with TestClient(app) as client:
        response = client.get("/api/clusters/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["coverage"]["requested_users"] == 129
    assert payload["clusters"][0]["name"] == "cluster-a"


def test_rejects_snapshot_without_complete_contract() -> None:
    broken = live_payload()
    broken.pop("jobs_observed_at")
    with pytest.raises(ValueError, match="jobs_observed_at"):
        normalize_cluster_snapshot(
            envelope(broken),
            stale_seconds=180,
            now=datetime(2026, 8, 20, 10, 0, 30, tzinfo=UTC),
        )
