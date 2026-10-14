from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from research_ops import models
from sqlalchemy import select
from test_native_goals import _materialized_branch


def _goal(thread_id, **updates):
    return {
        "threadId": thread_id,
        "objective": "Original objective\nNo additions.",
        "status": "active",
        "tokenBudget": None,
        "tokensUsed": 12,
        "timeUsedSeconds": 3,
        "createdAt": 10,
        "updatedAt": 11,
        **updates,
    }


def test_replacement_and_clear_preserve_goals_with_notifications_off(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}/native-goal"
    state = client.app.state.rops
    old = _goal(branch["codex_thread_id"], status="complete")
    fake_codex.goals[branch["codex_thread_id"]] = old
    # The old Goal was never observed by Orbitree before this replacement.
    created = client.put(endpoint, json={"objective": "  A new goal\n  "})
    assert created.status_code == 200, created.text
    assert created.json()["goal"]["objective"] == "  A new goal\n  "
    history = client.get(f"{endpoint}/history").json()
    assert history["total"] == 1
    assert history["items"][0]["objective"] == old["objective"]
    assert history["items"][0]["status"] == "complete"
    assert history["items"][0]["tokens_used"] == 12
    assert history["items"][0]["cleared_at"]

    assert client.post(f"{endpoint}/control", json={"action": "clear"}).status_code == 200
    history = client.get(f"{endpoint}/history").json()
    assert history["total"] == 2
    assert {item["status"] for item in history["items"]} == {"complete", "cleared"}
    with state.database.session_factory() as session:
        assert session.scalar(select(models.NativeGoalNotification)) is None


def test_thread_rollover_retains_history_without_calling_codex(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    old = _goal(branch["codex_thread_id"], status="complete")
    state.native_goals.watch.observe_goal(branch["id"], branch["codex_thread_id"], old)
    with state.database.session_factory() as session:
        session.get(models.ExecutionBranch, branch["id"]).codex_thread_id = "new-thread"
        session.commit()
    fake_codex.get_goal = AsyncMock(side_effect=AssertionError("History must not contact Codex"))
    history = client.get(f"/api/branches/{branch['id']}/native-goal/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["thread_id"] == branch["codex_thread_id"]
    assert history.json()["items"][0]["status"] == "complete"
    fake_codex.get_goal.assert_not_called()
    assert fake_codex.goal_resumes == []


def test_progress_is_deduplicated_and_old_events_cannot_rewrite_history(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    watch = state.native_goals.watch
    tid = branch["codex_thread_id"]
    old = _goal(tid)
    watch.observe_goal(branch["id"], tid, old)
    watch.observe_goal(branch["id"], tid, {**old, "status": "complete", "updatedAt": 20})
    watch.observe_event(
        branch["id"],
        "item/completed",
        {
            "threadId": tid,
            "item": {"type": "agentMessage", "text": "Recorded final update."},
        },
    )
    watch.observe_goal(branch["id"], tid, old)
    with state.database.session_factory() as session:
        records = list(session.scalars(select(models.NativeGoalHistory)))
        assert len(records) == 1
        assert records[0].goal_snapshot["status"] == "complete"
        assert records[0].last_summary == "Recorded final update."
        session.get(models.ExecutionBranch, branch["id"]).codex_thread_id = "next-thread"
        session.commit()
    watch.observe_event(
        branch["id"],
        "item/completed",
        {
            "threadId": tid,
            "item": {"type": "agentMessage", "text": "Late stale event."},
        },
    )
    history = state.native_goal_history.page(branch["id"])
    assert history.items[0].last_summary == "Recorded final update."


def _notice(branch, key, *, observed_at, **payload):
    return models.NativeGoalNotification(
        branch_id=branch["id"],
        event_key=f"{branch['id']}:{key}:{observed_at.timestamp()}",
        recipient="test",
        kind="cleared" if payload.get("goal_status") == "cleared" else "state",
        payload={
            "thread_id": "historical-thread",
            "objective": "Recovered original",
            "goal_status": "active",
            "observed_at": observed_at.isoformat(),
            **payload,
        },
        created_at=observed_at,
        updated_at=observed_at,
    )


def test_backfill_is_idempotent_and_uses_latest_evidence(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    now = datetime.now(UTC)
    fingerprint = "a" * 64
    with state.database.session_factory() as session:
        session.add_all(
            [
                _notice(branch, fingerprint, observed_at=now - timedelta(hours=1)),
                _notice(branch, fingerprint, observed_at=now, goal_status="complete", summary="Done."),
                _notice(branch, "b" * 64, observed_at=now, goal_status="cleared"),
            ]
        )
        session.commit()
    assert state.native_goal_history.backfill() == 2
    assert state.native_goal_history.backfill() == 0
    records = state.native_goal_history.page(branch["id"]).items
    completed = next(item for item in records if item.goal_key == fingerprint)
    assert completed.status == "complete"
    assert completed.last_summary == "Done."
    assert completed.source == "notification"
    assert completed.goal_created_at is None
    assert completed.tokens_used is None
    assert completed.first_recorded_at == now - timedelta(hours=1)
    assert next(item for item in records if item.goal_key != fingerprint).cleared_at == now
    assert fake_codex.goal_resumes == []


def test_backfill_prefers_full_monitor_snapshot_and_preserves_live_records(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        session.add(_notice(branch, "a" * 64, observed_at=now - timedelta(minutes=1)))
        session.add(
            models.NativeGoalMonitor(
                branch_id=branch["id"],
                goal_key="a" * 64,
                thread_id="historical-thread",
                goal_snapshot=_goal(
                    "historical-thread", objective="Exact original", status="complete"
                ),
                last_summary="Full monitor result",
                updated_at=now,
            )
        )
        session.commit()
    state.native_goal_history.backfill()
    record = state.native_goal_history.page(branch["id"]).items[0]
    assert record.objective == "Exact original"
    assert record.goal_created_at == 10
    assert record.last_summary == "Full monitor result"
    assert record.status == "complete"
    assert record.source == "monitor"
    assert state.native_goal_history.backfill() == 0


def test_history_pagination_validation_and_branch_isolation(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        for index in range(3):
            session.add(
                _notice(branch, str(index) * 64, observed_at=now + timedelta(seconds=index))
            )
        session.commit()
    state.native_goal_history.backfill()
    endpoint = f"/api/branches/{branch['id']}/native-goal/history"
    page = client.get(endpoint, params={"limit": 2}).json()
    assert page["total"] == 3
    assert page["next_offset"] == 2
    assert [item["goal_key"][0] for item in page["items"]] == ["2", "1"]
    last = client.get(endpoint, params={"limit": 2, "offset": 2}).json()
    assert last["next_offset"] is None
    assert last["items"][0]["goal_key"][0] == "0"
    assert client.get(endpoint, params={"offset": -1}).status_code == 422
    assert client.get(endpoint, params={"limit": 101}).status_code == 422
    assert client.get("/api/branches/missing/native-goal/history").status_code == 404


def test_history_survives_database_reinitialization(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    tid = branch["codex_thread_id"]
    state.native_goals.watch.observe_goal(branch["id"], tid, _goal(tid))
    state.native_goals.watch.observe_goal(branch["id"], tid, None)
    state.database.init()
    assert state.native_goal_history.backfill() == 0
    assert state.native_goal_history.page(branch["id"]).items[0].status == "cleared"


def test_completed_current_goal_moves_into_history(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    state = client.app.state.rops
    tid = branch["codex_thread_id"]
    goal = _goal(tid)
    state.native_goals.watch.observe_goal(branch["id"], tid, goal)
    assert state.native_goal_history.page(branch["id"]).total == 0
    state.native_goals.watch.observe_goal(branch["id"], tid, {**goal, "status": "complete"})
    assert state.native_goal_history.page(branch["id"]).items[0].status == "complete"
