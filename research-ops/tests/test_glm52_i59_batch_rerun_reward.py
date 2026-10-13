from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from research_ops.agentos import AgentOSBatchSnapshot
from research_ops.reward import GoblinJobSnapshot

from scripts.run_glm52_i59_batch_rerun_reward import (
    AmbiguousMutationError,
    BatchRerunRewardWorkflow,
    WorkflowConfigurationError,
    batch_phase,
    inspect_driver_log,
    load_manifest,
    safe_plan,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config/glm52-i59-batch-rerun-reward.yaml"


def test_manifest_is_attach_only_and_plan_is_redacted() -> None:
    manifest, digest = load_manifest(DEFAULT_MANIFEST)
    plan = safe_plan(manifest, digest)

    assert manifest["teardown"]["enabled"] is False
    assert manifest["rollout"]["restart_all_records"] is True
    assert manifest["serving"]["per_endpoint_concurrency"] == 20
    assert plan["execute_required_for_mutations"] is True
    assert plan["readiness"]["formula"].startswith("min(")
    assert "DATAWHEEL_TOKEN" not in json.dumps(plan)
    assert "GOBLIN_REWARD_API_TOKEN" not in json.dumps(plan)


def test_manifest_rejects_inline_secret_and_teardown(tmp_path: Path) -> None:
    text = DEFAULT_MANIFEST.read_text(encoding="utf-8")
    inline = tmp_path / "inline.yaml"
    inline.write_text(
        text.replace('api_key_env: "AGENTOS_MODEL_API_KEY"', 'api_key: "secret"'),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowConfigurationError, match="environment reference"):
        load_manifest(inline)

    teardown = tmp_path / "teardown.yaml"
    teardown.write_text(text.replace("enabled: false\n", "enabled: true\n", 1), encoding="utf-8")
    with pytest.raises(WorkflowConfigurationError, match="never tears down"):
        load_manifest(teardown)


def test_driver_ready_evidence_must_be_newer_than_submission(tmp_path: Path) -> None:
    log = tmp_path / "serve.driver.log"
    log.write_text(
        "\n".join(
            [
                "stage=serving replica=dev1 job=job-1 model=model-1 endpoint=http://192.0.2.1:14234",
                json.dumps(
                    {
                        "actual_job": "job-1",
                        "endpoint": "http://192.0.2.1:14234",
                        "model_name": "model-1",
                        "gateway_endpoint_count": 1,
                        "gateway_generation_ok": True,
                        "local_generation_ok": True,
                        "observed_at_utc": "2026-09-04T06:32:06Z",
                    }
                ),
                "stage=serving status=ready endpoint=http://192.0.2.1:14234",
            ]
        ),
        encoding="utf-8",
    )
    stale = inspect_driver_log(
        log,
        job_name="job-1",
        model_name="model-1",
        submitted_at=datetime(2026, 9, 4, 17, 33, tzinfo=UTC),
    )
    assert stale.ready_at is None
    assert stale.reason == "ready_evidence_predates_submission"

    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "2026-09-04T06:32:06Z", "2026-09-04T17:40:06Z"
        ),
        encoding="utf-8",
    )
    fresh = inspect_driver_log(
        log,
        job_name="job-1",
        model_name="model-1",
        submitted_at=datetime(2026, 9, 4, 17, 33, tzinfo=UTC),
    )
    assert fresh.endpoint == "http://192.0.2.1:14234"
    assert fresh.ready_at == datetime(2026, 9, 4, 17, 40, 6, tzinfo=UTC)


def test_batch_phase_requires_all_records_to_settle() -> None:
    assert batch_phase(
        AgentOSBatchSnapshot("b", "completed", {"completed": 2, "pending": 1}, 3)
    ) == "mixed_terminal"
    assert batch_phase(
        AgentOSBatchSnapshot("b", "completed", {"completed": 2, "failed": 1}, 3),
        expected_total=3,
    ) == "completed"
    assert batch_phase(
        AgentOSBatchSnapshot("b", "terminated", {"completed": 2, "pending": 1}, 3)
    ) == "mixed_terminal"


class _FakeMonitor:
    def __init__(self) -> None:
        self.poll_count = 0

    def poll(self) -> dict[str, object]:
        self.poll_count += 1
        count = 1 if self.poll_count == 1 else 2
        return {
            "observed_at_utc": "2026-09-04T18:00:00Z",
            "model_name": "demo-reward-model",
            "ready_count": count,
            "ready_endpoints": [f"http://192.0.2.{index}:14234" for index in range(1, count + 1)],
            "ready_job_count": count,
            "jobs": [],
        }


class _FakeDatawheel:
    def __init__(self) -> None:
        self.snapshots = iter(
            [
                AgentOSBatchSnapshot("b", "running", {"running": 3}, 3),
                AgentOSBatchSnapshot("b", "running", {"running": 3}, 3),
                AgentOSBatchSnapshot("b", "running", {"running": 3}, 3),
                AgentOSBatchSnapshot("b", "completed", {"completed": 3}, 3),
            ]
        )
        self.concurrency_updates: list[int] = []
        self.restart_groups: list[list[str]] = []
        self.download_count = 0

    def get_batch(self, _batch_id: str) -> AgentOSBatchSnapshot:
        try:
            return next(self.snapshots)
        except StopIteration:
            return AgentOSBatchSnapshot("b", "completed", {"completed": 3}, 3)

    def list_batch_records(self, _batch_id: str, *, page_size: int) -> list[dict[str, str]]:
        assert page_size == 200
        return [{"id": f"r{index}", "agent_generate_batch_task_id": "b"} for index in range(1, 4)]

    def update_concurrency(self, _batch_id: str, value: int) -> dict[str, bool]:
        self.concurrency_updates.append(value)
        return {"ok": True}

    def restart_records(self, record_ids: list[str], *, skip_check: bool) -> dict[str, bool]:
        assert skip_check is False
        self.restart_groups.append(record_ids)
        return {"ok": True}

    def download_result(self, _batch_id: str, destination: Path) -> Path:
        self.download_count += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"xlsx")
        return destination


class _FakeGoblin:
    def __init__(self) -> None:
        self.submit_count = 0

    def submit_file(self, path: Path, **kwargs: object) -> GoblinJobSnapshot:
        assert path.read_bytes() == b"xlsx"
        assert kwargs["rollout_model"] == "demo-reward-model"
        self.submit_count += 1
        return GoblinJobSnapshot("goblin-1", "pending")

    def get_job(self, _job_id: str) -> GoblinJobSnapshot:
        return GoblinJobSnapshot("goblin-1", "completed")


def test_workflow_accumulates_capacity_restarts_every_record_and_rewards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, digest = load_manifest(DEFAULT_MANIFEST)
    runtime = dict(manifest["runtime"])
    runtime.update(
        {
            "state_file": str(tmp_path / "state.json"),
            "result_file": str(tmp_path / "result.xlsx"),
            "poll_interval_seconds": 0,
            "service_timeout_seconds": 30,
            "batch_timeout_seconds": 30,
            "reward_timeout_seconds": 30,
        }
    )
    manifest["runtime"] = runtime
    marker = tmp_path / "FP8_QUANT_SUCCESS"
    marker.write_text("validated\n", encoding="utf-8")
    manifest["model"] = dict(manifest["model"], artifact_success_marker=str(marker))
    monkeypatch.setenv("DATAWHEEL_TOKEN", "test-token")
    monkeypatch.setenv("GOBLIN_REWARD_API_TOKEN", "test-token")
    workflow = BatchRerunRewardWorkflow(
        manifest,
        digest,
        datawheel=_FakeDatawheel(),  # type: ignore[arg-type]
        goblin=_FakeGoblin(),  # type: ignore[arg-type]
        monitor=_FakeMonitor(),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )

    assert workflow.run() == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "complete"
    assert state["record_count"] == 3
    assert state["restart_next_index"] == 3
    assert state["applied_concurrency"] == 40
    assert state["goblin_job_id"] == "goblin-1"


def test_unfinished_mutation_blocks_resume(tmp_path: Path) -> None:
    manifest, digest = load_manifest(DEFAULT_MANIFEST)
    runtime = dict(manifest["runtime"])
    runtime["state_file"] = str(tmp_path / "state.json")
    runtime["result_file"] = str(tmp_path / "result.xlsx")
    manifest["runtime"] = runtime
    state = {
        "schema_version": manifest["schema_version"],
        "pipeline_name": manifest["pipeline_name"],
        "manifest_sha256": digest,
        "batch_task_id": manifest["rollout"]["batch_task_id"],
        "model_name": manifest["model"]["name"],
        "phase": "updating_concurrency",
        "pending_mutation": {"kind": "update_concurrency"},
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    workflow = BatchRerunRewardWorkflow(
        manifest,
        digest,
        datawheel=object(),  # type: ignore[arg-type]
        goblin=None,
        monitor=object(),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )
    with pytest.raises(AmbiguousMutationError):
        workflow.run()
