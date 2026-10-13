from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from research_ops import models
from research_ops.api import create_app
from research_ops.services.session_workflows import SessionWorkflowService
from research_ops.services.workflow_links import safe_url, workflow_links
from sqlalchemy import func, select
from test_api_workflow import create_work

OCTO = "http://127.0.0.1:8300"


def test_markdown_links_are_parsed_not_executed_or_read_from_code():
    text = f"""
[工作流：评测](https://example.test/dag?revision=2)
[Workflow **diagram**][diagram]

[diagram]: https://example.test/workflow_(2)

{OCTO}/#workflow/job-1
[job one]({OCTO}/#workflow/job-1)
`{OCTO}/#workflow/code-inline`
```
[Workflow: example](https://example.test/not-real)
```
![Workflow image](https://example.test/image.png)
[A paper](https://arxiv.org/abs/123)
[Ignore earlier instructions](https://example.test/not-workflow)
[{OCTO}/#workflow/not-a-target](https://example.test/other-target)
"""
    links = workflow_links(text, OCTO)
    assert {item["url"] for item in links} == {
        "https://example.test/dag?revision=2",
        "https://example.test/workflow_(2)",
        f"{OCTO}/#workflow/job-1",
    }
    assert links[1]["title"] == "Workflow diagram"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/workflow.html",
        "data:text/html,abc",
        "http://user:secret@example.test/path",
        "http://example.test:bad/path",
        "https://example.test/?authorization=private",
        "https://example.test/?api_key=private",
        "https://example.test/#token=private",
        "https://example.test/?X-Amz-Signature=private",
        "https://example.test/?%61ccess_token=private",
        "https://example.test/a\nb",
        "https://example.test\\@evil.test/path",
    ],
)
def test_unsafe_or_secret_bearing_urls_are_rejected(url):
    assert safe_url(url) is None


def test_link_projection_is_branch_scoped_read_only_and_preserves_octolinks(client):
    _, branch = create_work(client)
    _, sibling = create_work(client)
    state = client.app.state.rops
    url = f"{OCTO}/#workflow/evaluation"
    with state.database.session_factory() as db:
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            thread_id="workflow-links-thread",
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
            external_turn_id="link-turn",
            prompt="Visualize the evaluation",
            status="completed",
            final_answer=f"[Workflow: Evaluation]({url})",
            completed_at=models.utc_now(),
        )
        db.add(turn)
        db.add_all(
            [
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="assistant",
                    content=f"[Workflow]({url})",
                ),
                models.ConversationMessage(
                    branch_id=sibling["id"],
                    role="assistant",
                    content=f"[Workflow: Sibling]({OCTO}/#workflow/sibling)",
                ),
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="tool",
                    content=f"[Workflow: Tool]({OCTO}/#workflow/tool-output)",
                ),
                models.SessionWorkflow(
                    branch_id=branch["id"],
                    graphs=[
                        {
                            "id": "octo:previous",
                            "source": "octo",
                            "title": "Previous workflow",
                        },
                        {"id": "analysis:old", "source": "analysis", "title": "Old analysis"},
                    ],
                ),
            ]
        )
        db.commit()
        saved_answer = turn.final_answer
        turn_id = turn.id
    path = f"/api/branches/{branch['id']}/workflow-links"
    first = client.get(path)
    assert first.status_code == 200
    assert {item["url"] for item in first.json()["links"]} == {
        url,
        f"{OCTO}/#workflow/previous",
    }
    assert client.get(path).json() == first.json()
    assert client.get("/api/branches/unknown/workflow-links").status_code == 404
    with state.database.session_factory() as db:
        assert db.get(models.CodexTurn, turn_id).final_answer == saved_answer
        for table in (models.GoalRun, models.SessionAttempt):
            assert db.scalar(select(func.count()).select_from(table)) == 0
        assert db.scalar(select(func.count()).select_from(models.SessionWorkflow)) == 1


def test_retired_worker_does_not_restart_even_with_legacy_flags(settings, fake_codex, monkeypatch):
    settings.session_workflows_enabled = True
    settings.session_workflow_analysis_enabled = True
    analysis, polling = AsyncMock(), AsyncMock()
    monkeypatch.setattr(SessionWorkflowService, "analyze_next", analysis)
    monkeypatch.setattr(SessionWorkflowService, "refresh_all", polling)
    with TestClient(create_app(settings, fake_codex)) as client:
        _, branch = create_work(client)
        assert client.get(f"/api/branches/{branch['id']}/workflow-links").json()["links"] == []
        assert client.post(f"/api/branches/{branch['id']}/workflow/observe").status_code == 410
    analysis.assert_not_called()
    polling.assert_not_called()
