from __future__ import annotations

import httpx
import pytest
from research_ops.agentos import (
    AgentOSClient,
    AgentOSClientError,
    AgentOSConfigurationError,
    AgentOSHTTPError,
    normalize_datawheel_base_url,
)


def test_normalize_datawheel_base_url_accepts_host_and_api_roots() -> None:
    assert normalize_datawheel_base_url("https://agentos.example") == (
        "https://agentos.example/api/v1/datawheel"
    )
    assert normalize_datawheel_base_url("https://agentos.example/api/v1") == (
        "https://agentos.example/api/v1/datawheel"
    )
    assert normalize_datawheel_base_url("https://agentos.example/api/v1/datawheel/") == (
        "https://agentos.example/api/v1/datawheel"
    )


def test_client_requires_token_at_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_AGENTOS_TOKEN", raising=False)
    client = AgentOSClient("https://agentos.example", token_env="TEST_AGENTOS_TOKEN")
    with pytest.raises(AgentOSConfigurationError):
        client.get_batch("batch-1")


def test_client_accepts_sdk_style_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "Bearer sdk-config-token")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": []})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    client.get_batch("batch-1")
    assert seen == ["Bearer sdk-config-token"]


def test_client_submit_status_and_trajectory_use_http_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "not-for-persistence")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/agent/batch/new"):
            return httpx.Response(200, json={"data": {"batch_task_id": "batch-1"}})
        if request.url.path.endswith("/agent/batch/list"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "status": "completed",
                            "status_counts": {"completed": 2, "failed": 1},
                            "total_size": 3,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/agent/record/download"):
            return httpx.Response(
                200,
                json={"url": "https://artifacts.example/result.xlsx?signature=temporary"},
            )
        if request.url.path.endswith("/result.xlsx"):
            return httpx.Response(200, content=b"xlsx-fixture")
        if request.url.path.endswith("/miaoda/parse/coding_messages_all"):
            return httpx.Response(200, json={"app_id": "app-1", "messages": []})
        return httpx.Response(404, json={"error": "not found"})

    client = AgentOSClient(
        "https://agentos.example",
        trajectory_base_url="https://trajectory.example/api/v1/datawheel",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    submitted = client.submit_batch(
        {"taskName": "one", "records": [{"id": "case-1"}]},
        idempotency_key="exec-1",
    )
    status = client.get_batch("batch-1")
    listed = client.list_batches(page=2, page_size=50)
    trajectory = client.get_trajectory("app-1", environment="test")
    result_path = client.download_result("batch-1", tmp_path / "result.xlsx")

    assert submitted.batch_task_id == "batch-1"
    assert status.status == "completed"
    assert status.status_counts == {"completed": 2, "failed": 1}
    assert status.total_size == 3
    assert listed[0]["status"] == "completed"
    assert trajectory["app_id"] == "app-1"
    assert result_path.read_bytes() == b"xlsx-fixture"
    assert calls[0].headers["Idempotency-Key"] == "exec-1"
    assert calls[0].headers["Authorization"] == "Bearer not-for-persistence"
    assert calls[1].url.params["batch_task_id"] == "batch-1"
    assert calls[2].url.params["page"] == "2"
    assert calls[2].url.params["page_size"] == "50"
    assert calls[3].url.params["env"] == "test"
    assert calls[3].url.host == "trajectory.example"
    assert calls[4].url.params["batch_task_id"] == "batch-1"
    assert calls[5].url.host == "artifacts.example"


def test_http_error_exposes_status_without_request_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "super-secret-token")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AgentOSHTTPError) as error:
        client.get_batch("batch-1")
    assert error.value.status_code == 401
    assert "super-secret-token" not in str(error.value)


def test_submit_http_error_redacts_model_key_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "datawheel-token")

    def handler(request: httpx.Request) -> httpx.Response:
        echoed = request.read().decode("utf-8")
        return httpx.Response(400, text=f"invalid request: {echoed}")

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentOSHTTPError) as error:
        client.submit_batch(
            {"metadata": {"codingConfig": {"api_key": "runtime-model-secret"}}}
        )

    assert "runtime-model-secret" not in str(error.value)
    assert "datawheel-token" not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_list_batch_records_uses_exact_foreign_key_filter_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        page = int(request.url.params["page"])
        rows = [
            {
                "id": str(index),
                "agent_generate_batch_task_id": "batch-exact",
                "case_id": index,
            }
            for index in range((page - 1) * 2 + 1, min(page * 2, 3) + 1)
        ]
        return httpx.Response(
            200,
            json={"data": rows, "page": page, "page_size": 2, "total": 3},
        )

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    rows = client.list_batch_records("batch-exact", page_size=2)

    assert [row["case_id"] for row in rows] == [1, 2, 3]
    assert len(calls) == 2
    assert all(
        request.url.params["agent_generate_batch_task_id"] == "batch-exact"
        for request in calls
    )
    assert all("batch_task_id" not in request.url.params for request in calls)


def test_terminate_batch_uses_dedicated_control_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    client.terminate_batch("batch-stop")

    assert calls[0].method == "POST"
    assert calls[0].url.path.endswith("/agent/batch/terminate")
    assert calls[0].read() == b'{"batch_task_id":"batch-stop"}'


def test_resume_batch_uses_dedicated_control_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    client.resume_batch("batch-resume")

    assert calls[0].method == "POST"
    assert calls[0].url.path.endswith("/agent/batch/resume")
    assert calls[0].read() == b'{"batch_task_id":"batch-resume"}'


def test_restart_records_requires_explicit_unique_ids_and_disables_skip_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )
    client.restart_records(["record-1", "record-2"])

    assert calls[0].method == "POST"
    assert calls[0].url.path.endswith("/agent/record/restart")
    assert calls[0].read() == (
        b'{"record_ids":["record-1","record-2"],"skip_check":false}'
    )
    with pytest.raises(ValueError, match="non-empty"):
        client.restart_records([])
    with pytest.raises(ValueError, match="duplicates"):
        client.restart_records(["record-1", "record-1"])


def test_get_dataset_records_uses_named_dataset_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": 1, "state_input": {"query": "one"}}]},
        )

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )

    records = client.get_dataset_records("example-app-300")

    assert records == [{"id": 1, "state_input": {"query": "one"}}]
    assert calls[0].url.path.endswith("/agent/eval/datasets/data")
    assert calls[0].url.params["dataset_name"] == "example-app-300"


def test_get_dataset_records_rejects_partial_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AGENTOS_TOKEN", "runtime-only")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": 1}, "invalid"]})

    client = AgentOSClient(
        "https://agentos.example",
        token_env="TEST_AGENTOS_TOKEN",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AgentOSClientError, match="invalid records"):
        client.get_dataset_records("example-app-300")
