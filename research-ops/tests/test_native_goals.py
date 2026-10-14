from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from research_ops import models
from research_ops.codex.base import CodexUnavailable
from sqlalchemy import select


def _materialized_branch(client: TestClient, fake_codex) -> dict:  # type: ignore[no-untyped-def]
    created = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Native Goal API",
            "objective": "Exercise the Codex Goal lifecycle.",
            "branch_mode": "write",
        },
    )
    assert created.status_code == 200, created.text
    tree = client.get("/api/programs/HARN/tree").json()
    branch = next(
        item for item in tree["branches"] if item["work_item_id"] == created.json()["id"]
    )
    thread_id = "019f-native-goal-thread"
    state = client.app.state.rops
    with state.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        assert stored is not None
        stored.codex_thread_id = thread_id
        session.add(
            models.CodexThreadBinding(
                branch_id=stored.id,
                thread_id=thread_id,
                source="test",
                status="current",
                bound_at=stored.created_at,
            )
        )
        session.commit()
    fake_codex.threads.add(thread_id)
    return {**branch, "codex_thread_id": thread_id}


def test_native_goal_api_round_trip(client: TestClient, fake_codex) -> None:  # type: ignore[no-untyped-def]
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}/native-goal"

    empty = client.get(endpoint)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "supported": True,
        "thread_id": branch["codex_thread_id"],
        "goal": None,
        "reason": None,
        "runtime": None,
    }

    created = client.put(
        endpoint,
        json={"objective": "Finish the native implementation.", "token_budget": 250_000},
    )
    assert created.status_code == 200, created.text
    goal = created.json()["goal"]
    assert goal["objective"] == "Finish the native implementation."
    assert goal["status"] == "active"
    assert goal["token_budget"] == 250_000

    paused = client.post(f"{endpoint}/control", json={"action": "pause"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["goal"]["status"] == "paused"

    resumed = client.post(f"{endpoint}/control", json={"action": "resume"})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["goal"]["status"] == "active"

    completed = client.post(f"{endpoint}/control", json={"action": "complete"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["goal"]["status"] == "complete"

    cleared = client.post(f"{endpoint}/control", json={"action": "clear"})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["goal"] is None
    assert fake_codex.goals == {}

    state = client.app.state.rops
    with state.database.session_factory() as session:
        event_types = list(
            session.scalars(
                select(models.ActivityEvent.event_type).where(
                    models.ActivityEvent.scope_id == branch["id"]
                )
            )
        )
    assert "codex.goal_set" in event_types
    assert "codex.goal_paused" in event_types
    assert "codex.goal_resumed" in event_types
    assert "codex.goal_completed" in event_types
    assert "codex.goal_cleared" in event_types


def test_pause_stops_active_goal_turn_using_original_owner(client, fake_codex, monkeypatch):
    branch = _materialized_branch(client, fake_codex)
    thread_id = branch["codex_thread_id"]
    endpoint = f"/api/branches/{branch['id']}/native-goal"
    assert client.put(endpoint, json={"objective": "Keep working"}).status_code == 200
    fake_codex.goal_turns[thread_id] = {"id": "running-turn", "status": "inProgress"}
    calls = []

    async def interrupt(**kwargs):
        assert fake_codex.goals[thread_id]["status"] == "paused"
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(fake_codex, "interrupt_turn", interrupt)
    paused = client.post(endpoint + "/control", json={"action": "pause"})
    assert paused.status_code == 200, paused.text
    assert calls == [{"thread_id": thread_id, "turn_id": "running-turn"}]
    assert paused.json()["goal"]["status"] == "paused"
    assert paused.json()["runtime"]["next_retry_at"] is None


@pytest.mark.parametrize("turn", [{}, {"id": "old-turn", "status": "completed"}])
def test_pause_does_not_interrupt_idle_goal(client, fake_codex, turn):
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}/native-goal"
    assert client.put(endpoint, json={"objective": "Keep working"}).status_code == 200
    fake_codex.goal_turns[branch["codex_thread_id"]] = turn
    assert client.post(endpoint + "/control", json={"action": "pause"}).status_code == 200
    assert fake_codex.interrupted_turns == []


def test_pause_reports_interruption_failure_without_resuming_goal(client, fake_codex, monkeypatch):
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}/native-goal"
    assert client.put(endpoint, json={"objective": "Keep working"}).status_code == 200
    fake_codex.goal_turns[branch["codex_thread_id"]] = {"id": "live", "status": "inProgress"}
    monkeypatch.setattr(fake_codex, "interrupt_turn", AsyncMock(
        side_effect=CodexUnavailable("Control channel unavailable")
    ))
    paused = client.post(endpoint + "/control", json={"action": "pause"})
    assert paused.status_code == 409
    assert "Goal paused, but the current turn could not be interrupted" in paused.text
    assert fake_codex.goals[branch["codex_thread_id"]]["status"] == "paused"
    with client.app.state.rops.database.session_factory() as session:
        monitor = session.get(models.NativeGoalMonitor, branch["id"])
        assert monitor.next_retry_at is None
        assert "Control channel unavailable" in monitor.last_error
    monkeypatch.setattr(fake_codex, "interrupt_turn", AsyncMock(return_value={}))
    retried = client.post(endpoint + "/control", json={"action": "pause"})
    assert retried.status_code == 200
    assert retried.json()["runtime"]["last_error"] is None
    assert retried.json()["goal"]["status"] == "paused"


def test_native_goal_requires_a_materialized_codex_thread(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/work-items",
        json={
            "program_code": "COMMON",
            "title": "Deferred native Goal",
            "objective": "Wait for a real thread.",
            "branch_mode": "write",
        },
    )
    tree = client.get("/api/programs/COMMON/tree").json()
    branch = next(
        item for item in tree["branches"] if item["work_item_id"] == created.json()["id"]
    )
    endpoint = f"/api/branches/{branch['id']}/native-goal"

    state = client.get(endpoint)
    assert state.status_code == 200
    assert state.json()["supported"] is False
    assert "Run one Codex turn first" in state.json()["reason"]

    rejected = client.put(endpoint, json={"objective": "This cannot run yet."})
    assert rejected.status_code == 409


def test_active_native_goal_is_recovered_after_service_restart(
    client: TestClient,
    fake_codex,
) -> None:  # type: ignore[no-untyped-def]
    branch = _materialized_branch(client, fake_codex)
    thread_id = branch["codex_thread_id"]
    fake_codex.goals[thread_id] = {
        "threadId": thread_id,
        "objective": "Keep working after Orbitree restarts.",
        "status": "active",
        "tokenBudget": 100_000,
        "tokensUsed": 12_000,
        "timeUsedSeconds": 90,
        "createdAt": 1,
        "updatedAt": 2,
    }

    state = client.app.state.rops
    recovered = asyncio.run(state.native_goals.recover_active())

    assert recovered["recovered"] == 1
    assert fake_codex.goal_resumes == [thread_id]
    assert fake_codex.goals[thread_id]["status"] == "active"
    with state.database.session_factory() as session:
        event = session.scalar(
            select(models.ActivityEvent).where(
                models.ActivityEvent.scope_id == branch["id"],
                models.ActivityEvent.event_type == "codex.goal_recovered",
            )
        )
    assert event is not None
    assert event.payload["thread_id"] == thread_id
