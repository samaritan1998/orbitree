from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from research_ops.agentos.client import AgentOSBatchSnapshot
from research_ops.agentos.entrypoint import AgentOSStreamResult
from research_ops.openreward import (
    MiaodaAgentOSBackend,
    MiaodaDatawheelGoblinBackend,
    MiaodaOnlineConfig,
    build_default_environments,
    create_openreward_app,
)
from research_ops.openreward.adapters import MiaodaEnvironment
from research_ops.openreward.miaoda_online import _policy_url_for_episode
from research_ops.reward.goblin import GoblinJobSnapshot


def _sse_end_payload(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            return json.loads(line[6:])
    raise AssertionError(f"missing ORS end payload: {body!r}")


def test_miaoda_policy_url_adds_the_openai_path_for_a_bare_gateway() -> None:
    assert _policy_url_for_episode("http://policy.test:14255", "episode-1") == (
        "http://policy.test:14255/v1/chat/completions?mopd_episode_id=episode-1"
    )


def test_ors_lifecycle_and_sse_reward_for_all_domain_adapters() -> None:
    environments = build_default_environments(
        task_catalogs={
            "miaoda": {
                "train": [{"query": "demo miaoda", "mock_reward": 0.9}]
            },
            "swe": {
                "train": [
                    {
                        "query": "demo swe",
                        "initial_files": {"app.py": "broken"},
                        "expected_files": {"app.py": "fixed"},
                        "test_command": "pytest -q",
                    }
                ]
            },
            "ppt": {
                "train": [
                    {
                        "query": "demo ppt",
                        "expected_output": "artifact.pptx",
                        "required_text": ["Title", "Conclusion"],
                    }
                ]
            },
            "tooluse": {
                "train": [
                    {
                        "query": "demo tooluse",
                        "initial_state": {"count": 0},
                        "target_state": {"count": 2},
                    }
                ]
            },
        }
    )
    app = create_openreward_app(environments)

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/list_environments").json() == [
            "miaoda",
            "swe",
            "ppt",
            "tooluse",
        ]

        sessions: dict[str, str] = {}
        for name in ("miaoda", "swe", "ppt", "tooluse"):
            assert client.get(f"/{name}/splits").status_code == 200
            assert client.post(f"/{name}/tasks", json={"split": "train"}).json()[
                "tasks"
            ][0]["query"] == f"demo {name}"
            sid = client.post("/create_session").json()["sid"]
            created = client.post(
                "/create",
                headers={"X-Session-ID": sid},
                json={"env_name": name, "split": "train", "index": 0},
            )
            assert created.status_code == 200
            prompt = client.get(f"/{name}/prompt", headers={"X-Session-ID": sid})
            assert prompt.status_code == 200
            assert prompt.json()[0]["type"] == "text"
            if name == "swe":
                swe_prompt = prompt.json()[0]["text"]
                assert "/testbed" in swe_prompt
                assert "edit the repository" in swe_prompt
                assert "call submit" in swe_prompt
                assert swe_prompt.endswith("Task:\ndemo swe")
            if name == "ppt":
                ppt_prompt = prompt.json()[0]["text"]
                assert "/workspace/slides.pptx" in ppt_prompt
                assert "/opt/claude-skills/pptx/SKILL.md" in ppt_prompt
                assert "never scan the filesystem root" in ppt_prompt
                assert "reusable helper functions" in ppt_prompt
                assert "below 6000 policy tokens" in ppt_prompt
                assert "apply all requested edits in one bounded script" in ppt_prompt
                assert "prefer one python-pptx script" in ppt_prompt
                assert "do not echo or replace an entire slide XML" in ppt_prompt
                assert "do not append duplicate slide IDs or relationships" in ppt_prompt
                assert "ppt_read and render" in ppt_prompt
                assert "calling submit_deliverable" in ppt_prompt
                assert ppt_prompt.endswith("Task:\ndemo ppt")
            tools = client.get(f"/{name}/task_tools", headers={"X-Session-ID": sid})
            assert tools.status_code == 200
            assert tools.json()["tools"]
            sessions[name] = sid

        miaoda_start = client.post(
            "/miaoda/call",
            headers={"X-Session-ID": sessions["miaoda"]},
            json={"name": "start_rollout", "input": {}},
        )
        assert _sse_end_payload(miaoda_start.text)["output"]["finished"] is False
        miaoda_wait = client.post(
            "/miaoda/call",
            headers={"X-Session-ID": sessions["miaoda"]},
            json={"name": "poll_rollout", "input": {}},
        )
        assert _sse_end_payload(miaoda_wait.text)["output"]["metadata"]["phase"] == "reward"
        miaoda_done = client.post(
            "/miaoda/call",
            headers={"X-Session-ID": sessions["miaoda"]},
            json={"name": "poll_rollout", "input": {}},
        )
        miaoda_output = _sse_end_payload(miaoda_done.text)["output"]
        assert miaoda_output["finished"] is True
        assert miaoda_output["reward"] == 0.9

        write = client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={
                "name": "write_file",
                "input": {"path": "app.py", "content": "fixed"},
            },
        )
        assert _sse_end_payload(write.text)["output"]["finished"] is False
        tests = client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={"name": "bash", "input": {"command": "pytest -q"}},
        )
        assert _sse_end_payload(tests.text)["output"]["metadata"]["exit_code"] == 0
        swe_done = client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={"name": "submit", "input": {}},
        )
        assert _sse_end_payload(swe_done.text)["output"]["reward"] == 1.0

        client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={
                "name": "ppt_write",
                "input": {
                    "path": "artifact.pptx",
                    "content": "Title\nBody\nConclusion",
                },
            },
        )
        render = client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={"name": "render", "input": {"path": "artifact.pptx"}},
        )
        render_blocks = _sse_end_payload(render.text)["output"]["blocks"]
        assert any(block["type"] == "image" for block in render_blocks)
        ppt_done = client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={
                "name": "submit_deliverable",
                "input": {"path": "artifact.pptx"},
            },
        )
        assert _sse_end_payload(ppt_done.text)["output"]["reward"] == 1.0

        invoke = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sessions["tooluse"]},
            json={
                "name": "invoke_tool",
                "input": {
                    "tool_name": "increment",
                    "arguments": {"key": "count", "delta": 2},
                },
            },
        )
        assert _sse_end_payload(invoke.text)["output"]["blocks"][0]["text"] == "2"
        tooluse_done = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sessions["tooluse"]},
            json={"name": "submit", "input": {}},
        )
        assert _sse_end_payload(tooluse_done.text)["output"]["reward"] == 1.0

        for sid in sessions.values():
            assert client.post("/delete", headers={"X-Session-ID": sid}).status_code == 200


def test_ors_create_session_supports_the_official_resumable_sse_form() -> None:
    app = create_openreward_app(build_default_environments())
    with TestClient(app) as client:
        response = client.post(
            "/create_session",
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200
        assert "event: task_id\ndata: " in response.text
        assert "event: end\n" in response.text


def test_ors_optional_api_key_accepts_official_and_bearer_headers(monkeypatch) -> None:
    monkeypatch.setenv("TEST_OPENREWARD_SERVER_KEY", "runtime-only-key")
    app = create_openreward_app(
        build_default_environments(),
        api_key_env="TEST_OPENREWARD_SERVER_KEY",
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/list_environments").status_code == 401
        assert client.get(
            "/list_environments",
            headers={"X-API-Key": "runtime-only-key"},
        ).status_code == 200
        assert client.get(
            "/list_environments",
            headers={"Authorization": "Bearer runtime-only-key"},
        ).status_code == 200


def test_ors_rejects_unknown_tool_without_leaking_task_data() -> None:
    app = create_openreward_app(build_default_environments())
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        client.post(
            "/create",
            headers={"X-Session-ID": sid},
            json={"env_name": "miaoda", "task_spec": {"query": "private query"}},
        )
        response = client.post(
            "/miaoda/call",
            headers={"X-Session-ID": sid},
            json={"name": "does_not_exist", "input": {}},
        )
        assert response.status_code == 200
        assert "private query" not in response.text
        payload = _sse_end_payload(response.text)
        assert payload == {
            "ok": False,
            "error": "'does_not_exist' is not a valid tool",
            "reason": "not_found",
        }


def test_ors_validates_advertised_tool_schema_without_echoing_input() -> None:
    app = create_openreward_app(build_default_environments())
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        client.post(
            "/create",
            headers={"X-Session-ID": sid},
            json={"env_name": "swe", "task_spec": {"query": "schema check"}},
        )
        missing = client.post(
            "/swe/call",
            headers={"X-Session-ID": sid},
            json={"name": "bash", "input": {}},
        )
        missing_payload = _sse_end_payload(missing.text)
        assert missing_payload["ok"] is False
        assert missing_payload["reason"] == "input_validation"

        reflected_secret = "must-not-be-reflected"
        unexpected = client.post(
            "/swe/call",
            headers={"X-Session-ID": sid},
            json={
                "name": "bash",
                "input": {"command": "pwd", "credential": reflected_secret},
            },
        )
        assert reflected_secret not in unexpected.text
        assert _sse_end_payload(unexpected.text)["reason"] == "input_validation"


def test_default_tooluse_mock_keeps_session_state_isolated() -> None:
    app = create_openreward_app(build_default_environments())
    task = {
        "query": "increment once",
        "initial_state": {"count": 0},
        "target_state": {"count": 1},
    }
    with TestClient(app) as client:
        first = client.post("/create_session").json()["sid"]
        second = client.post("/create_session").json()["sid"]
        for sid in (first, second):
            assert client.post(
                "/create",
                headers={"X-Session-ID": sid},
                json={"env_name": "tooluse", "task_spec": task},
            ).status_code == 200
        client.post(
            "/tooluse/call",
            headers={"X-Session-ID": first},
            json={
                "name": "invoke_tool",
                "input": {
                    "tool_name": "increment",
                    "arguments": {"key": "count"},
                },
            },
        )
        first_result = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": first},
            json={"name": "submit", "input": {}},
        )
        second_result = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": second},
            json={"name": "submit", "input": {}},
        )
        assert _sse_end_payload(first_result.text)["output"]["reward"] == 1.0
        assert _sse_end_payload(second_result.text)["output"]["reward"] == 0.0


def test_ors_delete_and_shutdown_teardown_each_live_session_once() -> None:
    class TrackingBackend:
        def __init__(self) -> None:
            self.setup_calls = 0
            self.teardown_calls = 0

        def setup(self, *, task_spec, secrets, state):
            del task_spec, secrets, state
            self.setup_calls += 1

        def execute(self, *, action, payload, task_spec, state):
            del action, payload, task_spec, state
            return {"text": "ok"}

        def teardown(self, *, task_spec, state):
            del task_spec, state
            self.teardown_calls += 1

    backend = TrackingBackend()
    app = create_openreward_app(
        build_default_environments(backends={"swe": backend})
    )
    with TestClient(app) as client:
        sessions = [client.post("/create_session").json()["sid"] for _ in range(2)]
        for sid in sessions:
            client.post(
                "/create",
                headers={"X-Session-ID": sid},
                json={"env_name": "swe", "task_spec": {"query": "teardown"}},
            )
        client.post("/delete", headers={"X-Session-ID": sessions[0]})
        assert backend.teardown_calls == 1
    assert backend.setup_calls == 2
    assert backend.teardown_calls == 2


def test_ors_replays_a_completed_task_id_without_running_the_backend_twice() -> None:
    app = create_openreward_app(build_default_environments())
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        client.post(
            "/create",
            headers={"X-Session-ID": sid},
            json={"env_name": "tooluse", "task_spec": {"query": "idempotent"}},
        )
        first = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sid},
            json={
                "name": "invoke_tool",
                "input": {"tool_name": "lookup"},
                "task_id": "reconnect-me",
            },
        )
        second = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sid},
            json={
                "name": "invoke_tool",
                "input": {"tool_name": "different"},
                "task_id": "reconnect-me",
            },
        )
        assert _sse_end_payload(first.text) == _sse_end_payload(second.text)


def test_miaoda_agentos_backend_keeps_one_application_for_continuations() -> None:
    class FakeAgentOS:
        def __init__(self) -> None:
            self.created = 0
            self.calls = 0

        def create_application(self):
            self.created += 1
            return SimpleNamespace(app_id="app-1", conversation_id="conv-1", user_id="u-1")

        def _mock_login_token(self):
            return "request-scoped-token"

        def chat_round(self, application, *, token, parts, metadata, include_app_id):
            assert application.app_id == "app-1"
            assert token == "request-scoped-token"
            assert include_app_id is True
            assert parts[0]["kind"] == "text"
            assert metadata["runtime"] == "miaoda"
            self.calls += 1
            return AgentOSStreamResult(
                events=[{"result": {"metadata": {"text": f"obs-{self.calls}"}}}],
                terminal_state="awaiting_user_input" if self.calls == 1 else "finished",
            )

    fake = FakeAgentOS()
    backend = MiaodaAgentOSBackend(fake)  # type: ignore[arg-type]
    environment = MiaodaEnvironment({"query": "build app"}, backend=backend)

    async def run() -> tuple[Any, Any]:
        await environment.setup()
        first = await environment.call_tool("miaoda_step", {"message": "start"})
        second = await environment.call_tool("submit", {"result": "done"})
        await environment.teardown()
        return first, second

    first, second = asyncio.run(run())
    assert fake.created == 1
    assert fake.calls == 2
    assert first.finished is False
    assert second.finished is True
    assert "request-scoped-token" not in first.model_dump_json()
    assert "request-scoped-token" not in second.model_dump_json()


def test_ors_secrets_header_is_session_scoped_and_never_returned() -> None:
    class SecretBackend:
        def setup(self, *, task_spec, secrets, state):
            del task_spec
            state["secret_seen"] = secrets["service_key"]

        def execute(self, *, action, payload, task_spec, state):
            del action, payload, task_spec
            assert state["secret_seen"] == "do-not-echo"
            return {"text": "ok", "finished": True, "reward": 1.0}

        def teardown(self, *, task_spec, state):
            del task_spec, state

    app = create_openreward_app(
        build_default_environments(backends={"tooluse": SecretBackend()})
    )
    envelope = base64.b64encode(
        json.dumps({"service_key": {"value": "do-not-echo"}}).encode()
    ).decode()
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        created = client.post(
            "/create",
            headers={"X-Session-ID": sid, "X-Secrets": envelope},
            json={"env_name": "tooluse", "task_spec": {"query": "secret test"}},
        )
        assert created.status_code == 200
        response = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sid},
            json={"name": "invoke_tool", "input": {"tool_name": "x"}},
        )
        assert "do-not-echo" not in response.text
        assert _sse_end_payload(response.text)["output"]["reward"] == 1.0


def test_fallback_prompt_drops_nested_credential_like_task_fields() -> None:
    app = create_openreward_app(build_default_environments())
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        created = client.post(
            "/create",
            headers={"X-Session-ID": sid},
            json={
                "env_name": "tooluse",
                "task_spec": {
                    "objective": "inspect state",
                    "credentials": {
                        "api_key": "do-not-show",
                        "nested": {"token": "also-do-not-show"},
                    },
                    "safe_metadata": {"case_id": "case-1"},
                },
            },
        )
        assert created.status_code == 200
        prompt = client.get("/tooluse/prompt", headers={"X-Session-ID": sid})
        assert prompt.status_code == 200
        text = prompt.json()[0]["text"]
        assert "do-not-show" not in text
        assert "also-do-not-show" not in text
        assert "case-1" in text


def test_miaoda_datawheel_goblin_state_machine_through_ors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MIAODA_MODEL_KEY", "runtime-model-key")
    reward_file = tmp_path / "goblin.jsonl"
    reward_file.write_text('{"episode_id":"ep-1","reward":0.91}\n', encoding="utf-8")

    class Datawheel:
        def __init__(self) -> None:
            self.statuses = iter(("running", "completed"))

        def submit_batch(self, payload, *, idempotency_key):
            assert payload["records"][0]["query"] == "make app"
            assert idempotency_key.startswith("ors-miaoda-")
            assert "ep-1" not in idempotency_key
            return AgentOSBatchSnapshot(batch_task_id="dw-1", status="submitted")

        def get_batch(self, batch_task_id):
            return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status=next(self.statuses))

        def download_result(self, batch_task_id, destination: Path):
            destination.write_bytes(b"artifact")
            return destination

    class Goblin:
        def __init__(self) -> None:
            self.statuses = iter(("running", "completed"))

        def submit_file(self, path, **kwargs):
            assert path.exists()
            assert kwargs["rollout_model"] == "policy-r1"
            return GoblinJobSnapshot(job_id="gb-1", status="pending")

        def get_job(self, job_id):
            return GoblinJobSnapshot(
                job_id=job_id,
                status=next(self.statuses),
                result_file=str(reward_file),
            )

    backend = MiaodaDatawheelGoblinBackend(
        Datawheel(),
        Goblin(),
        MiaodaOnlineConfig(
            generated_user_id="generated-user",
            model_name="policy-r1",
            model_api_url="http://gateway/v1/chat/completions",
            model_api_key_env="TEST_MIAODA_MODEL_KEY",
            artifact_dir=tmp_path / "artifacts",
        ),
    )
    app = create_openreward_app(
        build_default_environments(backends={"miaoda": backend})
    )
    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        assert client.post(
            "/create",
            headers={"X-Session-ID": sid},
            json={
                "env_name": "miaoda",
                "task_spec": {"episode_id": "ep-1", "query": "make app"},
            },
        ).status_code == 200
        start = client.post(
            "/miaoda/call",
            headers={"X-Session-ID": sid},
            json={"name": "start_rollout", "input": {}},
        )
        assert _sse_end_payload(start.text)["output"]["finished"] is False
        outputs = []
        for _ in range(4):
            response = client.post(
                "/miaoda/call",
                headers={"X-Session-ID": sid},
                json={"name": "poll_rollout", "input": {}},
            )
            outputs.append(_sse_end_payload(response.text)["output"])
        assert outputs[-1]["finished"] is True
        assert outputs[-1]["reward"] == 0.91
        assert outputs[-1]["metadata"]["usable"] is True
