from __future__ import annotations

import json

import httpx
import pytest
from research_ops.agentos import (
    AgentOSConfigurationError,
    AgentOSEntrypointClient,
)


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def test_entrypoint_runs_two_phase_application_and_reads_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_WORKSPACE_JWT", "workspace-secret")
    calls: list[httpx.Request] = []
    a2a_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/innerapi/v1/conversation/empty_create":
            assert request.url.params["whitApp"] == "true"
            assert request.headers["X-Authorization"].startswith("Bearer ")
            assert "workspace-secret" not in request.headers["X-Authorization"]
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "createdBy": "user-online",
                        "appId": "app-online",
                        "conversationId": "conv-online",
                    },
                },
            )
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={"url": "https://entrypoint.example/a2a"})
        if request.url.path == "/a2a":
            a2a_requests.append(json.loads(request.content))
            headers = request.headers
            assert headers["X-Miaoda-App-Id"] == "app-online"
            assert headers["X-Miaoda-Session-Id"] == "conv-online"
            state = "awaiting_user_input" if len(a2a_requests) == 1 else "finished"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse({"result": {"metadata": {"agent_state": state}}}),
            )
        if request.url.path.endswith("/bduss/login/mock"):
            return httpx.Response(200, json={"status": 0, "data": {"token": "mock-token"}})
        if request.url.path == "/api/v1/conversation/trajectory":
            assert request.headers["Authorization"] == "Bearer mock-token"
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse({"result": {"status": {"state": "completed"}}}),
            )
        return httpx.Response(404, json={"error": "not found"})

    client = AgentOSEntrypointClient(
        workspace_base_url="https://workspace.example",
        entrypoint_base_url="https://entrypoint.example",
        user_id="user-online",
        jwt_secret_env="TEST_WORKSPACE_JWT",
        transport=httpx.MockTransport(handler),
    )
    result = client.first_coding_rollout(
        "Build a task tracker",
        coding_config={
            "codingPromptiModelConfig": {
                "provider": "qianfancoder_adapter_v4",
                "api_key": "request-only-model-key",
            }
        },
        skill_ids=["skill-demo"],
    )
    trajectory = client.get_conversation_trajectory(result.application.app_id)

    assert result.application.app_id == "app-online"
    assert result.prd.terminal_state == "awaiting_user_input"
    assert result.coding.terminal_state == "finished"
    assert trajectory.terminal_state == "completed"
    assert len(a2a_requests) == 2
    first_metadata = a2a_requests[0]["params"]["message"]["metadata"]
    second_metadata = a2a_requests[1]["params"]["message"]["metadata"]
    assert a2a_requests[0]["params"]["message"]["parts"][0]["metadata"] == {}
    assert first_metadata["skillList"] == ["skill-demo"]
    assert first_metadata["codingConfig"]["codingPromptiModelConfig"]["api_key"] == (
        "request-only-model-key"
    )
    assert second_metadata["userConfirmation"] == {"type": "generateApp"}
    assert any(request.url.path == "/api/v1/conversation/trajectory" for request in calls)


def test_entrypoint_requires_workspace_secret_at_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_WORKSPACE_JWT", raising=False)
    client = AgentOSEntrypointClient(
        workspace_base_url="https://workspace.example",
        entrypoint_base_url="https://entrypoint.example",
        user_id="user-online",
        jwt_secret_env="TEST_WORKSPACE_JWT",
    )

    with pytest.raises(AgentOSConfigurationError):
        client.create_application()


def test_entrypoint_omits_skill_filter_when_no_skills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_WORKSPACE_JWT", "workspace-secret")
    a2a_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/innerapi/v1/conversation/empty_create":
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "createdBy": "user-online",
                        "appId": "app-online",
                        "conversationId": "conv-online",
                    },
                },
            )
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={"url": "https://entrypoint.example/a2a"})
        if request.url.path == "/a2a":
            a2a_requests.append(json.loads(request.content))
            state = "awaiting_user_input" if len(a2a_requests) == 1 else "finished"
            return httpx.Response(
                200,
                content=_sse({"result": {"metadata": {"agent_state": state}}}),
            )
        return httpx.Response(404)

    client = AgentOSEntrypointClient(
        workspace_base_url="https://workspace.example",
        entrypoint_base_url="https://entrypoint.example",
        user_id="user-online",
        jwt_secret_env="TEST_WORKSPACE_JWT",
        transport=httpx.MockTransport(handler),
    )
    client.first_coding_rollout("Build an app", coding_config={})

    assert "skillList" not in a2a_requests[0]["params"]["message"]["metadata"]
    assert "skillList" not in a2a_requests[1]["params"]["message"]["metadata"]


def test_entrypoint_replay_url_uses_exact_replay_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_WORKSPACE_JWT", "workspace-secret")
    a2a_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/innerapi/v1/conversation/empty_create":
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "createdBy": "user-online",
                        "appId": "app-online",
                        "conversationId": "conv-online",
                    },
                },
            )
        if request.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={"url": "https://entrypoint.example/a2a"})
        if request.url.path == "/a2a":
            a2a_requests.append(json.loads(request.content))
            state = "awaiting_user_input" if len(a2a_requests) == 1 else "finished"
            return httpx.Response(
                200,
                content=_sse({"result": {"metadata": {"agent_state": state}}}),
            )
        return httpx.Response(404)

    client = AgentOSEntrypointClient(
        workspace_base_url="https://workspace.example",
        entrypoint_base_url="https://entrypoint.example",
        user_id="user-online",
        jwt_secret_env="TEST_WORKSPACE_JWT",
        transport=httpx.MockTransport(handler),
    )
    client.first_coding_rollout(
        "Build an app",
        coding_config={},
        prd="Fixed PRD",
        replay_url="https://storage.example/piece.json?authorization=secret",
    )

    state_input = a2a_requests[0]["params"]["message"]["metadata"]["StateInput"]
    assert state_input == {
        "query": "Build an app",
        "replay_url": "https://storage.example/piece.json?authorization=secret",
    }


def test_entrypoint_runs_raw_query_end_to_end_over_workspace_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_WORKSPACE_JWT", "workspace-secret")
    chat_requests: list[dict] = []
    trajectory_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal trajectory_calls
        if request.url.path == "/innerapi/v1/conversation/empty_create":
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "createdBy": "user-online",
                        "appId": "app-online",
                        "conversationId": "conv-online",
                    },
                },
            )
        if request.url.path.endswith("/bduss/login/mock"):
            return httpx.Response(200, json={"status": 0, "data": {"token": "mock-token"}})
        if request.url.path == "/api/v1/conversation/chat":
            assert request.headers["Authorization"] == "Bearer mock-token"
            chat_requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "result": {
                        "status": {
                            "state": "submitted",
                            "message": {"metadata": {"appId": "app-online"}},
                        }
                    }
                },
            )
        if request.url.path == "/api/v1/conversation/trajectory":
            trajectory_calls += 1
            state = "awaiting_user_input" if trajectory_calls == 1 else "finished"
            return httpx.Response(
                200,
                content=_sse(
                    {
                        "result": {
                            "final": True,
                            "metadata": {
                                "observation": "agent_state_changed",
                                "agent_state": state,
                            },
                        }
                    }
                ),
            )
        return httpx.Response(404)

    client = AgentOSEntrypointClient(
        workspace_base_url="https://workspace.example",
        entrypoint_base_url="https://entrypoint.example",
        user_id="user-online",
        jwt_secret_env="TEST_WORKSPACE_JWT",
        transport=httpx.MockTransport(handler),
    )
    result = client.end_to_end_rollout(
        "Build a courier mini program",
        coding_config={
            "codingPromptiModelConfig": {
                "provider": "qianfancoder_adapter_v4",
                "api_key": "request-only-model-key",
            }
        },
    )

    assert result.requirement.terminal_state == "awaiting_user_input"
    assert result.coding.terminal_state == "finished"
    assert len(chat_requests) == 2
    first_params = chat_requests[0]["params"]
    second_params = chat_requests[1]["params"]
    assert first_params["message"]["parts"] == [
        {"kind": "text", "text": "Build a courier mini program"}
    ]
    assert "codingConfig" not in first_params["metadata"]
    assert "appId" not in first_params["metadata"]
    assert second_params["metadata"]["appId"] == "app-online"
    assert second_params["metadata"]["userConfirmation"] == {"type": "generateApp"}
    assert second_params["metadata"]["codingConfig"]["codingPromptiModelConfig"][
        "provider"
    ] == "qianfancoder_adapter_v4"
