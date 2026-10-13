from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from research_ops.openreward import (
    OpenRewardRuntimeError,
    PptOfficeBackend,
    RemoteORSBackend,
    RemoteORSConfig,
    RemoteServiceConfig,
    SweSandboxBackend,
    ToolRegistryBackend,
    build_backends_from_contract,
    build_default_environments,
    create_openreward_app,
    load_backend_contract,
    load_task_catalogs,
    load_task_catalogs_many,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_CONTRACT = PROJECT_ROOT / "config" / "openreward-backends.example.yaml"
TASK_CATALOG = PROJECT_ROOT / "config" / "openreward-tasks.example.yaml"


def _sse_output(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            payload = json.loads(line[6:])
            assert payload["ok"] is True
            return dict(payload["output"])
    raise AssertionError("missing ORS SSE output")


def test_remote_service_bridges_all_three_runtime_domains_through_ors() -> None:
    requests: list[tuple[str, str]] = []
    runtime_secret = "runtime-secret-must-not-escape"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {runtime_secret}"
        requests.append((request.method, request.url.path))
        path = request.url.path
        if path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(200, json={"sandbox_id": "swe-1"})
        if path == "/v1/sandboxes/swe-1/files/write":
            return httpx.Response(200, json={"output": {"text": "written"}})
        if path == "/v1/sandboxes/swe-1/exec":
            return httpx.Response(
                200,
                json={
                    "output": {
                        "text": f"tests passed; {runtime_secret}",
                        "metadata": {"exit_code": 0, "token": runtime_secret},
                    }
                },
            )
        if path == "/v1/sandboxes/swe-1/verify":
            return httpx.Response(200, json={"reward": 1.0, "text": "verified"})
        if path == "/v1/sandboxes/swe-1" and request.method == "DELETE":
            return httpx.Response(204)

        if path == "/v1/presentations" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "ppt-1"})
        if path == "/v1/presentations/write":
            return httpx.Response(200, json={"text": "deck updated"})
        if path == "/v1/presentations/render":
            return httpx.Response(
                200,
                json={
                    "images": [
                        {
                            "data": "iVBORw0KGgo=",
                            "mimeType": "image/png",
                            "type": "image",
                        }
                    ]
                },
            )
        if path == "/v1/presentations/verify":
            return httpx.Response(
                200,
                json={"output": {"blocks": [{"type": "text", "text": "ok"}], "reward": 0.8}},
            )
        if path == "/v1/presentations/ppt-1" and request.method == "DELETE":
            return httpx.Response(204)

        if path == "/v1/tasks/tool-1/sessions" and request.method == "POST":
            return httpx.Response(200, json={"session_id": "tool-session-1"})
        if path == "/v1/tasks/tool-1/tools":
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "increment",
                            "description": "increment state",
                            "input_schema": {
                                "type": "object",
                                "properties": {"delta": {"type": "integer"}},
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            )
        if path == "/v1/sessions/tool-session-1/tools/increment":
            return httpx.Response(200, json={"text": "2"})
        if path == "/v1/sessions/tool-session-1/state":
            return httpx.Response(200, json={"text": '{"count":2}'})
        if path == "/v1/sessions/tool-session-1/verify":
            return httpx.Response(200, json={"reward": 1.0, "text": "target reached"})
        if path == "/v1/sessions/tool-session-1" and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    environ = {
        "SWE_URL": "http://runtime.test",
        "PPT_URL": "http://runtime.test",
        "TOOL_URL": "http://runtime.test",
        "RUNTIME_TOKEN": runtime_secret,
    }
    auth = {"Authorization": "RUNTIME_TOKEN"}
    swe = SweSandboxBackend(
        RemoteServiceConfig(
            environment="swe",
            base_url_env="SWE_URL",
            auth_header_envs=auth,
            create_operation="POST /v1/sandboxes",
            action_operations={
                "bash": "POST /v1/sandboxes/{sandbox_id}/exec",
                "read_file": "POST /v1/sandboxes/{sandbox_id}/files/read",
                "write_file": "POST /v1/sandboxes/{sandbox_id}/files/write",
                "submit": "POST /v1/sandboxes/{sandbox_id}/verify",
            },
            destroy_operation="DELETE /v1/sandboxes/{sandbox_id}",
            terminal_actions=frozenset({"submit"}),
        ),
        environ=environ,
        transport=transport,
    )
    ppt = PptOfficeBackend(
        RemoteServiceConfig(
            environment="ppt",
            base_url_env="PPT_URL",
            auth_header_envs=auth,
            create_operation="POST /v1/presentations",
            action_operations={
                "ppt_read": "POST /v1/presentations/read",
                "ppt_write": "POST /v1/presentations/write",
                "render": "POST /v1/presentations/render",
                "submit_deliverable": "POST /v1/presentations/verify",
            },
            destroy_operation="DELETE /v1/presentations/{session_id}",
            terminal_actions=frozenset({"submit_deliverable"}),
        ),
        environ=environ,
        transport=transport,
    )
    tooluse = ToolRegistryBackend(
        RemoteServiceConfig(
            environment="tooluse",
            base_url_env="TOOL_URL",
            auth_header_envs=auth,
            create_operation="POST /v1/tasks/{task_id}/sessions",
            tools_operation="GET /v1/tasks/{task_id}/tools",
            action_operations={
                "invoke_tool": "POST /v1/sessions/{session_id}/tools/{tool_name}",
                "get_state": "GET /v1/sessions/{session_id}/state",
                "submit": "POST /v1/sessions/{session_id}/verify",
            },
            dynamic_action_operation="POST /v1/sessions/{session_id}/tools/{tool_name}",
            destroy_operation="DELETE /v1/sessions/{session_id}",
            terminal_actions=frozenset({"submit"}),
        ),
        environ=environ,
        transport=transport,
    )
    app = create_openreward_app(
        build_default_environments(
            backends={"swe": swe, "ppt": ppt, "tooluse": tooluse}
        )
    )

    with TestClient(app) as client:
        sessions: dict[str, str] = {}
        for env, task in (
            ("swe", {"task_id": "swe-1", "query": "fix"}),
            ("ppt", {"task_id": "ppt-1", "query": "deck"}),
            ("tooluse", {"task_id": "tool-1", "query": "increment"}),
        ):
            sid = client.post("/create_session").json()["sid"]
            response = client.post(
                "/create",
                headers={"X-Session-ID": sid},
                json={"env_name": env, "task_spec": task},
            )
            assert response.status_code == 200
            sessions[env] = sid

        client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={"name": "write_file", "input": {"path": "x.py", "content": "ok"}},
        )
        swe_run = client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={"name": "bash", "input": {"command": "pytest -q"}},
        )
        assert runtime_secret not in swe_run.text
        assert _sse_output(swe_run)["metadata"] == {"exit_code": 0, "environment": "swe", "turn": 2}
        swe_done = client.post(
            "/swe/call",
            headers={"X-Session-ID": sessions["swe"]},
            json={"name": "submit", "input": {}},
        )
        assert _sse_output(swe_done)["finished"] is True

        client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={"name": "ppt_write", "input": {"path": "deck.pptx", "content": "x"}},
        )
        render = client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={"name": "render", "input": {"path": "deck.pptx"}},
        )
        assert _sse_output(render)["blocks"][0]["type"] == "image"
        ppt_done = client.post(
            "/ppt/call",
            headers={"X-Session-ID": sessions["ppt"]},
            json={"name": "submit_deliverable", "input": {"path": "deck.pptx"}},
        )
        assert _sse_output(ppt_done)["reward"] == 0.8

        tools = client.get(
            "/tooluse/task_tools",
            headers={"X-Session-ID": sessions["tooluse"]},
        ).json()["tools"]
        assert "increment" in {item["name"] for item in tools}
        invoked = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sessions["tooluse"]},
            json={"name": "increment", "input": {"delta": 2}},
        )
        assert _sse_output(invoked)["blocks"][0]["text"] == "2"
        state = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sessions["tooluse"]},
            json={"name": "get_state", "input": {}},
        )
        assert "count" in _sse_output(state)["blocks"][0]["text"]
        tool_done = client.post(
            "/tooluse/call",
            headers={"X-Session-ID": sessions["tooluse"]},
            json={"name": "submit", "input": {}},
        )
        assert _sse_output(tool_done)["reward"] == 1.0

        for sid in sessions.values():
            assert client.post("/delete", headers={"X-Session-ID": sid}).status_code == 200

    assert ("DELETE", "/v1/sandboxes/swe-1") in requests
    assert ("DELETE", "/v1/presentations/ppt-1") in requests
    assert ("DELETE", "/v1/sessions/tool-session-1") in requests


def test_remote_ors_proxy_forwards_toolathlon_prompt_tools_reward_and_teardown() -> None:
    requests: list[tuple[str, str, str | None]] = []
    remote_secret = "toolathlon-runtime-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {remote_secret}"
        sid = request.headers.get("x-session-id")
        requests.append((request.method, request.url.path, sid))
        if request.url.path == "/create":
            body = json.loads(request.content)
            assert body == {
                "env_name": "toolathlongym",
                "task_spec": {
                    "task_id": "howtocook-meal-plan-gcal",
                    "task_name": "howtocook-meal-plan-gcal",
                    "query": "prepare a weekly meal plan",
                },
            }
            assert sid
            return httpx.Response(200, json={"sid": sid})
        if request.url.path == "/toolathlongym/prompt":
            return httpx.Response(
                200,
                json=[{"type": "text", "text": "official Toolathlon task prompt"}],
            )
        if request.url.path == "/toolathlongym/task_tools":
            return httpx.Response(
                200,
                json={
                    "tools": [
                        {
                            "name": "get_tool_details",
                            "description": "inspect one MCP schema",
                            "input_schema": {"type": "object"},
                        },
                        {
                            "name": "call_tool",
                            "description": "invoke one MCP tool",
                            "input_schema": {"type": "object"},
                        },
                        {
                            "name": "claim_done",
                            "description": "run the official evaluator",
                            "input_schema": None,
                        },
                    ]
                },
            )
        if request.url.path == "/toolathlongym/call":
            body = json.loads(request.content)
            assert "task_id" not in body
            if body["name"] == "claim_done":
                output = {
                    "ok": True,
                    "output": {
                        "blocks": [{"type": "text", "text": "PASS"}],
                        "reward": 1.0,
                        "finished": True,
                        "metadata": {"verifier": "official", "token": remote_secret},
                    },
                }
            else:
                assert body["name"] == "call_tool"
                assert body["input"]["name"] == "howtocook.search_recipes"
                output = {
                    "ok": True,
                    "output": {
                        "blocks": [{"type": "text", "text": "recipe results"}],
                        "finished": False,
                    },
                }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="event: end\ndata: " + json.dumps(output) + "\n\n",
            )
        if request.url.path == "/delete":
            return httpx.Response(200, json={"sid": sid})
        return httpx.Response(404)

    backend = RemoteORSBackend(
        RemoteORSConfig(
            environment="tooluse",
            remote_environment="toolathlongym",
            base_url_env="TOOLATHLON_URL",
            auth_header_envs={"Authorization": "TOOLATHLON_TOKEN"},
        ),
        environ={
            "TOOLATHLON_URL": "http://toolathlon.test",
            "TOOLATHLON_TOKEN": remote_secret,
        },
        transport=httpx.MockTransport(handler),
    )
    app = create_openreward_app(
        build_default_environments(backends={"tooluse": backend})
    )

    with TestClient(app) as client:
        sid = client.post("/create_session").json()["sid"]
        headers = {"X-Session-ID": sid}
        created = client.post(
            "/create",
            headers=headers,
            json={
                "env_name": "tooluse",
                "task_spec": {
                    "task_id": "howtocook-meal-plan-gcal",
                    "task_name": "howtocook-meal-plan-gcal",
                    "query": "prepare a weekly meal plan",
                },
            },
        )
        assert created.status_code == 200
        assert client.get("/tooluse/prompt", headers=headers).json() == [
            {"type": "text", "text": "official Toolathlon task prompt", "detail": None}
        ]
        tools = client.get("/tooluse/task_tools", headers=headers).json()["tools"]
        assert {item["name"] for item in tools} == {
            "get_tool_details",
            "call_tool",
            "claim_done",
        }
        invoked = client.post(
            "/tooluse/call",
            headers=headers,
            json={
                "name": "call_tool",
                "input": {
                    "name": "howtocook.search_recipes",
                    "arguments": {"query": "breakfast"},
                },
            },
        )
        assert _sse_output(invoked)["blocks"][0]["text"] == "recipe results"
        done = client.post(
            "/tooluse/call",
            headers=headers,
            json={"name": "claim_done", "input": {}},
        )
        terminal = _sse_output(done)
        assert terminal["reward"] == 1.0
        assert terminal["finished"] is True
        assert terminal["metadata"]["runtime"] == "openreward-proxy"
        assert terminal["metadata"]["remote_environment"] == "toolathlongym"
        assert terminal["metadata"]["reward_source"] == "remote-ors"
        assert remote_secret not in done.text
        assert client.post("/delete", headers=headers).status_code == 200

    paths = [(method, path) for method, path, _sid in requests]
    assert paths == [
        ("POST", "/create"),
        ("GET", "/toolathlongym/prompt"),
        ("GET", "/toolathlongym/task_tools"),
        ("POST", "/toolathlongym/call"),
        ("POST", "/toolathlongym/call"),
        ("POST", "/delete"),
    ]
    remote_session_ids = {sid for _method, _path, sid in requests if sid}
    assert len(remote_session_ids) == 1


def test_contract_assembles_all_four_real_backend_types(tmp_path: Path) -> None:
    contract = load_backend_contract(BACKEND_CONTRACT)
    values = {
        "ROPS_AGENTOS_DATAWHEEL_BASE_URL": "http://datawheel.test/api/v1/datawheel",
        "DATAWHEEL_TOKEN": "dw-secret",
        "DATAWHEEL_GENERATED_USER_ID": "generated-user",
        "MOPD_POLICY_MODEL_NAME": "policy-r1",
        "MOPD_POLICY_API_URL": "http://policy.test/v1/chat/completions",
        "AGENTOS_MODEL_API_KEY": "model-secret",
        "ROPS_GOBLIN_BASE_URL": "http://goblin.test",
        "GOBLIN_REWARD_API_TOKEN": "goblin-secret",
        "GOBLIN_REWARD_RESULT_ROOT": str(tmp_path),
        "ORS_SWE_SANDBOX_URL": "http://swe.test",
        "ORS_SWE_SANDBOX_TOKEN": "swe-secret",
        "ORS_PPT_RUNTIME_URL": "http://ppt.test",
        "ORS_PPT_RUNTIME_TOKEN": "ppt-secret",
        "ORS_TOOLUSE_SERVICE_URL": "http://tool.test",
        "ORS_TOOLUSE_SERVICE_TOKEN": "tool-secret",
    }
    backends = build_backends_from_contract(contract, environ=values)
    assert set(backends) == {"miaoda", "swe", "ppt", "tooluse"}
    assert isinstance(backends["swe"], SweSandboxBackend)
    assert isinstance(backends["ppt"], PptOfficeBackend)
    assert isinstance(backends["tooluse"], RemoteORSBackend)

    miaoda_names = {
        "ROPS_AGENTOS_DATAWHEEL_BASE_URL",
        "DATAWHEEL_TOKEN",
        "DATAWHEEL_GENERATED_USER_ID",
        "MOPD_POLICY_MODEL_NAME",
        "MOPD_POLICY_API_URL",
        "AGENTOS_MODEL_API_KEY",
        "ROPS_GOBLIN_BASE_URL",
        "GOBLIN_REWARD_API_TOKEN",
        "GOBLIN_REWARD_RESULT_ROOT",
    }
    only_miaoda = build_backends_from_contract(
        contract,
        domains=["miaoda"],
        environ={key: value for key, value in values.items() if key in miaoda_names},
    )
    assert set(only_miaoda) == {"miaoda"}

    with pytest.raises(OpenRewardRuntimeError) as caught:
        build_backends_from_contract(contract, environ={})
    message = str(caught.value)
    assert "DATAWHEEL_TOKEN" in message
    assert "dw-secret" not in message


def test_task_catalog_loader_covers_four_domains_and_rejects_secret_fields(
    tmp_path: Path,
) -> None:
    catalogs = load_task_catalogs(TASK_CATALOG)
    assert set(catalogs) == {"miaoda", "swe", "ppt", "tooluse"}
    assert all(len(catalogs[name]["train"]) == 1 for name in catalogs)

    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text(
        "version: 1\nenvironments:\n  swe:\n    train:\n"
        "      - query: fix\n        api_key: do-not-store\n",
        encoding="utf-8",
    )
    with pytest.raises(OpenRewardRuntimeError):
        load_task_catalogs(unsafe)


def test_multiple_task_catalogs_merge_domains_and_reject_duplicate_ids(
    tmp_path: Path,
) -> None:
    swe = tmp_path / "swe.json"
    swe.write_text(
        json.dumps(
            {
                "version": 1,
                "environments": {
                    "swe": {"train": [{"task_id": "swe-1", "query": "fix"}]}
                },
            }
        ),
        encoding="utf-8",
    )
    tooluse = tmp_path / "tooluse.json"
    tooluse.write_text(
        json.dumps(
            {
                "version": 1,
                "environments": {
                    "tooluse": {
                        "train": [{"task_id": "tool-1", "query": "use tool"}]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    catalogs = load_task_catalogs_many([swe, tooluse])
    assert tuple(catalogs) == ("swe", "tooluse")
    assert catalogs["swe"]["train"][0]["task_id"] == "swe-1"
    assert catalogs["tooluse"]["train"][0]["task_id"] == "tool-1"

    with pytest.raises(OpenRewardRuntimeError, match="duplicate task_id"):
        load_task_catalogs_many([swe, swe])


def test_dynamic_remote_tool_schema_validates_before_http_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/sessions"):
            return httpx.Response(
                200,
                json={
                    "session_id": "s-1",
                    "tools": [
                        {
                            "name": "strict_tool",
                            "description": "strict",
                            "input_schema": {
                                "type": "object",
                                "properties": {"value": {"type": "integer"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        }
                    ],
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"text": "ok"})

    backend = ToolRegistryBackend(
        RemoteServiceConfig(
            environment="tooluse",
            base_url_env="URL",
            create_operation="POST /v1/tasks/{task_id}/sessions",
            action_operations={"submit": "POST /v1/sessions/{session_id}/verify"},
            dynamic_action_operation="POST /v1/sessions/{session_id}/tools/{tool_name}",
            destroy_operation="DELETE /v1/sessions/{session_id}",
        ),
        environ={"URL": "http://runtime.test"},
        transport=httpx.MockTransport(handler),
    )

    async def run() -> None:
        environment_cls = build_default_environments(backends={"tooluse": backend})[
            "tooluse"
        ]
        environment = environment_cls({"task_id": "task-1", "query": "strict"}, {})
        await environment.setup()
        before = calls
        with pytest.raises(ValueError):
            await environment.call_tool("strict_tool", {"value": "not-an-int"})
        assert calls == before
        await environment.teardown()

    asyncio.run(run())
