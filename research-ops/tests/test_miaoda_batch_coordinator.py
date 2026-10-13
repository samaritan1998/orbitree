from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import research_ops.openreward.miaoda_online as miaoda_online
from research_ops.agentos import AgentOSBatchSnapshot
from research_ops.openreward import MiaodaDatawheelGoblinBackend, MiaodaOnlineConfig
from research_ops.openreward.correlation import strip_episode_correlation


class _GoblinMustNotRun:
    def submit_file(self, path, **kwargs):
        del path, kwargs
        raise AssertionError("pure OPD must not submit Goblin reward")


def _capture(
    *, group_id: str, capture_id: str, assistant: str, response_token: int
) -> dict[str, object]:
    prompt = [{"role": "user", "content": "same query"}]
    message = {"role": "assistant", "content": assistant}
    return {
        "schema_version": "mopd-policy-capture.v2",
        "captured_at": datetime.now(UTC).isoformat(),
        "capture_id": capture_id,
        "episode_id": group_id,
        "prompt_sha256": miaoda_online._json_sha256(prompt),
        "response_message_sha256": miaoda_online._json_sha256(
            miaoda_online._message_identity(message)
        ),
        "prompt_token_ids": [10, 11],
        "response_token_ids": [response_token],
        "behavior_logprobs": [-0.1 * response_token],
        "weight_version": "iter-3",
    }


def test_pure_opd_sessions_share_one_datawheel_batch_and_join_exact_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")
    submitted: list[dict[str, object]] = []

    class Datawheel:
        status_calls = 0
        download_calls = 0

        def submit_batch(self, payload, *, idempotency_key):
            assert idempotency_key.startswith("ors-miaoda-")
            submitted.append(payload)
            return AgentOSBatchSnapshot(batch_task_id="batch-shared", status="submitted")

        def get_batch(self, batch_task_id):
            assert batch_task_id == "batch-shared"
            self.status_calls += 1
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status="completed")

        def download_result(self, batch_task_id, destination):
            assert batch_task_id == "batch-shared"
            self.download_calls += 1
            destination.write_bytes(b"shared-artifact")
            return destination

        def get_trajectory(self, app_id):
            assistant = {"app-1": "first answer", "app-2": "second answer"}[app_id]
            return {
                "messages": [
                    {"role": "user", "content": "same query"},
                    {"role": "assistant", "content": assistant},
                ]
            }

    rows = [
        {"id": "1", "query": "same query", "app_id": "app-1"},
        {"id": "2", "query": "same query", "app_id": "app-2"},
    ]
    monkeypatch.setattr(miaoda_online, "load_reward_records", lambda _path: rows)
    datawheel = Datawheel()
    backend = MiaodaDatawheelGoblinBackend(
        datawheel,
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=tmp_path / "captures",
            submission_batch_size=2,
            submission_batch_window_seconds=10,
            datawheel_poll_cache_seconds=0,
            concurrency=32,
        ),
    )
    tasks = [
        {
            "episode_id": f"episode-{index}",
            "query": "same query",
            "training_objective": "pure_opd",
            "task_reward_required": False,
        }
        for index in (1, 2)
    ]
    states: list[dict[str, object]] = [{}, {}]
    for task, state in zip(tasks, states, strict=True):
        backend.setup(task_spec=task, secrets={}, state=state)

    async def run():
        starts = await asyncio.gather(
            *(
                backend.execute(
                    action="start_rollout", payload={}, task_spec=task, state=state
                )
                for task, state in zip(tasks, states, strict=True)
            )
        )
        for index, state in enumerate(states, start=1):
            capture_path = Path(str(state["mopd_capture_file"]))
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_text(
                json.dumps(
                    _capture(
                        group_id=str(state["miaoda_capture_episode_id"]),
                        capture_id=f"capture-{index}",
                        assistant=("first answer" if index == 1 else "second answer"),
                        response_token=20 + index,
                    )
                )
                + "\n"
            )
            http_path = capture_path.with_name(
                capture_path.name.replace("capture-", "http-", 1)
            )
            http_path.write_text(
                json.dumps(
                    {
                        "schema_version": "mopd-policy-http.v1",
                        "captured_at": datetime.now(UTC).isoformat(),
                        "episode_id": str(state["miaoda_capture_episode_id"]),
                        "request_id": f"request-{index}",
                        "status_code": 200,
                    }
                )
                + "\n"
            )
        terminals = await asyncio.gather(
            *(
                backend.execute(
                    action="poll_rollout", payload={}, task_spec=task, state=state
                )
                for task, state in zip(tasks, states, strict=True)
            )
        )
        return starts, terminals

    starts, terminals = asyncio.run(run())
    assert len(submitted) == 1
    assert len(submitted[0]["records"]) == 2
    assert submitted[0]["concurrency"] == 32
    assert submitted[0]["remainingAutoRetryCount"] == 0
    tagged_queries = [record["query"] for record in submitted[0]["records"]]
    cleaned = [strip_episode_correlation(value) for value in tagged_queries]
    assert [value for value, _token in cleaned] == ["same query", "same query"]
    assert len({token for _value, token in cleaned}) == 2
    api_url = submitted[0]["metadata"]["codingConfig"]["codingPromptiModelConfig"][
        "api_url"
    ]
    assert parse_qs(urlsplit(api_url).query)["mopd_episode_id"][0].startswith("batch-")
    assert parse_qs(urlsplit(api_url).query)["mopd_require_record_correlation"] == ["1"]
    assert all(not output.finished for output in starts)
    assert all(
        output.finished and output.metadata["usable"] for output in terminals
    ), [(output.finished, output.metadata) for output in terminals]
    assert datawheel.status_calls == 1
    assert datawheel.download_calls == 1
    assert terminals[0].metadata["policy_calls"][0]["response_token_ids"] == [21]
    assert terminals[1].metadata["policy_calls"][0]["response_token_ids"] == [22]
    assert terminals[0].metadata["miaoda_policy_http_statuses"] == [200]
    assert terminals[1].metadata["miaoda_policy_http_statuses"] == [200]
    for task, state in zip(tasks, states, strict=True):
        backend.teardown(task_spec=task, state=state)
    assert backend._batch_runtimes == {}


def test_explicit_eight_record_batch_ignores_legacy_timer_and_submits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")
    submitted: list[tuple[dict[str, object], str]] = []

    class Datawheel:
        def submit_batch(self, payload, *, idempotency_key):
            submitted.append((payload, idempotency_key))
            return AgentOSBatchSnapshot(
                batch_task_id="batch-explicit-eight", status="submitted"
            )

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=tmp_path / "captures",
            submission_batch_size=8,
            submission_batch_window_seconds=0.001,
            explicit_batch_wait_timeout_seconds=1,
            concurrency=32,
        ),
    )
    tasks = [
        {
            "episode_id": f"episode-{index}",
            "query": f"query {index}",
            "training_objective": "pure_opd",
            "task_reward_required": False,
            "optimizer_batch_id": "slime-r1-f0-test",
            "optimizer_batch_expected_count": 8,
            "optimizer_batch_member_index": index,
        }
        for index in range(8)
    ]
    states: list[dict[str, object]] = [{} for _ in tasks]
    for task, state in zip(tasks, states, strict=True):
        backend.setup(task_spec=task, secrets={}, state=state)

    async def run():
        first_seven = [
            asyncio.create_task(
                backend.execute(
                    action="start_rollout", payload={}, task_spec=task, state=state
                )
            )
            for task, state in zip(tasks[:7], states[:7], strict=True)
        ]
        await asyncio.sleep(0.02)
        assert submitted == []
        eighth = asyncio.create_task(
            backend.execute(
                action="start_rollout",
                payload={},
                task_spec=tasks[7],
                state=states[7],
            )
        )
        return await asyncio.gather(*first_seven, eighth)

    starts = asyncio.run(run())
    assert len(submitted) == 1
    payload, idempotency_key = submitted[0]
    assert len(payload["records"]) == 8
    assert payload["remainingAutoRetryCount"] == 0
    assert idempotency_key.startswith("ors-miaoda-")
    assert {record["id"] for record in payload["records"]} == set(range(1, 9))
    assert len({record["meta"]["source_record_id"] for record in payload["records"]}) == 8
    assert len(
        {
            strip_episode_correlation(record["query"])[1]
            for record in payload["records"]
        }
    ) == 8
    assert all(not output.finished for output in starts)
    assert {
        output.metadata["miaoda_batch_task_id"] for output in starts
    } == {"batch-explicit-eight"}
    assert len({output.metadata["miaoda_task_name"] for output in starts}) == 1
    assert {
        output.metadata["miaoda_optimizer_batch_expected_count"] for output in starts
    } == {8}
    assert {
        output.metadata["miaoda_optimizer_batch_member_index"] for output in starts
    } == set(range(8))
    for task, state in zip(tasks, states, strict=True):
        backend.teardown(task_spec=task, state=state)
    assert backend._batch_runtimes == {}


def test_incomplete_explicit_batch_times_out_without_datawheel_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")
    submitted: list[object] = []

    class Datawheel:
        def submit_batch(self, payload, *, idempotency_key):
            submitted.append((payload, idempotency_key))
            raise AssertionError("an incomplete explicit batch must never be submitted")

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=tmp_path / "captures",
            submission_batch_size=8,
            submission_batch_window_seconds=0.001,
            explicit_batch_wait_timeout_seconds=0.02,
        ),
    )
    task = {
        "episode_id": "only-one-member",
        "query": "query",
        "training_objective": "pure_opd",
        "task_reward_required": False,
        "optimizer_batch_id": "slime-r1-f0-incomplete",
        "optimizer_batch_expected_count": 8,
        "optimizer_batch_member_index": 0,
    }
    state: dict[str, object] = {}
    backend.setup(task_spec=task, secrets={}, state=state)

    async def run():
        with pytest.raises(
            miaoda_online.MiaodaOnlineError,
            match="did not reach its expected count",
        ):
            await backend.execute(
                action="start_rollout", payload={}, task_spec=task, state=state
            )

    asyncio.run(run())
    assert submitted == []
    assert backend._explicit_submission_groups == {}


def test_exact_per_episode_join_preserves_successful_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()

    class Datawheel:
        def get_trajectory(self, app_id):
            assert app_id == "app-1"
            return {
                "messages": [
                    {"role": "user", "content": "same query"},
                    {"role": "assistant", "content": "answer"},
                ]
            }

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=capture_dir,
            submission_batch_size=2,
        ),
    )
    first_capture_id = "ep-00000000000000000000000000000001"
    second_capture_id = "ep-00000000000000000000000000000002"
    members = [
        miaoda_online._BatchMember(
            episode_id=f"episode-{index}",
            task_spec={"episode_id": f"episode-{index}", "query": "query"},
            state={},
            record_id=str(index),
            capture_episode_id=capture_id,
        )
        for index, capture_id in ((1, first_capture_id), (2, second_capture_id))
    ]
    runtime = miaoda_online._BatchRuntime(
        group_id="batch-group",
        batch_task_id="batch-id",
        members=members,
        capture_file=capture_dir / "capture-batch-group.jsonl",
        capture_started_at=0,
        rows=[{"id": "1", "app_id": "app-1"}, {"id": "2", "app_id": "app-2"}],
    )
    (capture_dir / f"capture-{first_capture_id}.jsonl").write_text(
        json.dumps(
            _capture(
                group_id=first_capture_id,
                capture_id="capture-first",
                assistant="answer",
                response_token=21,
            )
        )
        + "\n"
    )
    (capture_dir / f"http-{first_capture_id}.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "mopd-policy-http.v1",
                "captured_at": datetime.now(UTC).isoformat(),
                "episode_id": first_capture_id,
                "status_code": 200,
            }
        )
        + "\n"
    )

    joined = asyncio.run(backend._join_batched_policy_calls(runtime))
    assert set(joined) == {"episode-1"}
    assert joined["episode-1"][0]["response_token_ids"] == [21]
    assert members[0].state["miaoda_policy_http_statuses"] == [200]
    assert members[1].state["miaoda_policy_http_statuses"] == []

    runtime.rows[0]["status"] = "failed"
    assert asyncio.run(backend._join_batched_policy_calls(runtime)) == {}
    runtime.rows[0]["status"] = "completed"

    with (capture_dir / f"capture-{first_capture_id}.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                _capture(
                    group_id=first_capture_id,
                    capture_id="capture-retry",
                    assistant="answer",
                    response_token=21,
                )
            )
            + "\n"
        )
    with (capture_dir / f"http-{first_capture_id}.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "schema_version": "mopd-policy-http.v1",
                    "captured_at": datetime.now(UTC).isoformat(),
                    "episode_id": first_capture_id,
                    "status_code": 200,
                }
            )
            + "\n"
        )

    # A second identical policy call is a separate retry attempt. The latest
    # app trajectory contains only one call, so the combined capture must not
    # be accepted as an exact join.
    assert asyncio.run(backend._join_batched_policy_calls(runtime)) == {}


def test_per_episode_join_accepts_agentos_message_rewrite_with_call_count_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()

    class Datawheel:
        def get_trajectory(self, app_id):
            assert app_id == "app-1"
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": "same query",
                        "response_provider": "agentos-added-field",
                    },
                    {
                        "role": "assistant",
                        "content": "answer",
                        "response_id": "agentos-added-field",
                    },
                ]
            }

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=capture_dir,
            submission_batch_size=1,
        ),
    )
    capture_episode_id = "ep-00000000000000000000000000000001"
    member = miaoda_online._BatchMember(
        episode_id="episode-1",
        task_spec={"episode_id": "episode-1", "query": "query"},
        state={},
        record_id="1",
        capture_episode_id=capture_episode_id,
    )
    runtime = miaoda_online._BatchRuntime(
        group_id="batch-group",
        batch_task_id="batch-id",
        members=[member],
        capture_file=capture_dir / "capture-batch-group.jsonl",
        capture_started_at=0,
        rows=[
            {
                "case_id": 1,
                "generated_app_id": "app-1",
                "status": "completed",
            }
        ],
    )
    (capture_dir / f"capture-{capture_episode_id}.jsonl").write_text(
        json.dumps(
            _capture(
                group_id=capture_episode_id,
                capture_id="capture-first",
                assistant="answer",
                response_token=21,
            )
        )
        + "\n"
    )
    (capture_dir / f"http-{capture_episode_id}.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "mopd-policy-http.v1",
                "captured_at": datetime.now(UTC).isoformat(),
                "episode_id": capture_episode_id,
                "status_code": 200,
            }
        )
        + "\n"
    )

    joined = asyncio.run(backend._join_batched_policy_calls(runtime))

    assert set(joined) == {"episode-1"}
    assert joined["episode-1"][0]["response_token_ids"] == [21]
    assert member.state["miaoda_policy_join_method"] == (
        "record_correlation_call_count"
    )


def test_batched_join_prefers_datawheel_record_api_over_bos_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "runtime-only-model-key")

    class Datawheel:
        def list_batch_records(self, batch_task_id):
            assert batch_task_id == "batch-record-api"
            return [
                {
                    "id": f"record-{index}",
                    "agent_generate_batch_task_id": batch_task_id,
                    "case_id": index,
                    "generated_app_id": f"app-{index}",
                    "generated_conv_id": f"conv-{index}",
                    "status": "completed",
                }
                for index in (1, 2)
            ]

        def download_result(self, *_args, **_kwargs):
            raise AssertionError("record API should avoid the BOS workbook")

    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        _GoblinMustNotRun(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
            policy_capture_dir=capture_dir,
            submission_batch_size=2,
        ),
    )
    members = [
        miaoda_online._BatchMember(
            episode_id=f"episode-{index}",
            task_spec={"episode_id": f"episode-{index}", "query": "query"},
            state={"episode_id": f"episode-{index}"},
            record_id=str(index),
            capture_episode_id=f"ep-{index:032x}",
        )
        for index in (1, 2)
    ]
    runtime = miaoda_online._BatchRuntime(
        group_id="batch-group",
        batch_task_id="batch-record-api",
        members=members,
        capture_file=capture_dir / "capture-batch-group.jsonl",
        capture_started_at=0,
    )

    asyncio.run(backend._prepare_batched_artifact(runtime))

    assert runtime.artifact is None
    assert runtime.rows is not None and len(runtime.rows) == 2
    state = {"episode_id": "episode-2", "miaoda_record_id": "2"}
    miaoda_online._capture_datawheel_ids_from_rows(
        runtime.rows,
        task_spec={"episode_id": "episode-2"},
        state=state,
    )
    assert state["miaoda_app_id"] == "app-2"
    assert state["miaoda_conv_id"] == "conv-2"
    assert state["miaoda_record_uuid"] == "record-2"
