from __future__ import annotations

import httpx
from research_ops.agentos import AgentOSClient
from test_api_workflow import create_work


def test_agentos_batch_is_durable_and_pollable(client, monkeypatch) -> None:
    work, branch = create_work(client, "HARN")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/agent/batch/new"):
            return httpx.Response(200, json={"id": "batch-online-1", "status": "pending"})
        if request.url.path.endswith("/agent/batch/list"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "status": "completed",
                            "status_counts": {"completed": 1},
                            "total_size": 1,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/miaoda/parse/coding_messages_all"):
            return httpx.Response(200, json={"app_id": "app-online-1", "events": []})
        return httpx.Response(404, json={})

    monkeypatch.setenv("DATAWHEEL_TOKEN", "test-token")
    state = client.app.state.rops
    state.settings.agentos_enabled = True
    state.settings.agentos_job_sync_enabled = False
    state.agentos_jobs.client = AgentOSClient(
        "https://agentos.example",
        transport=httpx.MockTransport(handler),
    )

    payload = {
        "taskName": "online-mopd-smoke",
        "env": {"value": "evaluation"},
        "concurrency": 1,
        "selectedPipeline": "AgenticFirstCodingPipeline",
        "metadata": {
            "codingConfig": {
                "codingPromptiModelConfig": {
                    "provider": "qianfancoder_adapter_v4",
                    "api_key": "must-not-be-persisted",
                }
            }
        },
        "records": [{"id": "case-1", "state_input": {"query": "synthetic"}}],
    }
    response = client.post(
        "/api/agentos/batches",
        json={
            "work_item_id": work["id"],
            "idempotency_key": "mopd-smoke-1",
            "payload": payload,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["external_job"]["remote_job_id"] == "batch-online-1"
    snapshot = body["external_job"]["metadata_json"]["payload_snapshot"]
    assert snapshot["record_count"] == 1
    assert "synthetic" not in str(snapshot)
    assert "must-not-be-persisted" not in str(snapshot)
    assert body["execution"]["status"] == "pending"

    repeated = client.post(
        "/api/agentos/batches",
        json={
            "work_item_id": work["id"],
            "idempotency_key": "mopd-smoke-1",
            "payload": payload,
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["external_job"]["id"] == body["external_job"]["id"]

    conflicting = client.post(
        "/api/agentos/batches",
        json={
            "work_item_id": work["id"],
            "idempotency_key": "mopd-smoke-1",
            "payload": {**payload, "taskName": "different"},
        },
    )
    assert conflicting.status_code == 409, conflicting.text

    synced = client.post("/api/agentos/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json() == {"updated": 1}

    status = client.get("/api/agentos/batches/batch-online-1")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "completed"
    assert status.json()["status_counts"] == {"completed": 1}
    assert client.get(f"/api/branches/{branch['id']}").json()["work_item"]["flow"] == "actionable"

    trajectory = client.get("/api/agentos/trajectories/app-online-1?environment=evaluation")
    assert trajectory.status_code == 200, trajectory.text
    assert trajectory.json()["app_id"] == "app-online-1"
    assert any(request.url.path.endswith("/agent/batch/new") for request in calls)
