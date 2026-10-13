import asyncio
import socket
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from research_ops import models, schemas
from research_ops.codex.base import CodexUnavailable
from research_ops.services.codex_events import redact
from research_ops.services.core import ConflictError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_api_workflow import create_work


def test_web_runtime_has_no_default_global_capacity_gate(client) -> None:
    state = client.app.state.rops

    assert state.settings.codex_max_concurrent_turns == 0
    assert state.service._codex_turn_semaphore is None


async def test_web_heartbeat_lock_does_not_repeat_or_abort_runtime(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    service = client.app.state.rops.service
    settings.web_turn_heartbeat_seconds = 0.05
    real_heartbeat = service._heartbeat_web_attempt
    calls = 0
    runtime_calls = 0
    recovered = asyncio.Event()

    def heartbeat(branch_id, attempt_id):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        result = real_heartbeat(branch_id, attempt_id)
        if calls > 3:
            recovered_loop.call_soon_threadsafe(recovered.set)
        return result

    async def runtime(**_kwargs):
        nonlocal runtime_calls
        runtime_calls += 1
        await asyncio.wait_for(recovered.wait(), timeout=3)
        return {"turn": {"id": "after-db-retry"}}

    recovered_loop = asyncio.get_running_loop()
    monkeypatch.setattr(service, "_heartbeat_web_attempt", heartbeat)
    monkeypatch.setattr(fake_codex, "send_message", runtime)
    result = await service.send_message(branch["id"], schemas.ConversationSend(content="Run once"))
    assert result.metadata_json["send_status"] == "submitted"
    assert runtime_calls == 1
    assert calls >= 4


async def test_cancelling_web_runner_reaps_its_runtime(client, fake_codex, monkeypatch) -> None:
    _work, branch = create_work(client)
    service = client.app.state.rops.service
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def runtime(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(fake_codex, "send_message", runtime)
    task = asyncio.create_task(
        service.send_message(branch["id"], schemas.ConversationSend(content="Wait"))
    )
    await asyncio.wait_for(started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_completed_event_retries_persistence_without_duplicate(client, monkeypatch) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    events = state.codex_events
    save = events._save_completed_item
    calls = 0

    def save_with_lock(branch_id, params):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))
        return save(branch_id, params)

    monkeypatch.setattr(events, "_save_completed_item", save_with_lock)
    params = {
        "_researchOpsBranchId": branch["id"],
        "turnId": "retry-turn",
        "item": {"id": "item-1", "type": "agentMessage", "text": "Saved result"},
    }
    await events.handle_event("item/completed", params)
    await events.handle_event("item/completed", params)
    with state.database.session_factory() as session:
        messages = list(session.scalars(select(models.ConversationMessage).where(
            models.ConversationMessage.branch_id == branch["id"],
            models.ConversationMessage.external_item_id == "item-1",
        )))
        assert len(messages) == 1
        assert messages[0].content == "Saved result"


async def test_long_web_turn_recovers_and_renews_its_session(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.web_turn_heartbeat_seconds = 0.05
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send_message(**_kwargs):
        started.set()
        await release.wait()
        return {"turn": {"id": "turn-after-heartbeat"}}

    monkeypatch.setattr(fake_codex, "send_message", slow_send_message)
    task = asyncio.create_task(
        state.service.send_message(
            branch["id"], schemas.ConversationSend(content="Monitor the long run")
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    with state.database.session_factory() as session:
        attempt = session.scalar(
            select(models.SessionAttempt)
            .where(models.SessionAttempt.branch_id == branch["id"])
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )
        assert attempt
        attempt.status = "expired"
        attempt.completed_at = datetime.now(UTC)
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        attempt_id = attempt.id

    try:
        for _ in range(40):
            await asyncio.sleep(0.025)
            with state.database.session_factory() as session:
                renewed = session.get(models.SessionAttempt, attempt_id)
                assert renewed
                lease_expires_at = renewed.lease_expires_at
                if lease_expires_at.tzinfo is None:
                    lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
                if renewed.status == "active" and lease_expires_at > datetime.now(UTC):
                    break
        else:
            raise AssertionError("Web turn did not renew its expired Session")
    finally:
        release.set()

    message = await asyncio.wait_for(task, timeout=2)
    assert message.metadata_json["send_status"] == "submitted"
    with state.database.session_factory() as session:
        finished = session.get(models.SessionAttempt, attempt_id)
        assert finished
        assert finished.status == "completed"


async def test_queued_web_turn_returns_before_runtime_settles(
    client, fake_codex, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_send_message(**_kwargs):
        started.set()
        await release.wait()
        return {"turn": {"id": "queued-turn"}}

    monkeypatch.setattr(fake_codex, "send_message", slow_send_message)
    message = await asyncio.wait_for(
        state.service.queue_web_message(
            branch["id"], schemas.ConversationSend(content="Keep watching this run")
        ),
        timeout=0.25,
    )

    assert message.metadata_json["send_status"] == "pending"
    await asyncio.wait_for(started.wait(), timeout=2)
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"]["web_turn_status"] == "running"
    assert detail["conversation_state"]["can_send"] is False

    release.set()
    for _ in range(80):
        await asyncio.sleep(0.025)
        with state.database.session_factory() as session:
            stored = session.get(models.ConversationMessage, message.id)
            if stored and stored.metadata_json.get("send_status") == "submitted":
                break
    else:
        raise AssertionError("Queued Web turn did not settle")

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"]["can_send"] is True


async def test_queued_web_turn_persists_runtime_failures(
    client, fake_codex, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    started = asyncio.Event()

    async def failing_send_message(**_kwargs):
        started.set()
        raise RuntimeError("temporary runtime failure")

    monkeypatch.setattr(fake_codex, "send_message", failing_send_message)
    message = await state.service.queue_web_message(
        branch["id"], schemas.ConversationSend(content="Try the unavailable runtime")
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    for _ in range(80):
        await asyncio.sleep(0.025)
        with state.database.session_factory() as session:
            stored = session.get(models.ConversationMessage, message.id)
            if stored and stored.metadata_json.get("send_status") == "failed":
                break
    else:
        raise AssertionError("Queued Web failure was not persisted")

    detail = client.get(f"/api/branches/{branch['id']}").json()
    failed = next(item for item in detail["messages"] if item["id"] == message.id)
    assert failed["metadata_json"]["send_status"] == "failed"
    assert "temporary runtime failure" in failed["metadata_json"]["error"]
    assert detail["conversation_state"]["can_send"] is True


async def test_failed_web_turn_enqueues_infoflow_alert(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True
    settings.infoflow_error_notifications_enabled = True
    started = asyncio.Event()

    async def failing_send_message(**_kwargs):
        started.set()
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(fake_codex, "send_message", failing_send_message)
    message = await state.service.queue_web_message(
        branch["id"], schemas.ConversationSend(content="Notify on this failure")
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    for _ in range(80):
        await asyncio.sleep(0.025)
        with state.database.session_factory() as session:
            notification = session.scalar(
                select(models.ConversationFailureNotification).where(
                    models.ConversationFailureNotification.message_id == message.id
                )
            )
            if notification is not None:
                break
    else:
        raise AssertionError("Failed Web turn did not queue an Infoflow alert")

    assert notification.status == "pending"
    assert notification.recipient == settings.infoflow_recipient


async def test_web_turn_retries_model_capacity_before_failing(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.codex_capacity_retry_attempts = 2
    settings.codex_capacity_retry_initial_seconds = 0.01
    settings.codex_capacity_retry_max_seconds = 0.01
    calls = 0

    async def capacity_then_success(**_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CodexUnavailable("Selected model is at capacity")
        return {"turn": {"id": "capacity-recovered"}}

    monkeypatch.setattr(fake_codex, "send_message", capacity_then_success)
    message = await state.service.send_message(
        branch["id"], schemas.ConversationSend(content="Retry after capacity")
    )

    assert calls == 3
    assert message.metadata_json["send_status"] == "submitted"
    assert "capacity_retry" not in message.metadata_json


async def test_web_turn_retries_account_concurrency_limit(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.codex_capacity_retry_attempts = 1
    settings.codex_capacity_retry_initial_seconds = 0.01
    settings.codex_capacity_retry_max_seconds = 0.01
    calls = 0

    async def concurrency_then_success(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CodexUnavailable("Concurrency limit exceeded for user")
        return {"turn": {"id": "concurrency-recovered"}}

    monkeypatch.setattr(fake_codex, "send_message", concurrency_then_success)
    message = await state.service.send_message(
        branch["id"], schemas.ConversationSend(content="Retry after concurrency")
    )

    assert calls == 2
    assert message.metadata_json["send_status"] == "submitted"
    assert "capacity_retry" not in message.metadata_json


async def test_web_turn_retries_unstarted_gateway_disconnect(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.codex_transport_retry_attempts = 1
    settings.codex_transport_retry_initial_seconds = 0.01
    settings.codex_transport_retry_max_seconds = 0.01
    calls = 0

    async def disconnect_then_success(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CodexUnavailable(
                "stream disconnected before completion: websocket closed"
            )
        return {"turn": {"id": "transport-recovered"}}

    monkeypatch.setattr(fake_codex, "send_message", disconnect_then_success)
    message = await state.service.send_message(
        branch["id"], schemas.ConversationSend(content="Retry a dropped stream")
    )

    assert calls == 2
    assert message.metadata_json["send_status"] == "submitted"
    assert "transport_retry" not in message.metadata_json


async def test_web_turn_does_not_replay_after_execution_started(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.codex_transport_retry_attempts = 1
    calls = 0

    async def started_then_disconnect(**_kwargs):
        nonlocal calls
        calls += 1
        with state.database.session_factory() as session:
            message = session.scalar(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch["id"],
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            assert message is not None
            message.metadata_json = {
                **message.metadata_json,
                "runtime_execution_started": {"item_type": "commandExecution"},
            }
            session.commit()
        raise CodexUnavailable(
            "Request blocked: stream disconnected before completion: websocket closed"
        )

    monkeypatch.setattr(fake_codex, "send_message", started_then_disconnect)
    with pytest.raises(CodexUnavailable, match="Request blocked"):
        await state.service.send_message(
            branch["id"], schemas.ConversationSend(content="Do not replay commands")
        )

    assert calls == 1


async def test_started_codex_item_blocks_transport_replay(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.codex_transport_retry_attempts = 1
    calls = 0

    async def emit_item_then_disconnect(**kwargs):
        nonlocal calls
        calls += 1
        branch_id = kwargs["metadata"]["research_ops_branch_id"]
        await state.codex_events.handle_event(
            "turn/started",
            {
                "_researchOpsBranchId": branch_id,
                "threadId": kwargs["thread_id"],
                "turn": {"id": "event-started-turn", "status": "inProgress"},
            },
        )
        await state.codex_events.handle_event(
            "item/started",
            {
                "_researchOpsBranchId": branch_id,
                "turnId": "event-started-turn",
                "item": {"id": "command-1", "type": "commandExecution"},
            },
        )
        raise CodexUnavailable(
            "Request blocked: stream disconnected before completion: websocket closed"
        )

    monkeypatch.setattr(fake_codex, "send_message", emit_item_then_disconnect)
    with pytest.raises(CodexUnavailable, match="Request blocked"):
        await state.service.send_message(
            branch["id"], schemas.ConversationSend(content="Keep this command single-shot")
        )

    assert calls == 1
    with state.database.session_factory() as session:
        message = session.scalar(
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == branch["id"],
                models.ConversationMessage.role == "user",
            )
            .order_by(models.ConversationMessage.created_at.desc())
            .limit(1)
        )
        assert message is not None
        assert message.metadata_json["runtime_execution_started"]["item_type"] == "commandExecution"


async def test_interrupted_capacity_queued_turn_releases_its_branch_lock(
    client, fake_codex, monkeypatch
) -> None:
    _first_work, first_branch = create_work(client, "ADAPT")
    _second_work, second_branch = create_work(client, "HARN")
    state = client.app.state.rops
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def single_slot_send_message(**kwargs):
        content = str(kwargs["content"])
        calls.append(content)
        if content == "Occupy model capacity":
            first_started.set()
            await release_first.wait()
        return {"turn": {"id": f"turn-{len(calls)}"}}

    monkeypatch.setattr(fake_codex, "send_message", single_slot_send_message)
    await state.service.queue_web_message(
        first_branch["id"],
        schemas.ConversationSend(content="Occupy model capacity"),
    )
    await asyncio.wait_for(first_started.wait(), timeout=2)

    queued = await state.service.queue_web_message(
        second_branch["id"],
        schemas.ConversationSend(content="Cancel while queued"),
    )
    state.service.interrupt_web_turn(second_branch["id"], queued.id)

    for _ in range(40):
        await asyncio.sleep(0.025)
        if not state.service._web_turn_task_active(second_branch["id"]):
            break
    else:
        raise AssertionError("Interrupted queued turn retained the Branch lock")

    replacement = await asyncio.wait_for(
        state.service.queue_web_message(
            second_branch["id"],
            schemas.ConversationSend(content="Replacement after cancel"),
        ),
        timeout=0.5,
    )
    assert replacement.metadata_json["send_status"] == "pending"

    release_first.set()
    for _ in range(80):
        await asyncio.sleep(0.025)
        if "Replacement after cancel" in calls:
            break
    else:
        raise AssertionError("Replacement turn did not leave the capacity queue")


async def test_new_web_message_supersedes_the_running_web_turn(
    client, fake_codex, settings, monkeypatch
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    settings.web_turn_heartbeat_seconds = 0.05
    first_started = asyncio.Event()
    calls: list[str] = []

    async def controlled_send_message(**kwargs):
        content = str(kwargs["content"])
        calls.append(content)
        if content == "First prompt":
            first_started.set()
            await asyncio.Event().wait()
        return {"turn": {"id": f"turn-{len(calls)}"}}

    monkeypatch.setattr(fake_codex, "send_message", controlled_send_message)
    first = asyncio.create_task(
        state.service.send_message(
            branch["id"], schemas.ConversationSend(content="First prompt")
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=2)

    second = await asyncio.wait_for(
        state.service.send_message(
            branch["id"], schemas.ConversationSend(content="New direction")
        ),
        timeout=3,
    )

    with pytest.raises(ConflictError, match="interrupted"):
        await first
    assert calls == ["First prompt", "New direction"]
    assert second.metadata_json["send_status"] == "submitted"
    detail = client.get(f"/api/branches/{branch['id']}").json()
    prompts = [message for message in detail["messages"] if message["role"] == "user"]
    assert [message["metadata_json"]["send_status"] for message in prompts] == [
        "interrupted",
        "submitted",
    ]
    assert detail["conversation_state"]["can_send"] is True


def test_expired_orphaned_web_turn_releases_the_composer(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    with state.database.session_factory() as session:
        stored_attempt = session.get(models.SessionAttempt, attempt["id"])
        assert stored_attempt
        stored_attempt.status = "expired"
        stored_attempt.completed_at = datetime.now(UTC)
        stored_attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt["id"],
                role="user",
                content="This request lost its runtime",
                metadata_json={"send_status": "pending", "origin": "web"},
            )
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"]["can_send"] is True
    assert detail["conversation_state"]["web_turn_status"] == "failed"
    failed = next(message for message in detail["messages"] if message["role"] == "user")
    assert failed["metadata_json"]["send_status"] == "failed"


def test_active_pending_web_turn_handles_sqlite_naive_lease(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt["id"],
                role="user",
                content="This request still has a live runtime",
                metadata_json={"send_status": "pending", "origin": "web"},
            )
        )
        session.commit()

    response = client.get(f"/api/branches/{branch['id']}")

    assert response.status_code == 200, response.text
    pending = next(message for message in response.json()["messages"] if message["role"] == "user")
    assert pending["metadata_json"]["send_status"] == "pending"


def test_restarted_web_runtime_releases_an_unexpired_orphaned_lease(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        assert stored_branch is not None
        attempt = models.SessionAttempt(
            branch_id=branch["id"],
            codex_thread_id="019f-restarted-web-thread",
            status="active",
            lease_owner="web",
            lease_expires_at=now + timedelta(minutes=30),
            heartbeat_at=now,
            work_item_revision=stored_branch.work_item.revision,
            brief={},
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        message = models.ConversationMessage(
            branch_id=branch["id"],
            session_id=attempt.id,
            role="user",
            content="This Web runtime was restarted",
            metadata_json={"send_status": "pending", "origin": "web"},
        )
        session.add(message)
        session.commit()
        attempt_id = attempt.id
        message_id = message.id

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"]["can_send"] is True
    failed = next(item for item in detail["messages"] if item["id"] == message_id)
    assert failed["metadata_json"]["send_status"] == "failed"
    with state.database.session_factory() as session:
        stored_attempt = session.get(models.SessionAttempt, attempt_id)
        assert stored_attempt is not None
        assert stored_attempt.status == "completed"


def test_web_can_interrupt_its_pending_turn(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    with state.database.session_factory() as session:
        message = models.ConversationMessage(
            branch_id=branch["id"],
            session_id=attempt["id"],
            role="user",
            content="Stop this from the Web workspace",
            metadata_json={"send_status": "pending", "origin": "web"},
        )
        session.add(message)
        session.commit()
        message_id = message.id

    stopped = client.post(
        f"/api/branches/{branch['id']}/messages/{message_id}/interrupt"
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["metadata_json"]["send_status"] == "interrupted"
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"]["can_send"] is True
    assert detail["conversation_state"]["web_turn_status"] == "interrupted"
    assert detail["session"]["status"] == "completed"


def test_terminal_runtime_owns_lease_binds_thread_and_finishes(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops

    attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:123")
    assert attempt.status == "active"
    assert attempt.codex_thread_id is None
    with pytest.raises(ConflictError, match="already open"):
        state.service.claim_terminal_session(branch["id"], "terminal:test:456")

    binding = state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt.id,
        thread_id="019f-managed-terminal-thread",
        bound_at=attempt.started_at or datetime.now(UTC),
    )
    assert binding.branch_id == branch["id"]
    state.service.heartbeat_session(
        branch["id"],
        attempt.id,
        schemas.SessionHeartbeat(lease_id=attempt.lease_id),
    )
    finished = state.service.finish_terminal_session(
        branch["id"], attempt.id, attempt.lease_id, 0
    )

    assert finished.status == "completed"
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["branch"]["codex_thread_id"] == "019f-managed-terminal-thread"
    assert detail["session"]["lease_owner"] == "terminal:test:123"
    assert detail["work_item"]["flow"] == "actionable"
    with state.database.session_factory() as session:
        stored = session.get(models.CodexThreadBinding, binding.id)
        assert stored is not None
        assert stored.thread_id == "019f-managed-terminal-thread"

    handoff = client.post(
        f"/api/branches/{branch['id']}/sessions/{attempt.id}/handoff",
        json={
            "lease_id": attempt.lease_id,
            "outcome": "continue",
            "conclusion": "Terminal work completed",
            "next_action": "Review the result",
        },
    )
    assert handoff.status_code == 200, handoff.text
    duplicate = client.post(
        f"/api/branches/{branch['id']}/sessions/{attempt.id}/handoff",
        json={
            "lease_id": attempt.lease_id,
            "outcome": "continue",
            "conclusion": "Overwrite",
        },
    )
    assert duplicate.status_code == 409
    assert "immutable handoff" in duplicate.json()["detail"]


def test_dead_same_host_terminal_lease_is_reclaimed_immediately(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    dead_owner = f"terminal:{socket.gethostname()}:999999999"

    stale = state.service.claim_terminal_session(branch["id"], dead_owner)
    replacement = state.service.claim_terminal_session(
        branch["id"], "terminal:test:replacement"
    )

    assert replacement.status == "active"
    with state.database.session_factory() as session:
        stored = session.get(models.SessionAttempt, stale.id)
        assert stored is not None
        assert stored.status == "expired"
        assert stored.error == "Local terminal process ended; stale lease reclaimed"


def test_terminal_process_check_ignores_non_cli_owners(client) -> None:
    state = client.app.state.rops

    assert state.service._terminal_owner_process_state("terminal:test:123", "branch") is None
    assert state.service._terminal_owner_process_state("registry:test:123", "branch") is None


def test_local_pending_turn_blocks_web_input(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:local")
    binding = state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt.id,
        thread_id="019f-local-pending-thread",
        bound_at=attempt.started_at or datetime.now(UTC),
    )
    with state.database.session_factory() as session:
        session.add(
            models.CodexSyncCursor(
                thread_binding_id=binding.id,
                path="/tmp/local-rollout.jsonl",
                st_dev=1,
                st_ino=1,
                parser_version="test",
                pending_json={"turn-local": {"prompt": "Run the local verifier"}},
            )
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"] == {
        "can_send": False,
        "can_interrupt": False,
        "web_turn_status": "idle",
        "local_turn_active": True,
        "reason": "Wait for the current local CLI turn to finish",
    }
    blocked = client.post(
        f"/api/branches/{branch['id']}/messages",
        json={"content": "This must wait"},
    )
    assert blocked.status_code == 409
    assert "local CLI" in blocked.json()["detail"]


def test_stale_pending_turn_does_not_interrupt_matching_web_turn(client) -> None:
    """Historical pending entries and redacted secrets must not look local."""
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:web-match")
    binding = state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt.id,
        thread_id="019f-web-redacted-thread",
        bound_at=datetime.now(UTC),
    )
    raw_prompt = "<research_ops_brief>...</research_ops_brief>\nTOKEN=ghp_example_secret\nRun it"
    with state.database.session_factory() as session:
        session.add(
            models.CodexSyncCursor(
                thread_binding_id=binding.id,
                path="/tmp/web-redacted-rollout.jsonl",
                st_dev=1,
                st_ino=1,
                parser_version="test",
                pending_json={
                    "old-local-turn": {"prompt": "an old local prompt", "start_offset": 10},
                    "web-turn": {"prompt": raw_prompt, "start_offset": 20},
                },
            )
        )
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt.id,
                role="user",
                content=redact("TOKEN=ghp_example_secret\nRun it"),
                metadata_json={"send_status": "pending", "origin": "web"},
            )
        )
        session.commit()

    assert state.service.reconcile_web_turn_priority() == []
    with state.database.session_factory() as session:
        message = session.scalar(
            select(models.ConversationMessage).where(
                models.ConversationMessage.branch_id == branch["id"]
            )
        )
        assert message is not None
        assert message.metadata_json["send_status"] == "pending"


def test_finished_local_runtime_clears_a_stale_pending_cursor(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = state.service.claim_terminal_session(
        branch["id"], "terminal:test:finished-local"
    )
    binding = state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt.id,
        thread_id="019f-finished-local-thread",
        bound_at=attempt.started_at or datetime.now(UTC),
    )
    with state.database.session_factory() as session:
        cursor = models.CodexSyncCursor(
            thread_binding_id=binding.id,
            path="/tmp/finished-local-rollout.jsonl",
            st_dev=1,
            st_ino=1,
            parser_version="test",
            pending_json={"turn-stale": {"prompt": "The terminal already exited"}},
        )
        session.add(cursor)
        now = datetime.now(UTC)
        session.add(
            models.CodexWindow(
                thread_id=binding.thread_id,
                cwd=branch["cwd"],
                rollout_path=cursor.path,
                live=True,
                pid=None,
                match_confidence="managed_lease",
                branch_id=branch["id"],
                session_attempt_id=attempt.id,
                last_activity_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
        cursor_id = cursor.id
    state.service.finish_terminal_session(
        branch["id"], attempt.id, attempt.lease_id, 0
    )

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["conversation_state"] == {
        "can_send": True,
        "can_interrupt": False,
        "web_turn_status": "idle",
        "local_turn_active": False,
        "reason": None,
    }
    lightweight = client.get(
        f"/api/branches/{branch['id']}/conversation-state"
    )
    assert lightweight.status_code == 200, lightweight.text
    assert lightweight.json() == detail["conversation_state"]
    with state.database.session_factory() as session:
        stored = session.get(models.CodexSyncCursor, cursor_id)
        assert stored
        assert stored.pending_json == {}


def test_active_web_turn_keeps_its_pending_cursor_for_live_activity(client) -> None:
    """A managed Web turn has no local TUI, but its JSONL tail is still live."""
    _work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        assert stored_branch is not None
        attempt = models.SessionAttempt(
            branch_id=branch["id"],
            codex_thread_id="019f-active-web-thread",
            status="active",
            lease_owner="web",
            lease_expires_at=now + timedelta(minutes=10),
            heartbeat_at=now,
            work_item_revision=stored_branch.work_item.revision,
            brief={},
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            initial_session_id=attempt.id,
            thread_id=attempt.codex_thread_id,
            bound_at=now,
        )
        session.add(binding)
        session.flush()
        cursor = models.CodexSyncCursor(
            thread_binding_id=binding.id,
            path="/tmp/active-web-rollout.jsonl",
            st_dev=1,
            st_ino=1,
            parser_version="test",
            pending_json={"turn-web": {"prompt": "Keep watching the active run"}},
        )
        session.add(cursor)
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt.id,
                role="user",
                content="Keep watching the active run",
                metadata_json={"send_status": "pending", "origin": "web"},
            )
        )
        session.commit()
        cursor_id = cursor.id

    assert state.service.reconcile_web_turn_priority() == []
    with state.database.session_factory() as session:
        stored = session.get(models.CodexSyncCursor, cursor_id)
        assert stored is not None
        assert stored.pending_json == {
            "turn-web": {"prompt": "Keep watching the active run"}
        }


def test_interrupted_web_session_does_not_lock_the_local_cli_composer(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        assert stored_branch is not None
        attempt = models.SessionAttempt(
            branch_id=branch["id"],
            codex_thread_id="019f-interrupted-web-thread",
            status="active",
            lease_owner="web",
            lease_expires_at=now + timedelta(minutes=10),
            heartbeat_at=now,
            work_item_revision=stored_branch.work_item.revision,
            brief={},
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            initial_session_id=attempt.id,
            thread_id=attempt.codex_thread_id,
            bound_at=now,
        )
        session.add(binding)
        session.flush()
        cursor = models.CodexSyncCursor(
            thread_binding_id=binding.id,
            path="/tmp/interrupted-web-rollout.jsonl",
            st_dev=1,
            st_ino=1,
            parser_version="test",
            pending_json={"turn-web": {"prompt": "The interrupted Web prompt"}},
        )
        session.add(cursor)
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt.id,
                role="user",
                content="The interrupted Web prompt",
                metadata_json={
                    "origin": "web",
                    "send_status": "interrupted",
                    "error": "Interrupted from the Web workspace",
                },
            )
        )
        session.commit()
        attempt_id = attempt.id
        cursor_id = cursor.id

    detail = client.get(f"/api/branches/{branch['id']}").json()

    assert detail["conversation_state"] == {
        "can_send": True,
        "can_interrupt": False,
        "web_turn_status": "interrupted",
        "local_turn_active": False,
        "reason": None,
    }
    assert detail["session"]["status"] == "completed"
    with state.database.session_factory() as session:
        attempt = session.get(models.SessionAttempt, attempt_id)
        cursor = session.get(models.CodexSyncCursor, cursor_id)
        assert attempt and attempt.status == "completed"
        assert cursor and cursor.pending_json == {}


def test_branch_detail_limits_initial_message_history(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        session.add_all(
            [
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="assistant",
                    content=f"message-{index:03d}",
                    metadata_json={},
                )
                for index in range(250)
            ]
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()

    assert len(detail["messages"]) == 200
    assert detail["messages"][0]["content"] == "message-050"
    assert detail["messages"][-1]["content"] == "message-249"


def test_branch_detail_keeps_interrupted_queries_outside_message_window(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="user",
                content="The query that was interrupted",
                metadata_json={
                    "origin": "web",
                    "send_status": "interrupted",
                    "error": "Interrupted from the Web workspace",
                },
                created_at=base,
            )
        )
        session.add_all(
            [
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="assistant",
                    content=f"activity-{index:03d}",
                    metadata_json={},
                    created_at=base + timedelta(seconds=index + 1),
                )
                for index in range(250)
            ]
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()

    messages = detail["messages"]
    interrupted = [
        message
        for message in messages
        if message["metadata_json"].get("send_status") == "interrupted"
    ]
    assert len(messages) == 201
    assert len(interrupted) == 1
    assert interrupted[0]["content"] == "The query that was interrupted"


def test_branch_detail_bounds_extra_terminal_queries(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    base = datetime(2025, 1, 1, tzinfo=UTC)
    with state.database.session_factory() as session:
        session.add_all(
            [
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="user",
                    content=f"interrupted-{index:02d}",
                    metadata_json={"origin": "web", "send_status": "interrupted"},
                    created_at=base + timedelta(seconds=index),
                )
                for index in range(30)
            ]
        )
        session.add_all(
            [
                models.ConversationMessage(
                    branch_id=branch["id"],
                    role="assistant",
                    content=f"activity-{index:03d}",
                    metadata_json={},
                    created_at=base + timedelta(seconds=100 + index),
                )
                for index in range(200)
            ]
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()

    interrupted = [
        message
        for message in detail["messages"]
        if message["metadata_json"].get("send_status") == "interrupted"
    ]
    assert len(detail["messages"]) == 224
    assert len(interrupted) == 24
    assert interrupted[0]["content"] == "interrupted-06"
    assert interrupted[-1]["content"] == "interrupted-29"


def test_cli_claim_interrupts_pending_web_message(client) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt["id"],
                role="user",
                content="A Web turn in flight",
                metadata_json={"send_status": "pending", "origin": "web"},
            )
        )
        session.commit()

    claimed = state.service.claim_terminal_session(branch["id"], "terminal:test:priority")
    assert claimed.status == "active"
    with state.database.session_factory() as session:
        message = session.scalar(
            select(models.ConversationMessage).where(
                models.ConversationMessage.branch_id == branch["id"]
            )
        )
        assert message is not None
        assert message.metadata_json["send_status"] == "interrupted"
        assert "local CLI" in message.metadata_json["error"]
