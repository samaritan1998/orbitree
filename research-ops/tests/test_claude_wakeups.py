import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from research_ops import models, schemas
from research_ops.services.claude_wakeups import ClaudeWakeupService
from sqlalchemy import select
from test_api_workflow import create_work


def setup(client, fake_codex):
    _, branch = create_work(client)
    state = client.app.state.rops
    state.settings.scheduled_checks_enabled = True
    state.service.claude = fake_codex
    with state.database.session_factory() as session:
        row = session.get(models.ExecutionBranch, branch["id"])
        row.runtime, row.codex_thread_id = "claude", "claude-thread"
        row.execution_preferences = {"model": "opus", "reasoning_effort": "high"}
        session.commit()
    return state, branch["id"]


def schedule(
    state, branch_id, *, tool_id="tool-1", prompt="  Check job status once.\n", completed=True
):
    params = {
        "threadId": "claude-thread",
        "turnId": "source-turn",
        "toolId": tool_id,
        "input": {"delaySeconds": 1000, "prompt": prompt},
    }
    state.claude_wakeups.observe(branch_id, "claude/wakeup/scheduled", params)
    if completed:
        state.claude_wakeups.observe(
            branch_id,
            "turn/completed",
            {
                **params,
                "turn": {"status": "completed"},
            },
        )
    return state.claude_wakeups.detail(branch_id)[0]["id"]


def due(state, wakeup_id, *, ago=timedelta(seconds=1)):
    with state.database.session_factory() as session:
        row = session.get(models.ClaudeWakeup, wakeup_id)
        row.due_at = row.next_attempt_at = datetime.now(UTC) - ago
        session.commit()


async def drain(state):
    for tasks in (state.claude_wakeup_worker.tasks, state.service._web_turn_tasks):
        if tasks:
            await asyncio.wait_for(asyncio.gather(*list(tasks.values())), 5)


def test_live_wakeup_persists_and_does_not_replace_manual_periodic_check(client, fake_codex):
    state, branch = setup(client, fake_codex)
    state.scheduled_checks.save(branch, schemas.ScheduledCheckSave(prompt="manual prompt"))
    wakeup_id = schedule(state, branch)
    schedule(state, branch)
    reopened = ClaudeWakeupService(state.database.session_factory, state.settings)
    assert len(reopened.detail(branch)) == 1
    assert reopened.detail(branch)[0]["id"] == wakeup_id
    assert reopened.detail(branch)[0]["prompt"] == "  Check job status once.\n"
    assert state.scheduled_checks.detail(branch)["check"]["prompt"] == "manual prompt"
    assert reopened.reconcile_and_claim()[0] == []


@pytest.mark.asyncio
async def test_due_wakeup_sends_once_to_same_thread_verbatim(client, fake_codex, monkeypatch):
    state, branch = setup(client, fake_codex)
    wakeup_id = schedule(state, branch)
    expected = state.claude_wakeups.get(wakeup_id).execution
    seen = []

    async def send(**kwargs):
        seen.append(kwargs)
        return {"turn": {"id": "wakeup-turn"}}

    monkeypatch.setattr(fake_codex, "send_message", send)
    due(state, wakeup_id)
    assert state.claude_wakeups.reconcile_and_claim()[0] == [wakeup_id]
    assert state.claude_wakeups.reconcile_and_claim()[0] == []
    await state.claude_wakeup_worker._start(wakeup_id)
    await drain(state)
    assert len(seen) == 1
    assert seen[0]["thread_id"] == "claude-thread"
    assert seen[0]["content"] == "  Check job status once.\n"
    assert seen[0]["metadata"]["research_ops_model"] == expected["model"]
    assert seen[0]["metadata"]["research_ops_reasoning_effort"] == expected["reasoning_effort"]
    state.claude_wakeups.reconcile_and_claim()
    assert state.claude_wakeups.get(wakeup_id).status == "completed"
    await state.claude_wakeup_worker._start(wakeup_id)
    assert len(seen) == 1
    with state.database.session_factory() as session:
        message = session.get(
            models.ConversationMessage, state.claude_wakeups.get(wakeup_id).message_id
        )
        assert message.content == "  Check job status once.\n"
        assert message.metadata_json["claude_wakeup_id"] == wakeup_id


@pytest.mark.asyncio
async def test_busy_session_defers_and_cancellation_wins_dispatch_race(
    client, fake_codex, monkeypatch
):
    state, branch = setup(client, fake_codex)
    wakeup_id = schedule(state, branch)
    due(state, wakeup_id)
    state.claude_wakeups.reconcile_and_claim()
    from research_ops.services.core import ConflictError

    async def busy(*args, **kwargs):
        raise ConflictError("Local CLI is busy")

    with monkeypatch.context() as patch:
        patch.setattr(state.service, "queue_web_message", busy)
        await state.claude_wakeup_worker._start(wakeup_id)
    row = state.claude_wakeups.get(wakeup_id)
    assert row.status == "scheduled"
    assert row.reason == "Local CLI is busy"
    assert state.claude_wakeups.reconcile_and_claim()[0] == []
    due(state, wakeup_id)
    state.claude_wakeups.reconcile_and_claim()
    original = state.service._ensure_web_attempt

    async def cancel_during_start(*args, **kwargs):
        state.claude_wakeups.cancel(branch, wakeup_id)
        return await original(*args, **kwargs)

    monkeypatch.setattr(state.service, "_ensure_web_attempt", cancel_during_start)
    await state.claude_wakeup_worker._start(wakeup_id)
    assert state.claude_wakeups.get(wakeup_id).status == "cancelled"
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))


@pytest.mark.parametrize("change", ["hidden", "thread", "failed_source", "overdue", "disabled"])
def test_unsafe_or_stale_wakeups_never_start(client, fake_codex, change):
    state, branch = setup(client, fake_codex)
    if change == "disabled":
        state.settings.scheduled_checks_enabled = False
    wakeup_id = schedule(state, branch, completed=change != "failed_source")
    due(state, wakeup_id, ago=timedelta(days=2) if change == "overdue" else timedelta(seconds=1))
    with state.database.session_factory() as session:
        row = session.get(models.ExecutionBranch, branch)
        if change == "hidden":
            row.hidden = True
        elif change == "thread":
            row.codex_thread_id = "another-thread"
        session.commit()
    if change == "failed_source":
        state.claude_wakeups.observe(
            branch,
            "turn/completed",
            {
                "threadId": "claude-thread",
                "turnId": "source-turn",
                "turn": {"status": "aborted"},
            },
        )
    assert not state.claude_wakeups.reconcile_and_claim()[0]
    assert state.claude_wakeups.get(wakeup_id).status in {"cancelled", "missed"}


def test_new_tool_replaces_old_and_no_historical_import(client, fake_codex):
    state, branch = setup(client, fake_codex)
    assert state.claude_wakeups.detail(branch) == []
    old = schedule(state, branch)
    new = schedule(state, branch, tool_id="tool-2")
    assert state.claude_wakeups.get(old).status == "cancelled"
    assert state.claude_wakeups.get(new).status == "scheduled"
    response = client.post(f"/api/branches/{branch}/claude-wakeups/{new}/cancel")
    assert response.status_code == 200
    assert response.json()["wakeups"][0]["status"] == "cancelled"


def test_restart_only_reclaims_unaccepted_dispatch(client, fake_codex):
    state, branch = setup(client, fake_codex)
    wakeup_id = schedule(state, branch)
    due(state, wakeup_id)
    state.claude_wakeups.reconcile_and_claim()
    with state.database.session_factory() as session:
        row = session.get(models.ClaudeWakeup, wakeup_id)
        row.claimed_at = datetime.now(UTC) - timedelta(minutes=6)
        session.commit()
    reopened = ClaudeWakeupService(state.database.session_factory, state.settings)
    assert reopened.reconcile_and_claim()[0] == [wakeup_id]
    with state.database.session_factory() as session:
        row = session.get(models.ClaudeWakeup, wakeup_id)
        row.status = "running"
        session.commit()
    assert reopened.reconcile_and_claim()[0] == []
    assert reopened.get(wakeup_id).status == "failed"


@pytest.mark.asyncio
async def test_new_manual_message_cancels_pending_wakeup(client, fake_codex):
    state, branch = setup(client, fake_codex)
    wakeup_id = schedule(state, branch)
    await state.service.queue_web_message(
        branch, schemas.ConversationSend(content="Stop checking the old job")
    )
    await drain(state)
    assert state.claude_wakeups.get(wakeup_id).status == "cancelled"
