from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from research_ops import models
from test_api_workflow import create_work


def test_codex_question_is_visible_answerable_and_not_logged(client, fake_codex):
    _, branch = create_work(client)
    state = client.app.state.rops
    fake_codex.respond = AsyncMock()
    params = {
        "_researchOpsBranchId": branch["id"],
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "item-1",
        "isBlocking": True,
        "questions": [
            {"id": "target", "header": "Target", "question": "Which model?", "options": [
                {"label": "A", "description": "Fast"}, {"label": "B", "description": "Accurate"}
            ]},
            {"id": "note", "header": "Note", "question": "Any extra detail?"},
        ],
    }
    asyncio.run(
        state.codex_events.handle_request("question-1", "item/tool/requestUserInput", params)
    )
    visible = client.get(f"/api/branches/{branch['id']}").json()["approvals"]
    assert len(visible) == 1
    assert visible[0]["method"] == "item/tool/requestUserInput"
    assert visible[0]["params"]["questions"][0]["question"] == "Which model?"
    endpoint = f"/api/approvals/{visible[0]['id']}/resolve"
    incomplete = {"response": {"answers": {"target": {"answers": ["A"]}}}}
    assert client.post(endpoint, json=incomplete).status_code == 422
    fake_codex.respond.assert_not_called()

    answers = {"target": {"answers": ["A"]}, "note": {"answers": ["private detail"]}}
    response = client.post(endpoint, json={"response": {"answers": answers}})
    assert response.status_code == 200, response.text
    fake_codex.respond.assert_awaited_once_with("question-1", {"answers": answers})
    assert response.json()["response"] == {"answered": True}
    assert client.get(f"/api/branches/{branch['id']}").json()["approvals"] == []
    with state.database.session_factory() as session:
        saved = session.get(models.PendingApproval, visible[0]["id"])
        assert "private detail" not in str(saved.response)


def test_restart_expires_codex_questions(client):
    state = client.app.state.rops
    with state.database.session_factory() as session:
        approval = models.PendingApproval(
            rpc_request_id="question:old", method="item/tool/requestUserInput"
        )
        session.add(approval)
        session.commit()
        approval_id = approval.id
    state.codex_events.expire_detached_claude_approvals()
    with state.database.session_factory() as session:
        assert session.get(models.PendingApproval, approval_id).status == "expired"
