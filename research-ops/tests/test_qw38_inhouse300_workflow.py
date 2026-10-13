from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from research_ops.agentos import AgentOSBatchSnapshot
from research_ops.reward import GoblinJobSnapshot

from scripts.run_qw38_inhouse300_workflow import (
    DEFAULT_MANIFEST,
    RolloutRewardWorkflow,
    ServiceNotReady,
    ServingFleet,
    ServingRuntime,
    WorkflowConfigurationError,
    attach_job_to_ledger,
    ensure_primary_job_ledger,
    load_manifest,
    rollout_phase,
)

SFT53_MANIFEST = (
    Path(__file__).resolve().parents[1] / "config/qw38-sft53-inhouse300-rollout-reward.yaml"
)
DEPLOY_ROOT = Path(__file__).resolve().parents[1] / "deploy/ltp/qw38-inhouse300-formal"
SUPERVISOR_CONTRACT = (
    Path(__file__).resolve().parents[1] / "config/qw38-sft53-supervisor-contract.json"
)
MM200_ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts/ADAPT-0003/qwen38-online553-i23-mm200-20260828/rollout"
)
MM200_BCARD_ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts/ADAPT-0003/qwen38-online553-i23-mm200-dflash2-r3-20260828/rollout"
)
MM200_V6_ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts/ADAPT-0003/qw38-opxml-u0731-i104-mm200-v6-z2-20260831/rollout"
)
SECOND_ROUND_ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts/ADAPT-0003/qw38-sft53-second-round-z1-20260831/rollout"
)
SECOND_ROUND_OPENAI_ROOT = (
    Path(__file__).resolve().parents[1]
    / "artifacts/ADAPT-0003/qw38-sft53-second-round-z1-openai-r2-20260831/rollout"
)


class FakeServing:
    def __init__(self) -> None:
        self.probes = 0
        self.teardowns = 0

    def probe(self) -> dict[str, Any]:
        self.probes += 1
        return {
            "model_name": "demo-base-model",
            "requested_replicas": 1,
            "ready_replicas": 1,
            "endpoint_count": 1,
            "endpoints": ["http://192.0.2.8:14234"],
            "jobs": [
                {
                    "job_name": "demo-base-rollout-fixture",
                    "endpoint": "http://192.0.2.8:14234",
                    "gateway_generation_http_status": 200,
                }
            ],
            "job_errors": {},
        }

    def teardown(
        self,
        *,
        serving_snapshot: dict[str, Any] | None,
        timeout_seconds: int,
        poll_interval: float,
    ) -> dict[str, Any]:
        assert serving_snapshot is not None
        assert serving_snapshot["endpoints"] == ["http://192.0.2.8:14234"]
        assert timeout_seconds > 0
        assert poll_interval > 0
        self.teardowns += 1
        return {
            "schema_version": "miaoda.serving_teardown.v2",
            "model_name": "demo-base-model",
            "removed_endpoints": ["http://192.0.2.8:14234"],
            "remaining_endpoints": [],
            "jobs": [
                {
                    "ltp_job_name": "demo-base-rollout-fixture",
                    "ltp_delete": "accepted",
                }
            ],
        }


class FakeDatawheel:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.downloads = 0
        self.concurrency_updates: list[tuple[str, int]] = []

    def list_batches(self, *, page: int, page_size: int) -> list[dict[str, Any]]:
        assert page == 1
        assert page_size == 100
        return []

    def get_dataset_records(self, dataset_name: str) -> list[dict[str, Any]]:
        assert dataset_name == "example-app-300"
        return [{"id": index, "state_input": {"prd": f"case-{index}"}} for index in range(300)]

    def submit_batch(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> AgentOSBatchSnapshot:
        assert idempotency_key
        self.submissions.append(payload)
        return AgentOSBatchSnapshot("batch-fixture", "running")

    def get_batch(self, batch_task_id: str) -> AgentOSBatchSnapshot:
        assert batch_task_id == "batch-fixture"
        return AgentOSBatchSnapshot(
            batch_task_id,
            "completed",
            status_counts={"completed": 300},
            total_size=300,
        )

    def download_result(self, batch_task_id: str, destination: Path) -> Path:
        assert batch_task_id == "batch-fixture"
        self.downloads += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"xlsx fixture")
        return destination

    def update_concurrency(self, batch_task_id: str, concurrency: int) -> None:
        self.concurrency_updates.append((batch_task_id, concurrency))


class FakeGoblin:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []

    def submit_file(self, file_path: Path, **options: Any) -> GoblinJobSnapshot:
        assert file_path.read_bytes() == b"xlsx fixture"
        self.submissions.append(options)
        return GoblinJobSnapshot("reward-fixture", "running")

    def get_job(self, job_id: str) -> GoblinJobSnapshot:
        assert job_id == "reward-fixture"
        return GoblinJobSnapshot(job_id, "completed", total=300, completed=300)


def test_formal_manifest_is_secret_safe_and_exact() -> None:
    manifest, manifest_hash = load_manifest(DEFAULT_MANIFEST)

    assert len(manifest_hash) == 64
    assert manifest["rollout"]["dataset_name"] == "example-app-300"
    assert manifest["rollout"]["concurrency"] == 8
    model = manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]
    assert model["api_key_env"] == "AGENTOS_MODEL_API_KEY"
    assert "api_key" not in model
    assert model["max_tokens"] == 32000
    assert model["thinking_config"]["chat_template_kwargs"] == {
        "enable_thinking": True,
        "preserve_thinking": True,
    }
    assert manifest["reward"]["prompt_version"] == "v1_6"
    assert manifest["reward"]["reward_model"] == "gpt-5.5-agent"


def test_dflash2_performance_profile_allows_pinned_bcard_without_reward(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    document["workflow_profile"] = "qwen38_dflash2_performance"
    document["runtime"]["output_root"] = str(tmp_path / "workflow")
    document["serving"] |= {
        "candidate_clusters": ["cluster-dev"],
        "job_base": "q38i53-b-df2-mr48-r5",
        "model_name": "demo-speculative-serving",
        "readiness_file": str(tmp_path / "READINESS.json"),
        "readiness_dir": str(tmp_path / "readiness"),
    }
    document["rollout"]["coding_config"]["codingPromptiModelConfig"] |= {
        "model": "demo-speculative-serving",
        "model_value": "demo-speculative-serving",
    }
    document["reward"] = {"enabled": False}
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    manifest, manifest_hash = load_manifest(manifest_path)

    assert len(manifest_hash) == 64
    assert manifest["serving"]["candidate_clusters"] == ["cluster-dev"]
    assert manifest["serving"]["expected_runtime"]["max_running_requests"] == 48
    assert manifest["reward"] == {"enabled": False}


def test_dflash2_performance_profile_rejects_reward(tmp_path: Path) -> None:
    document = yaml.safe_load(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    document["workflow_profile"] = "qwen38_dflash2_performance"
    document["runtime"]["output_root"] = str(tmp_path / "workflow")
    document["serving"] |= {
        "candidate_clusters": ["cluster-dev"],
        "job_base": "q38i53-b-df2-mr48-r5",
        "model_name": "demo-speculative-serving",
        "readiness_file": str(tmp_path / "READINESS.json"),
        "readiness_dir": str(tmp_path / "readiness"),
    }
    document["rollout"]["coding_config"]["codingPromptiModelConfig"] |= {
        "model": "demo-speculative-serving",
        "model_value": "demo-speculative-serving",
    }
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(WorkflowConfigurationError, match="does not authorize Reward"):
        load_manifest(manifest_path)


def test_sft53_manifest_reuses_formal_profile_with_unique_identities() -> None:
    baseline, _ = load_manifest(DEFAULT_MANIFEST)
    manifest, manifest_hash = load_manifest(SFT53_MANIFEST)

    assert len(manifest_hash) == 64
    assert manifest["pipeline_name"] != baseline["pipeline_name"]
    assert manifest["runtime"]["output_root"] != baseline["runtime"]["output_root"]
    assert manifest["serving"]["job_base"] == "demo-tuned-rollout"
    assert manifest["serving"]["model_name"] == "demo-tuned-model"
    assert manifest["serving"]["readiness_file"] != baseline["serving"]["readiness_file"]
    assert manifest["serving"]["readiness_dir"].endswith("/readiness")
    assert manifest["rollout"]["configured_max_concurrency"] == 48
    assert manifest["rollout"]["task_name"] != baseline["rollout"]["task_name"]
    assert manifest["rollout"]["coding_config"] | {} == baseline["rollout"]["coding_config"] | {
        "codingPromptiModelConfig": {
            **baseline["rollout"]["coding_config"]["codingPromptiModelConfig"],
            "model": "demo-tuned-model",
            "model_value": "demo-tuned-model",
        }
    }
    assert manifest["reward"] == baseline["reward"] | {"rollout_model": "demo-tuned-model"}


@pytest.mark.skipif(not (MM200_ROOT / "manifest.yaml").is_file(), reason="Private experiment fixture not distributed")
def test_online553_mm200_manifest_uses_h100_openai_profile_without_reward() -> None:
    manifest, manifest_hash = load_manifest(MM200_ROOT / "manifest.yaml")

    assert len(manifest_hash) == 64
    assert manifest["serving"]["candidate_clusters"] == ["cluster-d"]
    assert manifest["serving"]["expected_runtime"] == {
        "tp_size": 8,
        "dp_size": 1,
        "context_length": 131072,
        "cuda_graph_max_bs": 16,
        "max_running_requests": 16,
    }
    assert manifest["rollout"]["dataset_name"] == "example-multimodal-200"
    assert manifest["rollout"]["expected_records"] == 200
    assert manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]["provider"] == "openai"
    assert manifest["reward"]["enabled"] is False


@pytest.mark.skipif(not (MM200_BCARD_ROOT / "manifest.yaml").is_file(), reason="Private experiment fixture not distributed")
def test_online553_mm200_manifest_accepts_complete_bcard_profile() -> None:
    manifest, manifest_hash = load_manifest(MM200_BCARD_ROOT / "manifest.yaml")

    assert len(manifest_hash) == 64
    assert set(manifest["serving"]["candidate_clusters"]) == {
        "cluster-a",
        "cluster-b",
        "cluster-c",
        "cluster-reserved-a",
        "cluster-reserved-b",
        "cluster-reserved-c",
    }
    assert manifest["rollout"]["dataset_name"] == "example-multimodal-200"
    assert manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]["provider"] == "openai"
    assert manifest["reward"]["enabled"] is False


@pytest.mark.skipif(not (MM200_V6_ROOT / "manifest.yaml").is_file(), reason="Private experiment fixture not distributed")
def test_opusxml_mm200_manifest_accepts_train_z_v6_adapter() -> None:
    manifest, manifest_hash = load_manifest(MM200_V6_ROOT / "manifest.yaml")

    assert len(manifest_hash) == 64
    assert manifest["serving"]["candidate_clusters"] == ["cluster-d"]
    assert manifest["serving"]["expected_runtime"] == {
        "tp_size": 8,
        "dp_size": 1,
        "context_length": 131072,
        "cuda_graph_max_bs": 16,
        "max_running_requests": 16,
    }
    assert manifest["rollout"]["dataset_name"] == "example-multimodal-200"
    assert (
        manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]["provider"]
        == "qianfancoder_adapter_v6"
    )
    assert manifest["reward"]["enabled"] is False


@pytest.mark.skipif(not (SECOND_ROUND_ROOT / "manifest.yaml").is_file(), reason="Private experiment fixture not distributed")
def test_sft53_second_round_manifest_uses_h100_lui_and_v13_reward() -> None:
    manifest, manifest_hash = load_manifest(SECOND_ROUND_ROOT / "manifest.yaml")

    assert len(manifest_hash) == 64
    assert manifest["serving"]["candidate_clusters"] == ["cluster-d"]
    assert manifest["serving"]["expected_runtime"] == {
        "tp_size": 8,
        "dp_size": 1,
        "context_length": 131072,
        "cuda_graph_max_bs": 48,
        "max_running_requests": 48,
    }
    assert manifest["rollout"]["dataset_name"] == "miaoda-second-rounds-maas-0615"
    assert manifest["rollout"]["expected_records"] == 435
    assert manifest["rollout"]["pipeline"] == "LuiPipeline"
    assert (
        manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]["provider"]
        == "qianfancoder_adapter_v6"
    )
    assert manifest["reward"] | {} == manifest["reward"] | {
        "enabled": True,
        "query_round": "second_round",
        "prompt_version": "v1_3",
        "benchmark_dataset_name": "miaoda-second-rounds-maas-0615",
        "concurrency": 30,
    }


@pytest.mark.skipif(not (SECOND_ROUND_OPENAI_ROOT / "manifest.yaml").is_file(), reason="Private experiment fixture not distributed")
def test_sft53_second_round_manifest_accepts_explicit_openai_config() -> None:
    manifest, manifest_hash = load_manifest(SECOND_ROUND_OPENAI_ROOT / "manifest.yaml")

    assert len(manifest_hash) == 64
    model = manifest["rollout"]["coding_config"]["codingPromptiModelConfig"]
    assert manifest["rollout"]["coding_config"]["codingPromptiVariant"] == ("qianfan-coder")
    assert model["provider"] == "openai"
    assert model["thinking_config"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert model["api_key_env"] == "AGENTOS_MODEL_API_KEY"
    assert "api_key" not in model


@pytest.mark.skipif(not (DEPLOY_ROOT / "config.yaml").is_file(), reason="Private deployment fixture not distributed")
def test_sft53_ltp_config_only_changes_checkpoint_identities() -> None:
    baseline = yaml.safe_load((DEPLOY_ROOT / "config.yaml").read_text(encoding="utf-8"))
    candidate = yaml.safe_load((DEPLOY_ROOT / "config-sft53.yaml").read_text(encoding="utf-8"))

    assert candidate["jobargs"] == baseline["jobargs"] | {"job_name": "demo-tuned-rollout"}
    assert candidate["modelargs"] == baseline["modelargs"] | {
        "job_base": "demo-tuned-rollout",
        "model_name": "demo-tuned-model",
        "model_path": (
            "/data/models/experiments/"
            "demo-tuned-checkpoint/iter_0000053_hf_bridge"
        ),
        "gateway_creator_id": "research-ops-qw38-sft53-i300",
        "log_dir": (
            "/srv/research-ops/workspaces/alice/qianfan_coder/"
            "glm_post_train/sglang_logs/qw38-sft53-i300-0825-formal-r1"
        ),
        "readiness_dir": (
            "/srv/research-ops/workspaces/alice/qianfan_coder/"
            "glm_post_train/sglang_logs/qw38-sft53-i300-0825-formal-r1/readiness"
        ),
        "gateway_preclean": "false",
    }
    assert candidate["envargs"] == baseline["envargs"]
    assert candidate["trainargs"] == baseline["trainargs"]

    run_script = (DEPLOY_ROOT / "run.sh").read_text(encoding="utf-8")
    assert 'JOB_LOG_DIR="${LOG_DIR}/jobs/${ACTUAL_JOB_NAME}"' in run_script
    assert 'READINESS_RECEIPT="${READINESS_DIR}/${ACTUAL_JOB_NAME}.json"' in run_script


def test_supervisor_contract_captures_dynamic_fleet_policy() -> None:
    contract = json.loads(SUPERVISOR_CONTRACT.read_text(encoding="utf-8"))

    assert contract["serve"]["min_ready_endpoints"] == 1
    assert contract["fleet"]["max_endpoints"] == 6
    assert contract["fleet"]["effective_concurrency_formula"] == ("min(48, healthy_endpoints * 8)")
    assert contract["teardown_serving_after_rollout"] is True


def test_manifest_rejects_inline_model_key(tmp_path: Path) -> None:
    text = DEFAULT_MANIFEST.read_text(encoding="utf-8")
    unsafe = text.replace(
        'api_key_env: "AGENTOS_MODEL_API_KEY"',
        'api_key: "inline-secret"',
    )
    path = tmp_path / "unsafe.yaml"
    path.write_text(unsafe, encoding="utf-8")

    with pytest.raises(WorkflowConfigurationError, match="environment reference"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (AgentOSBatchSnapshot("one", "running", {"running": 8, "pending": 292}, 300), "running"),
        (
            AgentOSBatchSnapshot("one", "completed", {"completed": 299, "pending": 1}, 300),
            "mixed_terminal",
        ),
        (
            AgentOSBatchSnapshot("one", "completed", {"completed": 295, "failed": 5}, 300),
            "completed",
        ),
    ],
)
def test_rollout_phase_requires_every_record_to_settle(
    snapshot: AgentOSBatchSnapshot,
    expected: str,
) -> None:
    assert rollout_phase(snapshot) == expected


def test_formal_rollout_phase_rejects_wrong_terminal_count() -> None:
    snapshot = AgentOSBatchSnapshot(
        "one",
        "completed",
        {"completed": 299},
        299,
    )

    assert rollout_phase(snapshot, expected_total=300) == "count_mismatch"


def _write_readiness(path: Path, ready_at: datetime) -> str:
    endpoint = "http://192.0.2.8:14234"
    path.write_text(
        json.dumps(
            {
                "ready_at_utc": ready_at.isoformat().replace("+00:00", "Z"),
                "job_name": "unknown",
                "job_base": "demo-base-rollout",
                "model_name": "demo-base-model",
                "endpoint": endpoint,
                "gateway_api_url": "http://198.51.100.51:8080",
                "tp_size": 8,
                "dp_size": 1,
                "context_length": 131072,
                "cuda_graph_max_bs": 48,
                "max_running_requests": 48,
            }
        ),
        encoding="utf-8",
    )
    return endpoint


def test_serving_probe_requires_exact_fresh_runtime_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_manifest(DEFAULT_MANIFEST)
    serving_config = copy.deepcopy(manifest["serving"])
    readiness = tmp_path / "READINESS.json"
    endpoint = _write_readiness(readiness, datetime.now(UTC))
    serving_config["readiness_file"] = str(readiness)
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/proxy/endpoint/list":
            return httpx.Response(200, json={"data": {"endpoints": [endpoint]}})
        if request.url.path == "/v1/chat/completions":
            seen_authorization.append(request.headers["Authorization"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "4"}}]},
            )
        return httpx.Response(404)

    runtime = ServingRuntime(
        serving_config,
        manifest["teardown"],
        actual_job_name="demo-base-rollout-abcdef",
        model_api_key="runtime-key",
        transport=httpx.MockTransport(handler),
    )

    def fake_ltp(action: str, *, logs: bool = False) -> subprocess.CompletedProcess[str]:
        assert action == "show"
        assert logs is False
        submitted = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        return subprocess.CompletedProcess(
            ["ltp-cli"],
            0,
            stdout=f"status: Running\nsubmission_time: {submitted}\n",
            stderr="",
        )

    monkeypatch.setattr(runtime, "_ltp", fake_ltp)

    observed = runtime.probe()

    assert observed["endpoint"] == endpoint
    assert seen_authorization == ["Bearer runtime-key"]


def test_serving_probe_rejects_receipt_before_ltp_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_manifest(DEFAULT_MANIFEST)
    serving_config = copy.deepcopy(manifest["serving"])
    readiness = tmp_path / "READINESS.json"
    _write_readiness(readiness, datetime.now(UTC) - timedelta(minutes=10))
    serving_config["readiness_file"] = str(readiness)
    runtime = ServingRuntime(
        serving_config,
        manifest["teardown"],
        actual_job_name="demo-base-rollout-abcdef",
        model_api_key="runtime-key",
    )
    submitted = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(
        runtime,
        "_ltp",
        lambda _action, logs=False: subprocess.CompletedProcess(
            ["ltp-cli"],
            0,
            stdout=f"status: Running\nsubmission_time: {submitted}\n",
            stderr="",
        ),
    )

    with pytest.raises(ServiceNotReady, match="readiness_predates_ltp_submission"):
        runtime.probe()


def test_serving_teardown_removes_only_owned_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_manifest(DEFAULT_MANIFEST)
    owned = "http://192.0.2.8:14234"
    unrelated = "http://192.0.2.9:14234"
    endpoints = [owned, unrelated]
    deleted_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/endpoint/list":
            return httpx.Response(200, json={"data": {"endpoints": list(endpoints)}})
        if request.url.path == "/proxy/endpoint/delete":
            payload = json.loads(request.content)
            deleted_payloads.append(payload)
            endpoints.remove(payload["endpoints"][0])
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/proxy/delete":
            raise AssertionError("model must remain while an unrelated endpoint is registered")
        return httpx.Response(404)

    runtime = ServingRuntime(
        manifest["serving"],
        manifest["teardown"],
        actual_job_name="demo-base-rollout-abcdef",
        model_api_key="runtime-key",
        transport=httpx.MockTransport(handler),
    )

    def fake_ltp(action: str, *, logs: bool = False) -> subprocess.CompletedProcess[str]:
        if action == "delete":
            return subprocess.CompletedProcess(["ltp-cli"], 0, stdout="", stderr="")
        assert action == "show" and logs is True
        return subprocess.CompletedProcess(
            ["ltp-cli"],
            1,
            stdout="",
            stderr="job not found",
        )

    monkeypatch.setattr(runtime, "_ltp", fake_ltp)

    receipt = runtime.teardown(endpoint=owned, timeout_seconds=1, poll_interval=0.01)

    assert deleted_payloads == [
        {
            "model_name": "demo-base-model",
            "endpoints": [owned],
        }
    ]
    assert receipt["remaining_endpoints"] == [unrelated]


def test_serving_teardown_does_not_treat_control_plane_error_as_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_manifest(DEFAULT_MANIFEST)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxy/endpoint/list":
            return httpx.Response(200, json={"data": {"endpoints": []}})
        if request.url.path == "/proxy/delete":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    runtime = ServingRuntime(
        manifest["serving"],
        manifest["teardown"],
        actual_job_name="demo-base-rollout-abcdef",
        model_api_key="runtime-key",
        transport=httpx.MockTransport(handler),
    )

    def fake_ltp(action: str, *, logs: bool = False) -> subprocess.CompletedProcess[str]:
        if action == "delete":
            return subprocess.CompletedProcess(["ltp-cli"], 0, stdout="", stderr="")
        assert action == "show" and logs is True
        return subprocess.CompletedProcess(
            ["ltp-cli"],
            1,
            stdout="",
            stderr="temporary control plane error",
        )

    monkeypatch.setattr(runtime, "_ltp", fake_ltp)

    with pytest.raises(RuntimeError, match="ltp_teardown_verification_failed"):
        runtime.teardown(endpoint=None, timeout_seconds=1, poll_interval=0.01)


def test_job_ledger_migrates_primary_and_attaches_non_preclean_replica(
    tmp_path: Path,
) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    output_root = tmp_path / "workflow"
    legacy_readiness = tmp_path / "legacy" / "READINESS.json"
    manifest["runtime"]["output_root"] = str(output_root)
    manifest["serving"]["readiness_file"] = str(legacy_readiness)
    manifest["serving"]["readiness_dir"] = str(tmp_path / "readiness")
    manifest["rollout"]["configured_max_concurrency"] = 48
    output_root.mkdir(parents=True)
    primary = "demo-base-rollout-primary"
    (output_root / "submitted_jobs.tsv").write_text(
        "logical_job_name\tactual_job_name\tuser\tcluster\ttotal_nodes\t"
        "gpus_per_node\tsubmission_time_utc\tlog_dir\n"
        f"demo-base-rollout\t{primary}\talice\ttrain_p\t1\t8\t"
        "2026-08-25T00:00:00Z\t/tmp/logs\n",
        encoding="utf-8",
    )

    migrated = ensure_primary_job_ledger(manifest, primary)

    assert migrated[0]["readiness_file"] == str(legacy_readiness)
    assert migrated[0]["gateway_preclean"] == "true"

    replica = "demo-base-rollout-replica"
    attached, added = attach_job_to_ledger(
        manifest,
        replica,
        {
            "cluster": "cluster-reserved-a",
            "total_nodes": "1",
            "submission_time": "2026-08-25T01:00:00Z",
        },
    )
    duplicate, added_again = attach_job_to_ledger(manifest, replica, {})

    assert added is True
    assert added_again is False
    assert duplicate == attached
    assert attached["gateway_preclean"] == "false"
    assert attached["readiness_file"] == str(tmp_path / "readiness" / f"{replica}.json")


def test_job_ledger_uses_configured_receipt_for_new_primary(tmp_path: Path) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    output_root = tmp_path / "workflow"
    primary_readiness = tmp_path / "fixed" / "READINESS.json"
    manifest["runtime"]["output_root"] = str(output_root)
    manifest["serving"]["readiness_file"] = str(primary_readiness)
    manifest["serving"]["readiness_dir"] = str(tmp_path / "readiness")
    output_root.mkdir(parents=True)
    primary = "demo-base-rollout-primary"

    rows = ensure_primary_job_ledger(manifest, primary)

    assert rows[0]["readiness_file"] == str(primary_readiness)


def test_endpoint_capacity_drives_batch_concurrency_without_zero_termination(
    tmp_path: Path,
) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    manifest["runtime"]["output_root"] = str(tmp_path / "workflow")
    manifest["rollout"]["configured_max_concurrency"] = 48
    snapshots = [
        {
            "model_name": "model",
            "requested_replicas": 2,
            "ready_replicas": 2,
            "endpoint_count": 2,
            "endpoints": ["http://one", "http://two"],
            "jobs": [],
            "job_errors": {},
        },
        {
            "model_name": "model",
            "requested_replicas": 2,
            "ready_replicas": 0,
            "endpoint_count": 0,
            "endpoints": [],
            "jobs": [],
            "job_errors": {"one": "unavailable", "two": "unavailable"},
        },
    ]

    class CapacityServing:
        def probe(self) -> dict[str, Any]:
            return snapshots.pop(0)

    datawheel = FakeDatawheel()
    workflow = RolloutRewardWorkflow(
        manifest,
        "fixture-manifest-hash",
        actual_job_name="demo-base-rollout-primary",
        datawheel=datawheel,  # type: ignore[arg-type]
        goblin=FakeGoblin(),  # type: ignore[arg-type]
        serving=CapacityServing(),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )
    state: dict[str, Any] = {}

    workflow._refresh_serving_capacity(state, batch_id="batch-fixture")
    assert state["effective_concurrency"] == 16
    assert state["applied_concurrency"] == 16
    assert datawheel.concurrency_updates == [("batch-fixture", 16)]

    workflow._refresh_serving_capacity(state, batch_id="batch-fixture")
    assert state["effective_concurrency"] == 0
    assert state["applied_concurrency"] == 16
    assert "not applied automatically" in state["concurrency_update_deferred"]
    assert datawheel.concurrency_updates == [("batch-fixture", 16)]


def test_poll_rollout_corrects_remote_concurrency_drift(tmp_path: Path) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    manifest["runtime"]["output_root"] = str(tmp_path / "workflow")

    class DriftedDatawheel(FakeDatawheel):
        def __init__(self) -> None:
            super().__init__()
            self.snapshots = [
                AgentOSBatchSnapshot(
                    "batch-fixture",
                    "running",
                    {"pending": 280, "running": 20},
                    300,
                    {"concurrency": 20},
                ),
                AgentOSBatchSnapshot(
                    "batch-fixture",
                    "completed",
                    {"completed": 300},
                    300,
                    {"concurrency": 8},
                ),
            ]

        def get_batch(self, batch_task_id: str) -> AgentOSBatchSnapshot:
            assert batch_task_id == "batch-fixture"
            return self.snapshots.pop(0)

    datawheel = DriftedDatawheel()
    workflow = RolloutRewardWorkflow(
        manifest,
        "fixture-manifest-hash",
        actual_job_name="demo-base-rollout-primary",
        datawheel=datawheel,  # type: ignore[arg-type]
        goblin=FakeGoblin(),  # type: ignore[arg-type]
        serving=FakeServing(),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )
    state = {
        "applied_concurrency": 8,
        "rollout_submitted_at": datetime.now(UTC).isoformat(),
    }

    assert workflow._poll_rollout(state, "batch-fixture") == "completed"
    assert datawheel.concurrency_updates == [("batch-fixture", 8)]
    assert state["observed_remote_concurrency"] == 8
    assert state["applied_concurrency"] == 8
    assert state["rollout_terminal_at"]
    assert state["rollout_elapsed_seconds"] >= 0


def test_reward_disabled_does_not_require_reward_client(tmp_path: Path) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    manifest["runtime"]["output_root"] = str(tmp_path / "workflow")
    manifest["reward"] = {"enabled": False}
    workflow = RolloutRewardWorkflow(
        manifest,
        "fixture-manifest-hash",
        actual_job_name="demo-base-rollout-primary",
        datawheel=FakeDatawheel(),  # type: ignore[arg-type]
        goblin=None,
        serving=FakeServing(),  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )
    state: dict[str, Any] = {}

    assert workflow._ensure_reward(state, "unused-batch") == 0
    assert state["phase"] == "complete"


def test_fleet_teardown_cleans_every_ledger_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    manifest["runtime"]["output_root"] = str(tmp_path / "workflow")
    fleet = ServingFleet(manifest, model_api_key="runtime-key")
    rows = [
        {"actual_job_name": "job-one", "readiness_file": str(tmp_path / "one.json")},
        {"actual_job_name": "job-two", "readiness_file": str(tmp_path / "two.json")},
    ]
    calls: list[tuple[str, str | None]] = []

    class TeardownRuntime:
        def __init__(self, identity: str) -> None:
            self.identity = identity

        def teardown(
            self,
            *,
            endpoint: str | None,
            timeout_seconds: int,
            poll_interval: float,
        ) -> dict[str, Any]:
            assert timeout_seconds == 5
            assert poll_interval == 0.1
            calls.append((self.identity, endpoint))
            return {
                "ltp_job_name": self.identity,
                "removed_endpoints": [endpoint] if endpoint else [],
            }

        def _gateway_endpoints(self) -> list[str]:
            return []

    monkeypatch.setattr(fleet, "_jobs", lambda: rows)
    monkeypatch.setattr(
        fleet,
        "_runtime",
        lambda row: TeardownRuntime(row["actual_job_name"]),
    )
    receipt = fleet.teardown(
        serving_snapshot={
            "jobs": [
                {"job_name": "job-one", "endpoint": "http://one"},
                {"job_name": "job-two", "endpoint": "http://two"},
            ]
        },
        timeout_seconds=5,
        poll_interval=0.1,
    )

    assert calls == [("job-one", "http://one"), ("job-two", "http://two")]
    assert len(receipt["jobs"]) == 2
    assert receipt["remaining_endpoints"] == []


def test_workflow_runs_once_and_resumes_without_duplicate_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_MODEL_API_KEY", "runtime-model-key")
    monkeypatch.setenv("DATAWHEEL_GENERATED_USER_ID", "generated-user")
    source, _ = load_manifest(DEFAULT_MANIFEST)
    manifest = copy.deepcopy(source)
    manifest["runtime"]["output_root"] = str(tmp_path / "workflow")
    datawheel = FakeDatawheel()
    goblin = FakeGoblin()
    serving = FakeServing()
    workflow = RolloutRewardWorkflow(
        manifest,
        "fixture-manifest-hash",
        actual_job_name="demo-base-rollout-fixture",
        datawheel=datawheel,  # type: ignore[arg-type]
        goblin=goblin,  # type: ignore[arg-type]
        serving=serving,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )

    assert workflow.run() == 0
    assert workflow.run() == 0
    assert len(datawheel.submissions) == 1
    assert len(goblin.submissions) == 1
    assert serving.probes == 1
    assert serving.teardowns == 1
    assert datawheel.downloads == 1

    submitted_model = datawheel.submissions[0]["metadata"]["codingConfig"]
    submitted_model = submitted_model["codingPromptiModelConfig"]
    assert submitted_model["api_key"] == "runtime-model-key"
    assert goblin.submissions[0]["query_round"] == "first_round"
    assert goblin.submissions[0]["prompt_version"] == "v1_6"
    assert goblin.submissions[0]["eval_mm"] is True
    assert goblin.submissions[0]["eval_miniprog"] is True

    persisted = (tmp_path / "workflow/state.json").read_text(encoding="utf-8")
    assert json.loads(persisted)["phase"] == "complete"
    assert "runtime-model-key" not in persisted
