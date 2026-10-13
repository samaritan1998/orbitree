from __future__ import annotations

import asyncio
import copy
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from research_ops import models
from research_ops.octo import OctoClient
from research_ops.services.session_workflows import (
    SessionWorkflowService,
    bounded_evidence,
    validate_evidence,
)
from research_ops.workflow_schema import WorkflowAnalysis
from sqlalchemy import func, select
from test_api_workflow import create_work


def report(evidence):
    first = evidence["records"][0]["id"]
    return WorkflowAnalysis.model_validate(
        {
            "summary": "Deployment is waiting for capacity.",
            "evidence_refs": [first],
            "nodes": [
                {
                    "id": "deploy",
                    "title": "Deploy model",
                    "detail": "Wait for capacity.",
                    "reported_status": "blocked",
                    "evidence_refs": [first],
                    "job_refs": [],
                }
            ],
            "edges": [],
        }
    )


class FakeAnalyst:
    def __init__(self):
        self.calls = []
        self.error = False

    async def analyze(self, evidence):
        self.calls.append(copy.deepcopy(evidence))
        if self.error:
            raise RuntimeError("provider unavailable")
        return report(evidence)


class FakeOcto:
    configured = True

    def __init__(self):
        self.documents = []
        self.fail = False

    async def workflows(self):
        if self.fail:
            raise RuntimeError("Octo returned HTTP 503")
        return self.documents


@pytest.fixture
def context(client, settings):
    work, branch = create_work(client)
    settings.session_workflows_enabled = True
    settings.session_workflow_analysis_enabled = True
    state = client.app.state.rops
    analyst, octo = FakeAnalyst(), FakeOcto()
    service = SessionWorkflowService(state.database.session_factory, settings, octo, analyst)
    state.session_workflows = service
    with state.database.session_factory() as db:
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            thread_id="session-workflow-main",
            status="current",
            source="terminal",
            bound_at=models.utc_now(),
        )
        db.add(binding)
        db.flush()
        turn = models.CodexTurn(
            branch_id=branch["id"],
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="workflow-first",
            prompt="Deploy the model",
            final_answer="Waiting for capacity. No task was submitted.",
            completed_at=models.utc_now(),
            status="completed",
        )
        db.add(turn)
        db.commit()
        turn_id = turn.id
    return client, settings, work, branch, service, analyst, octo, turn_id


def document(branch_id=None, node_meta=None):
    return {
        "job_id": "experiment-workflow-r1",
        "meta": {"research_ops": {"branch_id": branch_id}},
        "status": "running",
        "current_stage": "train",
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T01:00:00Z",
        "nodes": [
            {"name": "train", "status": "succeeded", "meta": node_meta or {}},
            {"name": "evaluate", "status": "pending", "meta": {}},
        ],
        "edges": [{"source": "train", "target": "evaluate"}],
    }


@pytest.mark.asyncio
async def test_analysis_is_cached_and_cannot_write_the_original_session(context, tmp_path):
    client, _, _, branch, service, analyst, _, turn_id = context
    branch_id = branch["id"]
    before = client.get(f"/api/branches/{branch_id}").json()
    original = tmp_path / "original-session.jsonl"
    original.write_text('{"message":"unchanged original query"}\n')
    saved = original.read_bytes()
    endpoint = f"/api/branches/{branch_id}/workflow"
    assert client.get(endpoint).json()["observing"] is False
    service.observe(branch_id)
    service.refresh_branch(branch_id)
    assert await service.analyze_next()
    view = client.get(endpoint).json()
    assert view["analysis_status"] == "ready"
    graph = view["graphs"][-1]
    assert graph["source"] == "analysis"
    assert graph["nodes"][0]["status_basis"] == "reported"
    assert graph["nodes"][0]["evidence_refs"] == [f"turn:{turn_id}"]
    assert graph["nodes"][0]["status"] == "blocked"
    service.refresh_branch(branch_id)
    assert not await service.analyze_next()
    assert len(analyst.calls) == 1
    after = client.get(f"/api/branches/{branch_id}").json()
    assert after["branch"] == before["branch"]
    assert after["work_item"] == before["work_item"]
    assert after["turns"] == before["turns"]
    assert original.read_bytes() == saved
    with service.session_factory() as db:
        for table in (models.ConversationMessage, models.GoalRun, models.SessionAttempt):
            assert db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
async def test_octo_matches_session_nodes_without_borrowing_sibling_jobs(context):
    _, _, work, branch, service, _, octo, _ = context
    octo.documents = [
        document(node_meta={"codex_session_id": "session-workflow-main"}),
        {**document("unrelated-branch"), "job_id": "unrelated-workflow"},
    ]
    with service.session_factory() as db:
        db.add(
            models.ExternalJob(
                work_item_id=work["id"],
                provider="ltp",
                remote_job_id="sibling-job-id",
                status="running",
                metadata_json={"branch_id": "sibling"},
            )
        )
        db.commit()
    service.observe(branch["id"])
    await service.refresh_all()
    view = service.view(branch["id"])
    assert len(view["graphs"]) == 1
    graph = view["graphs"][0]
    assert [node["associated"] for node in graph["nodes"]] == [True, False]
    assert [node["status"] for node in graph["nodes"]] == ["completed", "pending"]
    assert not any(item["kind"] == "job" for item in view["evidence"])
    assert graph["edges"] == [{"source": "train", "target": "evaluate"}]


@pytest.mark.asyncio
async def test_octo_failure_preserves_last_snapshot_and_recovers_deletions(context):
    _, _, _, branch, service, _, octo, _ = context
    service.observe(branch["id"])
    octo.documents = [document(branch["id"])]
    await service.refresh_all()
    assert service.view(branch["id"])["octo"]["state"] == "connected"
    octo.fail = True
    service._last_octo_poll = 0
    await service.refresh_all()
    view = service.view(branch["id"])
    assert view["octo"]["state"] == "error"
    assert view["graphs"][0]["stale"]
    octo.fail, octo.documents, service._last_octo_poll = False, [], 0
    await service.refresh_all()
    assert service.view(branch["id"])["graphs"] == []


@pytest.mark.asyncio
async def test_polling_timestamps_do_not_reanalyze_but_job_changes_do(context):
    _, _, _, branch, service, analyst, octo, _ = context
    service.observe(branch["id"])
    octo.documents = [document(branch["id"])]
    await service.refresh_all()
    await service.analyze_next()
    service._last_octo_poll = 0
    await service.refresh_all()
    assert not await service.analyze_next()
    assert len(analyst.calls) == 1
    octo.documents[0]["nodes"][1]["status"] = "running"
    service._last_octo_poll = 0
    await service.refresh_all()
    view = service.view(branch["id"])
    assert view["analysis_status"] == "queued"
    assert view["analysis_stale"]
    assert view["graphs"][0]["nodes"][1]["status"] == "running"


@pytest.mark.asyncio
async def test_failure_retains_old_analysis_and_manual_retry(context):
    client, _, _, branch, service, analyst, _, turn_id = context
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    await service.analyze_next()
    with service.session_factory() as db:
        db.get(models.CodexTurn, turn_id).final_answer = "Capacity became available."
        row = db.get(models.SessionWorkflow, branch["id"])
        row.analyzed_at = models.utc_now() - timedelta(hours=1)
        db.commit()
    service.refresh_branch(branch["id"])
    analyst.error = True
    assert await service.analyze_next()
    view = service.view(branch["id"])
    assert view["analysis_status"] == "error"
    assert "provider unavailable" in view["analysis_error"]
    assert view["graphs"][-1]["summary"]
    service.observe(branch["id"], retry=True)
    analyst.error = False
    assert await service.analyze_next()
    assert service.view(branch["id"])["analysis_status"] == "ready"


@pytest.mark.asyncio
async def test_analysis_holds_no_db_transaction_and_changed_input_is_requeued(context):
    _, _, _, branch, service, _, _, turn_id = context
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowAnalyst:
        async def analyze(self, evidence):
            entered.set()
            await release.wait()
            return report(evidence)

    service.analyst = SlowAnalyst()
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    task = asyncio.create_task(service.analyze_next())
    await asyncio.wait_for(entered.wait(), 3)
    with service.session_factory() as db:
        db.get(models.CodexTurn, turn_id).final_answer = "A newer user-directed outcome."
        db.commit()
    service.refresh_branch(branch["id"])
    assert service._claim() is None
    release.set()
    await task
    view = service.view(branch["id"])
    assert view["analysis_status"] == "queued"
    assert view["analysis_stale"]


def test_dag_runner_reads_only_registered_paths_and_keeps_unstarted_nodes(context, tmp_path):
    _, settings, work, branch, service, _, _, _ = context
    settings.managed_job_allowed_roots = [tmp_path]
    state_path, definition_path = tmp_path / "state.json", tmp_path / "dag.json"
    state_path.write_text(
        json.dumps({"jobs": {"train": {"status": "running", "job_id": "ltp123"}}})
    )
    definition_path.write_text(
        json.dumps({"jobs": {"train": {}, "eval": {"depends_on": ["train"]}}})
    )
    with service.session_factory() as db:
        db.add(
            models.ExternalJob(
                work_item_id=work["id"],
                provider="dag-runner",
                remote_job_id="dag-exp",
                status="running",
                metadata_json={
                    "branch_id": branch["id"],
                    "state_path": str(state_path),
                    "dag_path": str(definition_path),
                },
            )
        )
        db.commit()
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    graph = service.view(branch["id"])["graphs"][0]
    assert graph["nodes"][1]["status"] == "pending"
    assert graph["edges"] == [{"source": "train", "target": "eval"}]
    definition_path.write_text("not valid JSON")
    service.refresh_branch(branch["id"])
    assert service.view(branch["id"])["warnings"]


def test_analysis_evidence_rejects_hallucinated_refs_and_cycles():
    evidence = {"records": [{"id": "turn:one", "kind": "turn"}]}
    value = report(evidence)
    value.nodes[0].evidence_refs = ["turn:invented"]
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_evidence(value, evidence)
    value.nodes[0].evidence_refs = ["turn:one"]
    value.nodes[0].job_refs = ["job:another-branch"]
    with pytest.raises(ValueError, match="unrelated job"):
        validate_evidence(value, evidence)
    payload = report(evidence).model_dump()
    payload["edges"] = [{"source": "deploy", "target": "deploy", "evidence_refs": ["turn:one"]}]
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowAnalysis.model_validate(payload)


def test_bounded_evidence_preserves_recent_requests_without_large_payloads():
    evidence = {
        "records": [
            {"id": f"turn:{i}", "kind": "turn", "excerpt": "a" * 1000} for i in range(1000)
        ],
        "source_graphs": [],
    }
    small = bounded_evidence(evidence, 9000)
    assert len(json.dumps(small)) < 9000
    assert small["records"][-1]["id"] == "turn:999"


@pytest.mark.asyncio
async def test_octo_client_only_gets_and_paginates_with_private_credentials(settings):
    settings.octo_api_key = "test-private-key"
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-private-key"
        offset = int(request.url.params["offset"])
        items = [
            {**document(), "job_id": f"job-{i}"} for i in range(offset, min(offset + 100, 101))
        ]
        return httpx.Response(200, json={"total": 101, "workflows": items})

    value = await OctoClient(settings, httpx.MockTransport(handler)).workflows()
    assert len(value) == 101
    assert len(calls) == 2
    assert "test-private-key" not in repr(settings)


def test_unknown_branch_is_404_and_workflow_disabled_blocks_observation(client):
    assert client.get("/api/branches/unknown/workflow").status_code == 404
    _, branch = create_work(client)
    assert client.post(f"/api/branches/{branch['id']}/workflow/observe").status_code == 410


def test_expired_last_analysis_lease_is_not_stuck_running(context):
    _, _, _, branch, service, _, _, _ = context
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    work = service._claim()
    assert work
    with service.session_factory() as db:
        row = db.get(models.SessionWorkflow, branch["id"])
        row.analysis_attempts = 3
        row.lease_until = models.utc_now() - timedelta(seconds=1)
        db.commit()
    assert service._claim() is None
    value = service.view(branch["id"])
    assert value["analysis_status"] == "error"
    assert value["next_analysis_at"] is None


@pytest.mark.parametrize("legacy", [True, False])
def test_only_legacy_http_failures_are_requeued_once(context, legacy):
    _, _, _, branch, service, _, _, _ = context
    service.observe(branch["id"])
    with service.session_factory() as db:
        row = db.get(models.SessionWorkflow, branch["id"])
        row.analysis_status = "error"
        row.analysis_error = (
            "Workflow analysis returned HTTP 403" if legacy else "Invalid evidence reference"
        )
        row.analysis_attempts = 3
        row.next_analysis_at = None
        db.commit()
    assert service.retry_legacy_http_failures() == int(legacy)
    assert service.retry_legacy_http_failures() == 0
    value = service.view(branch["id"])
    assert value["analysis_status"] == ("queued" if legacy else "error")
    if legacy:
        assert value["analysis_error"] is None
        assert value["next_analysis_at"] is not None


@pytest.mark.asyncio
async def test_three_failed_analyses_stop_until_retry_or_new_evidence(context):
    _, _, _, branch, service, analyst, _, _ = context
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    analyst.error = True
    for _ in range(3):
        with service.session_factory() as db:
            db.get(models.SessionWorkflow, branch["id"]).next_analysis_at = models.utc_now()
            db.commit()
        assert await service.analyze_next()
    value = service.view(branch["id"])
    assert value["analysis_status"] == "error" and value["next_analysis_at"] is None
    assert not await service.analyze_next()
    assert len(analyst.calls) == 3


@pytest.mark.asyncio
async def test_observed_job_status_overrides_ai_claim_without_writing_back(context):
    _, _, work, branch, service, _, _, _ = context
    with service.session_factory() as db:
        job = models.ExternalJob(
            work_item_id=work["id"],
            provider="ltp",
            remote_job_id="own-job-r1",
            status="running",
            metadata_json={"branch_id": branch["id"]},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    class JobAnalyst:
        async def analyze(self, evidence):
            value = report(evidence)
            value.nodes[0].reported_status = "completed"
            value.nodes[0].job_refs = [f"job:{job_id}"]
            return value

    service.analyst = JobAnalyst()
    service.observe(branch["id"])
    service.refresh_branch(branch["id"])
    await service.analyze_next()
    node = service.view(branch["id"])["graphs"][-1]["nodes"][0]
    assert node["status"] == "running" and node["status_basis"] == "observed"
    with service.session_factory() as db:
        assert db.get(models.ExternalJob, job_id).status == "running"


@pytest.mark.asyncio
async def test_incomplete_octo_catalog_cannot_erase_known_workflows(settings):
    settings.octo_api_key = "test-private-key"
    adapter = OctoClient(
        settings,
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"total": 2, "workflows": [document()]})
        ),
    )
    with pytest.raises(RuntimeError, match="incomplete catalog"):
        await adapter.workflows()


@pytest.mark.asyncio
async def test_reports_are_read_only_and_require_a_branch_association(context):
    client, settings, _, branch, service, _, octo, _ = context
    octo.documents = [document(branch["id"])]
    service.observe(branch["id"])
    await service.refresh_all()
    settings.octo_api_key = "test-private-key"
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/reports"):
            assert request.url.params["node_name"] == "train"
            return httpx.Response(200, json={"total": 1, "reports": [{"id": 1}]})
        return httpx.Response(
            200,
            json={
                "id": 1,
                "node_name": "train",
                "kind": "result",
                "format": "html",
                "content": "<script>alert(1)</script>",
            },
        )

    service.octo = OctoClient(settings, httpx.MockTransport(handler))
    path = f"/api/branches/{branch['id']}/workflow/reports"
    assert (
        client.get(path, params={"workflow_id": "octo:unrelated", "node_id": "train"}).status_code
        == 404
    )
    assert calls == []
    response = client.get(
        path, params={"workflow_id": "octo:experiment-workflow-r1", "node_id": "train"}
    )
    assert response.status_code == 200
    assert response.json()["reports"][0]["content"].startswith("<script>")
    assert len(calls) == 2
