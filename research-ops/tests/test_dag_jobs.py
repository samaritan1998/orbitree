from __future__ import annotations

import json

from test_api_workflow import create_work


def test_registered_dag_mirrors_only_materialized_nodes(client, tmp_path) -> None:
    work, _branch = create_work(client)
    definition_path = tmp_path / "dag.json"
    state_path = tmp_path / "dag.state.json"
    definition_path.write_text(
        json.dumps(
            {
                "dag_name": "ppt-e2e",
                "cluster": "cluster-a",
                "jobs": {
                    "train-ppt": {"cluster": "cluster-a", "config": "/tmp/train.yaml"},
                    "convert-ppt": {"cluster": "cluster-d", "config": "/tmp/convert.yaml"},
                    "deploy-ppt": {"executor": "local", "config": "/tmp/deploy.yaml"},
                },
            }
        ),
        encoding="utf-8",
    )
    state = {
        "dag_name": "ppt-e2e",
        "status": "running",
        "started_at": "2026-07-15T08:00:00Z",
        "jobs": {
            "train-ppt": {
                "status": "completed",
                "job_id": "train-ppt-a1",
                "executor": "ltp",
                "last_remote_status": "Succeeded",
            },
            "convert-ppt": {
                "status": "running",
                "job_id": "convert-ppt-b2",
                "executor": "ltp",
                "last_remote_status": "Running",
            },
            "deploy-ppt": {"status": "pending", "job_id": None},
        },
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    created = client.post(
        "/api/external-jobs",
        json={
            "work_item_id": work["id"],
            "provider": "dag-runner",
            "remote_job_id": "ppt-e2e",
            "status": "running",
            "metadata_json": {
                "dag_path": str(definition_path),
                "state_path": str(state_path),
            },
        },
    )
    assert created.status_code == 200, created.text

    client.app.state.rops.dag_jobs.sync_all()
    detail = client.get(f"/api/branches/{_branch['id']}").json()
    jobs = {item["remote_job_id"]: item for item in detail["external_jobs"]}
    assert jobs["train-ppt"]["provider"] == "ltp/cluster-a"
    assert jobs["train-ppt"]["status"] == "completed"
    assert jobs["convert-ppt"]["provider"] == "ltp/cluster-d"
    assert jobs["convert-ppt"]["status"] == "running"
    assert "deploy-ppt" not in jobs

    state["jobs"]["convert-ppt"]["status"] = "completed"
    state["jobs"]["deploy-ppt"] = {
        "status": "running",
        "job_id": "local-3",
        "executor": "local",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    client.app.state.rops.dag_jobs.sync_all()
    detail = client.get(f"/api/branches/{_branch['id']}").json()
    jobs = {item["remote_job_id"]: item for item in detail["external_jobs"]}
    assert jobs["convert-ppt"]["status"] == "completed"
    assert jobs["deploy-ppt"]["provider"] == "dag/local"
    assert jobs["deploy-ppt"]["status"] == "running"
