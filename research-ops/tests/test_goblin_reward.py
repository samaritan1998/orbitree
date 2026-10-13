from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import pytest
from research_ops.agentos.client import AgentOSBatchSnapshot
from research_ops.openreward import MiaodaDatawheelGoblinBackend, MiaodaOnlineConfig
from research_ops.openreward.miaoda_online import (
    MiaodaOnlineError,
    _load_policy_calls,
    _xlsx_reward_input,
)
from research_ops.reward import (
    GoblinConfigurationError,
    GoblinJobFailed,
    GoblinRewardClient,
    GoblinRewardError,
    reward_map_from_file,
)
from research_ops.reward.goblin import GoblinJobSnapshot


def test_miaoda_terminal_loads_bounded_v2_policy_calls(tmp_path: Path) -> None:
    capture = tmp_path / "capture-episode-1.jsonl"
    row = {
        "schema_version": "mopd-policy-capture.v2",
        "capture_id": "capture-1",
        "captured_at": "2026-08-03T00:00:01+00:00",
        "episode_id": "episode-1",
        "prompt_token_ids": [1, 2],
        "response_token_ids": [3, 4],
        "behavior_logprobs": [-0.1, -0.2],
        "weight_version": "iter-3",
    }
    capture.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")

    calls = _load_policy_calls(
        capture,
        episode_id="episode-1",
        started_at=0.0,
    )

    assert calls == [
        {
            "capture_id": "capture-1",
            "prompt_token_ids": [1, 2],
            "response_token_ids": [3, 4],
            "behavior_logprobs": [-0.1, -0.2],
            "weight_version": "iter-3",
        }
    ]


def test_shared_path_goblin_input_normalizes_datawheel_xlsx(tmp_path: Path) -> None:
    workbook = tmp_path / "result.xlsx"
    shared = ["id", "应用类型", "app_id", "conv_id", "1", "MINIPROGRAM", "app-1", "conv-1"]
    shared_xml = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )
    sheet_xml = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>
          <c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>
        <row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2" t="s"><v>5</v></c>
          <c r="C2" t="s"><v>6</v></c><c r="D2" t="s"><v>7</v></c></row>
      </sheetData>
    </worksheet>"""
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    normalized = _xlsx_reward_input(workbook)
    record = json.loads(normalized.read_text(encoding="utf-8"))
    assert record["app_type"] == "MINIPROGRAM"
    assert record["case_id"] == "1"
    assert record["generated_app_id"] == "app-1"
    assert record["generated_conv_id"] == "conv-1"


def test_goblin_upload_submit_and_poll_are_credential_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TEST_GOBLIN_TOKEN", "not-for-persistence")
    source = tmp_path / "rollout.jsonl"
    source.write_text('{"case_id":"case-1","query":"demo"}\n', encoding="utf-8")
    calls: list[httpx.Request] = []
    statuses = iter(("running", "completed"))

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-API-Token"] == "not-for-persistence"
        if request.url.path.endswith("/jobs/upload"):
            body = request.content
            assert b'filename="rollout.jsonl"' in body
            assert b'"rollout_model": "policy-r1"' in body
            return httpx.Response(
                202,
                json={
                    "job_id": "job-1",
                    "status": "pending",
                    "workdir_prefix": "/safe/work",
                    "log_dir": "/safe/log",
                },
            )
        if request.url.path.endswith("/jobs/job-1"):
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": next(statuses),
                    "total": 1,
                    "completed": 1,
                    "succeeded": 1,
                    "failed": 0,
                    "result_file": str(tmp_path / "reward.jsonl"),
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = GoblinRewardClient(
        "https://goblin.example",
        token_env="TEST_GOBLIN_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    submitted = client.submit_file(
        source,
        rollout_model="policy-r1",
        benchmark_dataset_name="miaoda-online-mopd-train-v1",
        concurrency=2,
        upload=True,
    )
    completed = client.wait("job-1", poll_interval_seconds=0.001, timeout_seconds=1)
    assert submitted.job_id == "job-1"
    assert submitted.status == "pending"
    assert completed.successful is True
    assert len(calls) == 3
    assert all("not-for-persistence" not in str(call) for call in calls)


def test_goblin_client_requires_token_at_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_GOBLIN_TOKEN", raising=False)
    client = GoblinRewardClient(
        "https://goblin.example",
        token_env="TEST_GOBLIN_TOKEN",
    )
    with pytest.raises(GoblinConfigurationError):
        client.get_job("job-1")


def test_miaoda_shared_path_requires_absolute_result_root() -> None:
    with pytest.raises(MiaodaOnlineError, match="absolute"):
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            reward_upload=False,
            reward_result_root=Path("relative/reward-root"),
            reward_workdir_prefix=Path("relative/reward-root"),
        )


def test_goblin_failed_job_is_not_silently_converted_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_GOBLIN_TOKEN", "runtime-token")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "job-fail", "status": "failed"})

    client = GoblinRewardClient(
        "https://goblin.example",
        token_env="TEST_GOBLIN_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoblinJobFailed) as error:
        client.wait("job-fail", poll_interval_seconds=0.001, timeout_seconds=1)
    assert error.value.snapshot.status == "failed"


def test_reward_map_from_jsonl_requires_explicit_numeric_joinable_reward(tmp_path) -> None:
    source = tmp_path / "result.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"case_id": "case-1", "reward": 0.75}),
                json.dumps({"app_id": "app-2", "overall_reward": "80%"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert reward_map_from_file(source) == {"case-1": 0.75, "app-2": 0.8}

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text('{"case_id":"case-3","rating":"good"}\n', encoding="utf-8")
    with pytest.raises(GoblinRewardError, match="no numeric reward field"):
        reward_map_from_file(invalid)


def test_miaoda_online_backend_waits_for_agentos_then_goblin(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "request-only-model-key")
    reward_file = tmp_path / "goblin-result.jsonl"
    reward_file.write_text(
        json.dumps({"case_id": "episode-1", "reward": 0.82}) + "\n",
        encoding="utf-8",
    )

    class FakeDatawheel:
        def __init__(self) -> None:
            self.statuses = iter(("running", "completed"))
            self.submitted = []

        def submit_batch(self, payload, *, idempotency_key):
            self.submitted.append((payload, idempotency_key))
            return AgentOSBatchSnapshot(batch_task_id="batch-1", status="submitted")

        def get_batch(self, batch_task_id):
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status=next(self.statuses))

        def download_result(self, batch_task_id, destination: Path):
            destination.write_bytes(b"rollout-artifact")
            return destination

    class FakeGoblin:
        def __init__(self) -> None:
            self.submitted = []
            self.statuses = iter(("running", "completed"))

        def submit_file(self, path, **kwargs):
            self.submitted.append((path, kwargs))
            return GoblinJobSnapshot(job_id="goblin-1", status="pending")

        def get_job(self, job_id):
            return GoblinJobSnapshot(
                job_id=job_id,
                status=next(self.statuses),
                result_file=str(reward_file),
            )

    datawheel = FakeDatawheel()
    goblin = FakeGoblin()
    backend = MiaodaDatawheelGoblinBackend(
        datawheel,
        goblin,
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://mopd-gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            benchmark_dataset_name="miaoda-online-mopd-train-v1",
        ),
    )
    task = {"episode_id": "episode-1", "query": "build an app"}
    state: dict[str, object] = {}
    backend.setup(task_spec=task, secrets={}, state=state)

    async def run():
        outputs = [
            await backend.execute(
                action=action,
                payload={},
                task_spec=task,
                state=state,
            )
            for action in (
                "start_rollout",
                "poll_rollout",
                "poll_rollout",
                "poll_rollout",
                "poll_rollout",
            )
        ]
        return outputs

    import asyncio

    outputs = asyncio.run(run())
    assert [output.finished for output in outputs] == [False, False, False, False, True]
    assert outputs[-1].reward == 0.82
    assert outputs[-1].metadata["usable"] is True
    assert len(datawheel.submitted) == 1
    assert len(goblin.submitted) == 1
    assert goblin.submitted[0][1]["rollout_model"] == "policy-r1"


def test_miaoda_pure_opd_finishes_after_agentos_without_goblin(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pure OPD uses teacher logprobs and must not block on task reward."""

    monkeypatch.setenv("TEST_MODEL_KEY", "request-only-model-key")

    class Datawheel:
        def submit_batch(self, payload, *, idempotency_key):
            del payload, idempotency_key
            return AgentOSBatchSnapshot(batch_task_id="batch-opd", status="submitted")

        def get_batch(self, batch_task_id):
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status="completed")

        def download_result(self, batch_task_id, destination: Path):
            del batch_task_id
            destination.write_bytes(b"agentos-artifact")
            return destination

    class GoblinMustNotRun:
        def submit_file(self, path, **kwargs):
            del path, kwargs
            raise AssertionError("Goblin must not run for pure OPD")

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
        ),
    )
    task = {
        "episode_id": "episode-opd",
        "query": "build an app",
        "training_objective": "pure_opd",
        "task_reward_required": False,
    }
    state: dict[str, object] = {}
    backend.setup(task_spec=task, secrets={}, state=state)

    import asyncio

    async def run():
        await backend.execute(action="start_rollout", payload={}, task_spec=task, state=state)
        return await backend.execute(
            action="poll_rollout", payload={}, task_spec=task, state=state
        )

    output = asyncio.run(run())
    assert output.finished is True
    assert output.reward == 0.0
    assert output.metadata["usable"] is True
    assert output.metadata["task_reward_used"] is False
    assert output.metadata["terminal_reason"] == "pure_opd_no_task_reward"
    assert output.metadata["miaoda_artifact"].endswith("datawheel-batch-opd.xlsx")
    assert "goblin_job_id" not in output.metadata


def test_miaoda_online_backend_captures_datawheel_join_ids(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Datawheel's generated IDs are available as Goblin join candidates."""

    monkeypatch.setenv("TEST_MODEL_KEY", "request-only-model-key")
    reward_file = tmp_path / "goblin-result.jsonl"
    reward_file.write_text(
        json.dumps({"跑批单条记录id": "record-uuid", "reward": 0.73}) + "\n",
        encoding="utf-8",
    )

    class Datawheel:
        def submit_batch(self, payload, *, idempotency_key):
            del payload, idempotency_key
            return AgentOSBatchSnapshot(batch_task_id="batch-ids", status="submitted")

        def get_batch(self, batch_task_id):
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status="completed")

        def download_result(self, batch_task_id, destination: Path):
            del batch_task_id
            destination.write_bytes(b"not-a-real-xlsx")
            return destination

    class Goblin:
        def submit_file(self, path, **kwargs):
            del path, kwargs
            return GoblinJobSnapshot(job_id="goblin-ids", status="pending")

        def get_job(self, job_id):
            del job_id
            return GoblinJobSnapshot(
                job_id="goblin-ids",
                status="completed",
                result_file=str(reward_file),
            )

    # Avoid depending on an optional Excel parser in this unit test while
    # exercising the same allow-listed fields as the real workbook.
    monkeypatch.setattr(
        "research_ops.openreward.miaoda_online.load_reward_records",
        lambda _path: [
            {
                "id": "1",
                "app_id": "app-generated",
                "conv_id": "conv-generated",
                "跑批单条记录id": "record-uuid",
            }
        ],
    )
    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        Goblin(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
        ),
    )
    task = {"episode_id": "episode-ids", "query": "build an app"}
    state: dict[str, object] = {}
    backend.setup(task_spec=task, secrets={}, state=state)

    import asyncio

    async def run():
        await backend.execute(action="start_rollout", payload={}, task_spec=task, state=state)
        await backend.execute(action="poll_rollout", payload={}, task_spec=task, state=state)
        return await backend.execute(
            action="poll_rollout", payload={}, task_spec=task, state=state
        )

    output = asyncio.run(run())
    assert output.reward == 0.73
    assert output.metadata["miaoda_app_id"] == "app-generated"
    assert output.metadata["miaoda_conv_id"] == "conv-generated"
    assert output.metadata["miaoda_record_uuid"] == "record-uuid"


def test_miaoda_online_backend_isolates_goblin_workdir_and_preserves_rating_label(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "request-only-model-key")
    class Datawheel:
        def submit_batch(self, payload, *, idempotency_key):
            del payload, idempotency_key
            return AgentOSBatchSnapshot(batch_task_id="batch-label", status="submitted")

        def get_batch(self, batch_task_id):
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status="completed")

        def download_result(self, batch_task_id, destination: Path):
            del batch_task_id
            destination.write_bytes(b"not-a-real-xlsx")
            return destination

    class Goblin:
        def submit_file(self, path, **kwargs):
            assert path.suffix == ".xlsx"
            workdir = Path(kwargs["workdir_prefix"])
            assert workdir.name == "goblin-batch-label"
            assert Path(kwargs["result_file"]) == workdir / "result.jsonl"
            assert Path(kwargs["log_dir"]) == workdir / "logs"
            (workdir / "1").mkdir(parents=True)
            (workdir / "1" / "rollout.msg").write_text(
                '最终评级：基本可用\n{"rating":"基本可用"}\n',
                encoding="utf-8",
            )
            Path(kwargs["result_file"]).write_text("{}\n", encoding="utf-8")
            return GoblinJobSnapshot(job_id="goblin-label", status="pending")

        def get_job(self, job_id):
            del job_id
            return GoblinJobSnapshot(
                job_id="goblin-label",
                status="completed",
                result_file=str(
                    tmp_path / "reward" / "goblin-batch-label" / "result.jsonl"
                ),
            )

    reward_root = tmp_path / "reward"
    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        Goblin(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            reward_result_root=reward_root,
            reward_workdir_prefix=reward_root,
            reward_upload=False,
        ),
    )
    task = {"episode_id": "episode-label", "query": "build an app"}
    state: dict[str, object] = {}
    backend.setup(task_spec=task, secrets={}, state=state)

    import asyncio

    async def run():
        await backend.execute(action="start_rollout", payload={}, task_spec=task, state=state)
        await backend.execute(action="poll_rollout", payload={}, task_spec=task, state=state)
        return await backend.execute(
            action="poll_rollout", payload={}, task_spec=task, state=state
        )

    output = asyncio.run(run())
    assert output.finished is True
    assert output.reward is None
    assert output.metadata["usable"] is False
    assert output.metadata["scalar_reward_available"] is False
    assert output.metadata["reward_label"] == "基本可用"
    assert output.metadata["terminal_reason"] == "reward_label_only"
