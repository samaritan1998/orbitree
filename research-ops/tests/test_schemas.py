from datetime import datetime

from research_ops.schemas import ActivityRead, ConversationMessageRead


def test_orm_response_timestamps_are_explicit_utc() -> None:
    naive = datetime(2026, 7, 17, 5, 48, 15, 601113)
    message = ConversationMessageRead(
        id="message-1",
        branch_id="branch-1",
        session_id=None,
        turn_id=None,
        external_item_id=None,
        role="assistant",
        kind="message",
        content="update",
        metadata_json={},
        created_at=naive,
    )
    activity = ActivityRead(
        id="event-1",
        scope_type="branch",
        scope_id="branch-1",
        event_type="codex.update",
        actor="codex",
        correlation_id=None,
        payload={},
        created_at=naive,
    )

    assert message.model_dump(mode="json")["created_at"] == "2026-07-17T05:48:15.601113Z"
    assert activity.model_dump(mode="json")["created_at"] == "2026-07-17T05:48:15.601113Z"
