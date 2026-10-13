from __future__ import annotations

from datetime import UTC, datetime

import pytest
from research_ops import models
from research_ops.api import create_app
from research_ops.services.codex_events import CodexEventService
from research_ops.services.turn_notifications import TurnNotificationService
from sqlalchemy import select
from test_api_workflow import create_work
from test_turn_notifications import FakeSender, _attempt_and_binding


def _completed_event(client, runtime="claude", *, answer="Completed result", metadata=None):
    work, branch = create_work(client)
    state = client.app.state.rops
    turn_id = f"{runtime}-completed-turn"
    with state.database.session_factory() as session:
        session.get(models.ExecutionBranch, branch["id"]).runtime = runtime
        attempt, binding = _attempt_and_binding(session, branch, owner="web")
        thread_id = binding.thread_id
        session.add(models.ConversationMessage(
            branch_id=branch["id"], session_id=attempt.id,
            role="user", turn_id=turn_id, content="Check the experiment",
            created_at=datetime.now(UTC),
            metadata_json={"runtime": runtime, **(metadata or {})},
        ))
        if answer is not None:
            session.add(models.ConversationMessage(
                branch_id=branch["id"], session_id=attempt.id,
                role="assistant", turn_id=turn_id, content=answer,
                metadata_json={"runtime": runtime},
            ))
        session.commit()
    return work, branch, {
        "_researchOpsBranchId": branch["id"],
        "threadId": thread_id,
        "runtime": runtime,
        "turn": {"id": turn_id, "status": "completed"},
    }


@pytest.mark.parametrize("runtime,label", [("claude", "Claude"), ("pi", "Pi")])
async def test_external_completed_turn_queues_and_sends_once(client, settings, runtime, label):
    work, branch, params = _completed_event(client, runtime)
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub,
        notification_recipient="alice",
    )
    await events.handle_event("turn/completed", params)
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        turns = list(session.scalars(select(models.CodexTurn)))
        notices = list(session.scalars(select(models.TurnNotification)))
        assert len(turns) == len(notices) == 1
        assert turns[0].prompt == "Check the experiment"
        assert turns[0].final_answer == "Completed result"
        assert notices[0].turn_id == turns[0].id
        assert notices[0].status == "pending"
        assert notices[0].attempts == 0

    sender = FakeSender()
    service = TurnNotificationService(
        state.database.session_factory,
        settings.model_copy(update={"infoflow_notifications_enabled": True}), sender,
    )
    assert [item.status for item in service.dispatch_pending()] == ["sent"]
    assert service.dispatch_pending() == []
    assert len(sender.sent) == 1
    recipient, message = sender.sent[0]
    assert recipient == "alice"
    assert f"{label} Session ID：{params['threadId']}" in message
    assert "Codex Session ID" not in message
    assert work["human_id"] in message
    assert f"/programs/HARN/branches/{branch['id']}/results" in message
    assert "来源：Web" in message


@pytest.mark.parametrize("recipient", [None, "", "  "])
async def test_external_completion_respects_disabled_notifications(client, recipient):
    _, _, params = _completed_event(client)
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub, notification_recipient=recipient,
    )
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        assert session.scalar(select(models.CodexTurn)) is not None
        assert session.scalar(select(models.TurnNotification)) is None


@pytest.mark.parametrize("status,answer", [
    ("failed", "Partial result"), ("error", "Partial result"),
    ("interrupted", "Partial result"), ("completed", None), ("completed", "  "),
])
async def test_only_successful_nonempty_external_results_notify(client, status, answer):
    _, _, params = _completed_event(client, answer=answer)
    params["turn"]["status"] = status
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub,
        notification_recipient="alice",
    )
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        assert session.scalar(select(models.CodexTurn)) is not None
        assert session.scalar(select(models.TurnNotification)) is None


async def test_scheduled_check_keeps_its_own_notification_policy(client):
    metadata = {"scheduled_check_id": "check-1", "scheduled_check_run_id": "run-1"}
    _, _, params = _completed_event(client, metadata=metadata)
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub,
        notification_recipient="alice",
    )
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        assert all(turn.metadata_json[key] == value for key, value in metadata.items())
        assert session.scalar(select(models.TurnNotification)) is None


async def test_native_codex_notifications_still_belong_to_jsonl_sync(client):
    _, _, params = _completed_event(client, "codex")
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub,
        notification_recipient="alice",
    )
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        assert session.scalar(select(models.CodexTurn)) is None
        assert session.scalar(select(models.TurnNotification)) is None


async def test_result_and_notification_commit_together(client, monkeypatch):
    _, _, params = _completed_event(client)
    state = client.app.state.rops
    events = CodexEventService(
        state.database.session_factory, state.event_hub,
        notification_recipient="alice",
    )

    def fail_outbox(**_kwargs):
        raise RuntimeError("outbox insertion failed")

    with monkeypatch.context() as patch:
        patch.setattr(models, "TurnNotification", fail_outbox)
        with pytest.raises(RuntimeError, match="outbox insertion failed"):
            await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        assert session.scalar(select(models.CodexTurn)) is None
        assert session.scalar(select(models.TurnNotification)) is None
    await events.handle_event("turn/completed", params)
    with state.database.session_factory() as session:
        assert session.scalar(select(models.CodexTurn)) is not None
        assert session.scalar(select(models.TurnNotification)) is not None


@pytest.mark.parametrize("enabled", [False, True])
def test_app_wires_the_notification_setting_into_external_runtime_events(
    settings, fake_codex, enabled,
):
    configured = settings.model_copy(update={
        "infoflow_notifications_enabled": enabled,
        "infoflow_recipient": "alice",
    })
    app = create_app(configured, fake_codex)
    state = app.state.rops
    try:
        assert state.codex_events.notification_recipient == ("alice" if enabled else None)
    finally:
        state.database.engine.dispose()
