from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from research_ops import models
from research_ops.infoflow import InfoflowSendResult
from research_ops.services.turn_notifications import TurnNotificationService
from sqlalchemy import select
from test_api_workflow import create_work


class FakeSender:
    configured = True

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, str]] = []

    def send_text(self, recipient: str, content: str) -> InfoflowSendResult:
        self.sent.append((recipient, content))
        if self.error:
            raise RuntimeError(self.error)
        return InfoflowSendResult(message_id="message-1")


def _attempt_and_binding(
    session,
    branch: dict,
    *,
    owner: str,
) -> tuple[models.SessionAttempt, models.CodexThreadBinding]:
    now = datetime.now(UTC)
    work = session.get(models.WorkItem, branch["work_item_id"])
    assert work is not None
    attempt = models.SessionAttempt(
        branch_id=branch["id"],
        codex_thread_id=f"thread-{branch['id']}",
        status="completed",
        lease_owner=owner,
        lease_expires_at=now + timedelta(minutes=45),
        heartbeat_at=now,
        work_item_revision=work.revision,
        brief={},
        started_at=now,
        completed_at=now,
    )
    session.add(attempt)
    session.flush()
    binding = models.CodexThreadBinding(
        branch_id=branch["id"],
        initial_session_id=attempt.id,
        thread_id=attempt.codex_thread_id,
        source="test",
        bound_at=now,
    )
    session.add(binding)
    session.flush()
    return attempt, binding


@pytest.mark.parametrize(
    "public_base_url",
    [
        "http://research-ops.example",
        "https://research.example.com",
        "https://research.example.com/",
    ],
)
def test_completed_turn_notification_is_sent_once(client, settings, public_base_url) -> None:
    work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt, binding = _attempt_and_binding(session, branch, owner="web")
        turn = models.CodexTurn(
            branch_id=branch["id"],
            session_id=attempt.id,
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="notification-turn",
            status="completed",
            prompt="Run it",
            final_answer="The experiment completed successfully.",
            started_at=datetime(2026, 7, 20, 23, 59, tzinfo=UTC),
            completed_at=datetime(2026, 7, 21, 0, 1, tzinfo=UTC),
            metadata_json={},
        )
        session.add(turn)
        session.flush()
        session.add(
            models.TurnNotification(
                turn_id=turn.id,
                channel="infoflow",
                recipient="alice",
            )
        )
        session.commit()

    configured = settings.model_copy(
        update={
            "infoflow_notifications_enabled": True,
            "infoflow_public_base_url": public_base_url,
        }
    )
    sender = FakeSender()
    service = TurnNotificationService(state.database.session_factory, configured, sender)

    assert [item.status for item in service.dispatch_pending()] == ["sent"]
    assert service.dispatch_pending() == []
    assert len(sender.sent) == 1
    recipient, message = sender.sent[0]
    assert recipient == "alice"
    assert f"[HARN] {work['human_id']} · Verifier experiment" in message
    assert "Branch：Main" in message
    assert f"Codex Session ID：thread-{branch['id']}" in message
    assert "来源：Web" in message
    assert "完成时间：2026-07-21 08:01" in message
    assert "The experiment completed successfully." in message
    assert (
        f"{public_base_url.rstrip('/')}/programs/HARN/branches/{branch['id']}/results"
        in message
    )
    with state.database.session_factory() as session:
        notification = session.scalar(select(models.TurnNotification))
        assert notification is not None
        assert notification.status == "sent"
        assert notification.attempts == 1
        assert notification.provider_message_id == "message-1"
        events = list(
            session.scalars(
                select(models.ActivityEvent).where(
                    models.ActivityEvent.event_type == "notification.infoflow.sent"
                )
            )
        )
        assert len(events) == 1


def test_notification_failure_is_retried_without_changing_turn(client, settings) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt, binding = _attempt_and_binding(
            session,
            branch,
            owner="terminal:test:1",
        )
        turn = models.CodexTurn(
            branch_id=branch["id"],
            session_id=attempt.id,
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="failed-notification-turn",
            status="completed",
            final_answer="Durable result",
            completed_at=datetime.now(UTC),
            metadata_json={},
        )
        session.add(turn)
        session.flush()
        session.add(
            models.TurnNotification(
                turn_id=turn.id,
                channel="infoflow",
                recipient="alice",
            )
        )
        session.commit()
        turn_id = turn.id

    configured = settings.model_copy(update={"infoflow_notifications_enabled": True})
    service = TurnNotificationService(
        state.database.session_factory,
        configured,
        FakeSender("temporary provider failure"),
    )
    result = service.dispatch_pending()

    assert [item.status for item in result] == ["failed"]
    with state.database.session_factory() as session:
        notification = session.scalar(select(models.TurnNotification))
        turn = session.get(models.CodexTurn, turn_id)
        assert notification is not None
        assert notification.status == "failed"
        assert notification.attempts == 1
        assert notification.next_attempt_at is not None
        assert notification.last_error == "temporary provider failure"
        assert turn is not None
        assert turn.status == "completed"
        assert turn.final_answer == "Durable result"


@pytest.mark.parametrize(
    "goal_status,prompt_kind,replaced,suppressed",
    [
        ("paused", "goal_continuation", False, True),
        ("cleared", "goal_continuation", False, True),
        ("active", "goal_continuation", True, True),
        ("active", "goal_continuation", False, False),
        ("blocked", "goal_continuation", False, False),
        ("complete", "goal_continuation", False, False),
        ("paused", "user", False, False),
    ],
)
def test_goal_turn_notice_checks_current_goal_at_dispatch(
    client, settings, goal_status, prompt_kind, replaced, suppressed
) -> None:
    _work, branch = create_work(client)
    factory = client.app.state.rops.database.session_factory
    with factory() as session:
        attempt, binding = _attempt_and_binding(session, branch, owner="web")
        session.get(models.ExecutionBranch, branch["id"]).codex_thread_id = (
            "replacement-thread" if replaced else binding.thread_id
        )
        turn = models.CodexTurn(
            branch_id=branch["id"],
            session_id=attempt.id if prompt_kind == "user" else None,
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="goal-notification-turn",
            status="completed",
            final_answer="Recorded result",
            completed_at=datetime.now(UTC),
            metadata_json={"prompt_kind": prompt_kind},
        )
        session.add(turn)
        session.flush()
        session.add(models.TurnNotification(
            turn_id=turn.id, channel="infoflow", recipient="alice"
        ))
        session.commit()
        turn_id = turn.id
        # The pause can arrive after the result was queued but before delivery.
        session.add(models.NativeGoalMonitor(
            branch_id=branch["id"],
            thread_id=binding.thread_id,
            goal_key="goal-key",
            goal_snapshot={} if goal_status == "cleared" else {"status": goal_status},
            execution_status=goal_status,
        ))
        session.commit()

    configured = settings.model_copy(update={"infoflow_notifications_enabled": True})
    sender = FakeSender()
    service = TurnNotificationService(factory, configured, sender)
    service.dispatch_pending()
    assert len(sender.sent) == (0 if suppressed else 1)
    with factory() as session:
        notice = session.scalar(select(models.TurnNotification))
        assert notice.status == ("suppressed" if suppressed else "sent")
        assert notice.attempts == (0 if suppressed else 1)
        assert session.get(models.CodexTurn, turn_id).final_answer == "Recorded result"
    if not suppressed and prompt_kind == "goal_continuation":
        assert "Native Goal" in sender.sent[0][1]
        assert "unknown" not in sender.sent[0][1]


def test_failed_web_conversation_notification_is_sent_once(client, settings) -> None:
    work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt, _binding = _attempt_and_binding(session, branch, owner="web")
        message = models.ConversationMessage(
            branch_id=branch["id"],
            session_id=attempt.id,
            role="user",
            kind="message",
            content="This private query must not appear in the notification.",
            metadata_json={
                "origin": "web",
                "send_status": "failed",
                "error": "Selected model is at capacity",
            },
        )
        session.add(message)
        session.flush()
        session.add(
            models.ConversationFailureNotification(
                message_id=message.id,
                channel="infoflow",
                recipient="alice",
            )
        )
        session.commit()
        message_id = message.id

    configured = settings.model_copy(
        update={
            "infoflow_notifications_enabled": True,
            "infoflow_error_notifications_enabled": True,
            "infoflow_public_base_url": "http://research-ops.example",
        }
    )
    sender = FakeSender()
    service = TurnNotificationService(state.database.session_factory, configured, sender)

    assert [item.status for item in service.dispatch_pending()] == ["sent"]
    assert service.dispatch_pending() == []
    assert len(sender.sent) == 1
    recipient, content = sender.sent[0]
    assert recipient == "alice"
    assert "Orbitree · 对话失败" in content
    assert f"[HARN] {work['human_id']} · Verifier experiment" in content
    assert "Branch：Main" in content
    assert "来源：Web" in content
    assert "原因：Selected model is at capacity" in content
    assert "This private query must not appear" not in content
    assert (
        f"http://research-ops.example/programs/HARN/branches/{branch['id']}/results"
        in content
    )
    with state.database.session_factory() as session:
        notification = session.scalar(
            select(models.ConversationFailureNotification).where(
                models.ConversationFailureNotification.message_id == message_id
            )
        )
        assert notification is not None
        assert notification.status == "sent"
        assert notification.attempts == 1
        events = list(
            session.scalars(
                select(models.ActivityEvent).where(
                    models.ActivityEvent.event_type == "notification.infoflow.failure_sent"
                )
            )
        )
        assert len(events) == 1


def test_failed_web_conversation_notification_can_be_disabled(client, settings) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt, _binding = _attempt_and_binding(session, branch, owner="web")
        message = models.ConversationMessage(
            branch_id=branch["id"],
            session_id=attempt.id,
            role="user",
            kind="message",
            content="An unavailable runtime",
            metadata_json={
                "origin": "web",
                "send_status": "failed",
                "error": "runtime error",
            },
        )
        session.add(message)
        session.flush()
        session.add(
            models.ConversationFailureNotification(
                message_id=message.id,
                channel="infoflow",
                recipient="alice",
            )
        )
        session.commit()

    configured = settings.model_copy(
        update={
            "infoflow_notifications_enabled": True,
            "infoflow_error_notifications_enabled": False,
        }
    )
    sender = FakeSender()
    service = TurnNotificationService(state.database.session_factory, configured, sender)

    assert service.dispatch_pending() == []
    assert sender.sent == []
    with state.database.session_factory() as session:
        notification = session.scalar(select(models.ConversationFailureNotification))
        assert notification is not None
        assert notification.status == "pending"
