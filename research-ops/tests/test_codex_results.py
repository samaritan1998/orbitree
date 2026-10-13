import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_ops import models
from research_ops.codex.rollout import CompletedTurn, read_rollout, user_visible_prompt
from research_ops.services.codex_results import CodexResultSync
from sqlalchemy import select
from test_api_workflow import create_work


def test_binding_accepts_second_precision_turn_started_in_same_second() -> None:
    binding = SimpleNamespace(bound_at=datetime(2026, 7, 18, 9, 2, 39, 386190, tzinfo=UTC))
    turn = CompletedTurn(
        external_turn_id="same-second",
        prompt="child request",
        final_answer="child result",
        started_at=datetime(2026, 7, 18, 9, 2, 39, tzinfo=UTC),
        completed_at=datetime(2026, 7, 18, 9, 2, 40, tzinfo=UTC),
        duration_ms=None,
        time_to_first_token_ms=None,
        end_offset=1,
    )

    assert CodexResultSync._belongs_to_binding(turn, binding) is True


def test_rollout_reads_new_response_item_user_messages_and_skips_environment_context(
    tmp_path: Path,
) -> None:
    thread_id = "response-item-thread"
    path = tmp_path / "rollout.jsonl"
    _write_records(
        path,
        [
            _session_meta(thread_id, str(tmp_path)),
            _event("task_started", turn_id="response-item-turn", started_at=1),
            _response_user(
                "<environment_context>\n  <current_date>2026-08-03</current_date>\n"
                "</environment_context>"
            ),
            _response_user(
                "<research_ops_policy>internal</research_ops_policy>\n\nShow the real query"
            ),
            _event(
                "task_complete",
                turn_id="response-item-turn",
                last_agent_message="Done",
                completed_at=2,
            ),
        ],
    )

    batch = read_rollout(path)

    assert batch.turns[0].prompt == "Show the real query"


def test_sync_skips_oversized_tool_record_and_keeps_result_and_notification(
    client, settings
) -> None:
    _work, branch = create_work(client)
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="oversized-turn", started_at=now),
            _event("user_message", message="Keep parsing after the large tool output"),
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "x" * 4096},
            },
            _event(
                "task_complete",
                turn_id="oversized-turn",
                last_agent_message="Recovered final result",
                completed_at=now + 1,
            ),
        ],
    )

    sync = CodexResultSync(
        client.app.state.rops.database.session_factory,
        settings.codex_home / "sessions",
        max_line_bytes=1024,
        notification_recipient="alice",
    )
    synced = sync.sync_all()

    assert [item.turn.final_answer for item in synced] == ["Recovered final result"]
    with client.app.state.rops.database.session_factory() as session:
        cursor = session.scalar(select(models.CodexSyncCursor))
        binding = session.scalar(select(models.CodexThreadBinding))
        notifications = list(session.scalars(select(models.TurnNotification)))
        assert cursor is not None
        assert cursor.byte_offset == rollout.stat().st_size
        assert cursor.last_error == (
            "Skipped 1 oversized Codex JSONL event(s) above 1024 bytes"
        )
        assert binding is not None
        assert binding.last_error == cursor.last_error
        assert len(notifications) == 1


def test_user_visible_prompt_strips_web_control_blocks_and_agents_context() -> None:
    prompt = user_visible_prompt(
        "<research_ops_policy>internal</research_ops_policy>\n\n"
        "<research_ops_handoff_policy>internal</research_ops_handoff_policy>\n\n"
        "<research_ops_web_turn_limits>internal</research_ops_web_turn_limits>\n\n"
        "<research_ops_brief>internal</research_ops_brief>\n\n"
        "Show the actual browser query"
    )
    agents_context = user_visible_prompt(
        "# AGENTS.md instructions for /workspace/example\n\n"
        "<INSTRUCTIONS>Internal workspace instructions</INSTRUCTIONS>"
    )

    assert prompt == "Show the actual browser query"
    assert agents_context is None


def test_rollout_preserves_precise_envelope_time_and_goal_provenance(tmp_path: Path) -> None:
    path = tmp_path / "goal.jsonl"
    start = _event("task_started", turn_id="goal-turn", started_at=1789439467)
    start["timestamp"] = "2026-09-15T02:31:07.687Z"
    records = [
        _session_meta("thread", str(tmp_path)), start,
        _response_user('<codex_internal_context source="goal">'
                       '<objective>Finish the experiment</objective></codex_internal_context>'),
    ]
    _write_records(path, records)
    partial = read_rollout(path)
    assert partial.pending_state["goal-turn"]["prompt_kind"] == "goal_continuation"
    _append_records(path, [_event(
        "task_complete", turn_id="goal-turn", completed_at=1789439500,
        last_agent_message="Experiment update",
    )])
    turn = read_rollout(path, offset=partial.next_offset,
                        pending_state=partial.pending_state).turns[0]
    assert turn.started_at == datetime(2026, 9, 15, 2, 31, 7, 687000, tzinfo=UTC)
    assert turn.prompt_kind == "goal_continuation"
    assert turn.prompt == "Continue objective: Finish the experiment"


def test_human_query_after_goal_context_is_not_a_goal_continuation(tmp_path: Path) -> None:
    path = tmp_path / "human.jsonl"
    _write_records(path, [
        _session_meta("thread", str(tmp_path)),
        _event("task_started", turn_id="turn", started_at=1),
        _response_user('<codex_internal_context source="goal">'
                       '<objective>Old objective</objective></codex_internal_context>'),
        _response_user("Continue objective: this is literal user input"),
        _event("task_complete", turn_id="turn", completed_at=2, last_agent_message="Answer"),
    ])
    turn = read_rollout(path).turns[0]
    assert turn.prompt_kind == "user"
    assert turn.prompt == "Continue objective: this is literal user input"


@pytest.mark.parametrize("precise_envelope", [True, False])
def test_query_pairing_and_historical_repair_do_not_borrow_previous_attempt(
    client, settings, precise_envelope: bool,
) -> None:
    _work, branch = create_work(client)
    first = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = first["codex_thread_id"]
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=10)
    factory = client.app.state.rops.database.session_factory
    with factory() as session:
        old = session.get(models.SessionAttempt, first["id"])
        old.status = "completed"
        old.completed_at = start - timedelta(seconds=1)
        second = models.SessionAttempt(
            branch_id=branch["id"], codex_thread_id=thread_id, status="completed",
            started_at=start + timedelta(milliseconds=313),
            completed_at=start + timedelta(seconds=5),
            heartbeat_at=start, lease_expires_at=start + timedelta(minutes=5),
            work_item_revision=1,
        )
        session.add(second)
        session.flush()
        second_id = second.id
        for attempt_id, prompt, at in [
            (old.id, "Previous query", start - timedelta(seconds=2)),
            (second.id, "Where is the performance bottleneck?",
             start + timedelta(milliseconds=344)),
        ]:
            session.add(models.ConversationMessage(
                branch_id=branch["id"], session_id=attempt_id, turn_id=f"tracking-{attempt_id}",
                role="user", kind="message", content=prompt, created_at=at,
                metadata_json={"origin": "web", "send_status": "completed"},
            ))
        session.commit()
    task_start = _event("task_started", turn_id="native-human", started_at=int(start.timestamp()))
    task_start["timestamp"] = (
        start + timedelta(milliseconds=687) if precise_envelope else start
    ).isoformat()
    path = _rollout_path(settings.codex_home, thread_id)
    _write_records(path, [
        _session_meta(thread_id, branch["cwd"]), task_start,
        _response_user("<research_ops_handoff_policy>internal</research_ops_handoff_policy>\n"
                       "Where is the performance bottleneck?"),
        _event("task_complete", turn_id="native-human", completed_at=int(start.timestamp()) + 4,
               last_agent_message="The backward pass is the bottleneck."),
        _event("task_started", turn_id="native-goal", started_at=int(start.timestamp()) + 10),
        _response_user('<codex_internal_context source="goal">'
                       '<objective>Complete four rounds</objective></codex_internal_context>'),
        _event("task_complete", turn_id="native-goal", completed_at=int(start.timestamp()) + 20,
               last_agent_message="Round 1 complete."),
    ])
    sync = CodexResultSync(factory, settings.codex_home / "sessions",
                           notification_recipient="test-recipient")
    synced = sync.sync_all()
    assert len(synced) == 2
    human, goal = (item.turn for item in synced)
    assert human.session_id == second_id
    assert human.prompt == "Where is the performance bottleneck?"
    assert human.metadata_json["prompt_source"] == "codex_session_jsonl"
    assert goal.session_id is None
    assert goal.prompt == "Continue objective: Complete four rounds"
    assert goal.metadata_json["prompt_kind"] == "goal_continuation"
    with factory() as session:
        before_answers = {t.id: t.final_answer for t in session.scalars(select(models.CodexTurn))}
        for turn in session.scalars(select(models.CodexTurn)):
            turn.prompt = "Previous query"
            turn.session_id = first["id"]
            turn.started_at = None
            turn.metadata_json = {"source": "codex_session_jsonl"}
        session.commit()
    assert sync.backfill_missing_prompts(branch_id=branch["id"], repair_mismatched=True) == 2
    assert sync.backfill_missing_prompts(branch_id=branch["id"], repair_mismatched=True) == 0
    with factory() as session:
        turns = list(session.scalars(
            select(models.CodexTurn).order_by(models.CodexTurn.completed_at)
        ))
        assert {t.id: t.final_answer for t in turns} == before_answers
        assert turns[0].session_id == second_id
        assert turns[0].prompt == "Where is the performance bottleneck?"
        assert turns[1].session_id is None
        assert turns[1].metadata_json["prompt_kind"] == "goal_continuation"
        assert len(list(session.scalars(select(models.ConversationMessage)))) == 2
        assert len(list(session.scalars(select(models.TurnNotification)))) == 2


def test_exact_turn_query_wins_and_ambiguous_session_fallback_is_rejected(client, settings) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    with client.app.state.rops.database.session_factory() as session:
        for tid, prompt in [("first", "First query"), ("second", "Second query")]:
            session.add(models.ConversationMessage(
                branch_id=branch["id"], session_id=attempt["id"], turn_id=tid,
                role="user", kind="message", content=prompt, metadata_json={"origin": "web"},
            ))
        session.commit()
        resolve = CodexResultSync._resolved_prompt
        assert resolve(session, branch_id=branch["id"], external_turn_id="second",
                       session_id=attempt["id"], rollout_prompt="Wrapped input") == (
            "Second query", "conversation_message_turn"
        )
        assert resolve(session, branch_id=branch["id"], external_turn_id="unknown",
                       session_id=attempt["id"], rollout_prompt=None) == (None, None)
    sync = CodexResultSync(client.app.state.rops.database.session_factory,
                           settings.codex_home / "sessions")
    with pytest.raises(ValueError, match="explicit Branch"):
        sync.backfill_missing_prompts(repair_mismatched=True)


def test_internal_goal_context_becomes_a_visible_continuation_query() -> None:
    prompt = user_visible_prompt(
        '<codex_internal_context source="goal">'
        "<objective>Run the packing experiment</objective>"
        "</codex_internal_context>"
    )

    assert prompt == "Continue objective: Run the packing experiment"


def test_observed_model_survives_incremental_turn_context(tmp_path: Path) -> None:
    path = tmp_path / "rollout-model.jsonl"
    records = [
        _session_meta("model-thread", str(tmp_path)),
        _event("task_started", turn_id="model-turn", started_at=1),
        {"type": "turn_context", "payload": {
            "turn_id": "model-turn", "model": "selected-in-tui", "effort": "xhigh",
        }},
    ]
    _write_records(path, records)
    first = read_rollout(path)
    _write_records(path, [*records, _event(
        "task_complete", turn_id="model-turn", last_agent_message="Done", completed_at=2,
    )])
    completed = read_rollout(path, offset=first.next_offset, pending_state=first.pending_state)
    assert completed.turns[0].model == "selected-in-tui"
    assert completed.turns[0].reasoning_effort == "xhigh"


def test_turn_context_keeps_overlapping_compaction_query_on_the_right_turn(
    tmp_path: Path,
) -> None:
    thread_id = "compaction-thread"
    path = tmp_path / "rollout.jsonl"
    _write_records(
        path,
        [
            _session_meta(thread_id, str(tmp_path)),
            _event("task_started", turn_id="compact-turn", started_at=1),
            _event("task_started", turn_id="next-turn", started_at=2),
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "turn_context",
                "payload": {"turn_id": "compact-turn"},
            },
            _response_user("Respond with exactly COMPACT_OK. Do not call tools."),
            _event(
                "task_complete",
                turn_id="compact-turn",
                last_agent_message="COMPACT_OK",
                completed_at=3,
            ),
        ],
    )

    batch = read_rollout(path)

    assert batch.turns[0].external_turn_id == "compact-turn"
    assert batch.turns[0].prompt == "Respond with exactly COMPACT_OK. Do not call tools."


def test_sync_uses_real_turn_times_and_persists_partial_turn_cursor(
    client, settings, tmp_path: Path
) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    bound_epoch = int(datetime.now(UTC).timestamp())
    records = [
        _session_meta(thread_id, branch["cwd"], forked_from_id="parent-thread"),
        _event("task_started", turn_id="parent-turn", started_at=1),
        _event("user_message", message="Inherited parent request"),
        _event(
            "task_complete",
            turn_id="parent-turn",
            last_agent_message="Inherited parent result",
            completed_at=2,
        ),
        _event("task_started", turn_id="parent-incomplete", started_at=3),
        _event("user_message", message="Inherited unfinished request"),
        _event("task_started", turn_id="turn-1", started_at=bound_epoch + 2),
        _event("user_message", message="Run the child experiment"),
        _event(
            "task_complete",
            turn_id="turn-1",
            last_agent_message="Final result",
            completed_at=bound_epoch + 5,
            duration_ms=3000,
        ),
    ]
    _write_records(rollout, records)

    sync = CodexResultSync(
        client.app.state.rops.database.session_factory,
        settings.codex_home / "sessions",
        notification_recipient="alice",
    )
    synced = sync.sync_all()

    assert [item.turn.final_answer for item in synced] == ["Final result"]
    assert synced[0].turn.prompt == "Run the child experiment"
    with client.app.state.rops.database.session_factory() as session:
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert cursor is not None
        assert cursor.pending_json == {}
        notification = session.scalar(select(models.TurnNotification))
        assert notification is not None
        assert notification.turn_id == synced[0].turn.id
        assert notification.recipient == "alice"
    assert sync.sync_all() == []

    _append_records(
        rollout,
        [
            _event("task_started", turn_id="turn-2", started_at=bound_epoch + 10),
            _event("user_message", message="Continue the experiment"),
        ],
    )
    assert sync.sync_all() == []
    with client.app.state.rops.database.session_factory() as session:
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert cursor is not None
        assert cursor.byte_offset == rollout.stat().st_size
        assert cursor.pending_json["turn-2"]["prompt"] == "Continue the experiment"

    _append_records(
        rollout,
        [
            _event(
                "task_complete",
                turn_id="turn-2",
                last_agent_message="Second result",
                completed_at=bound_epoch + 15,
            )
        ],
    )
    assert [item.turn.final_answer for item in sync.sync_all()] == ["Second result"]
    with client.app.state.rops.database.session_factory() as session:
        turns = list(
            session.scalars(select(models.CodexTurn).order_by(models.CodexTurn.completed_at))
        )
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert [turn.external_turn_id for turn in turns] == ["turn-1", "turn-2"]
        assert cursor is not None
        assert cursor.byte_offset == rollout.stat().st_size
        assert cursor.pending_json == {}
        notifications = list(session.scalars(select(models.TurnNotification)))
        assert len(notifications) == 2


def test_backfill_missing_prompts_recovers_the_user_visible_query(client, settings) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="turn-backfill", started_at=now + 1),
            _event(
                "user_message",
                message=(
                    "<research_ops_policy>internal policy</research_ops_policy>\n\n"
                    "Show this query in Results"
                ),
            ),
            _event(
                "task_complete",
                turn_id="turn-backfill",
                last_agent_message="Result",
                completed_at=now + 2,
            ),
        ],
    )
    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    assert len(sync.sync_all()) == 1
    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        turn.prompt = None
        session.commit()

    assert sync.backfill_missing_prompts(branch_id=branch["id"]) == 1
    assert sync.take_backfilled_turn_keys() == {(branch["id"], "turn-backfill")}
    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        assert turn.prompt == "Show this query in Results"


def test_backfill_uses_durable_web_query_when_jsonl_has_no_user_record(client, settings) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="web-fallback", started_at=now + 1),
            _event(
                "task_complete",
                turn_id="web-fallback",
                last_agent_message="Result without a JSONL user record",
                completed_at=now + 2,
            ),
        ],
    )
    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    assert len(sync.sync_all()) == 1
    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt["id"],
                turn_id="web-fallback",
                role="user",
                kind="message",
                content="Durable Web query",
                metadata_json={"origin": "web", "send_status": "submitted"},
            )
        )
        session.commit()

    assert sync.backfill_missing_prompts(branch_id=branch["id"]) == 1
    assert sync.take_backfilled_turn_keys() == {(branch["id"], "web-fallback")}
    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        assert turn.prompt == "Durable Web query"


def test_sync_and_backfill_prefer_web_session_query_over_agents_context(client, settings) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="native-codex-turn", started_at=now + 1),
            _event(
                "user_message",
                message=(
                    "# AGENTS.md instructions for /workspace/example\n\n"
                    "<INSTRUCTIONS>Internal workspace instructions</INSTRUCTIONS>"
                ),
            ),
            _event(
                "task_complete",
                turn_id="native-codex-turn",
                last_agent_message="Result from the native turn",
                completed_at=now + 2,
            ),
        ],
    )
    with client.app.state.rops.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt["id"],
                turn_id="provisional-web-turn",
                role="user",
                kind="message",
                content="The actual browser query",
                metadata_json={"origin": "web", "send_status": "submitted"},
            )
        )
        session.commit()

    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    synced = sync.sync_all()
    assert len(synced) == 1
    assert synced[0].turn.prompt == "The actual browser query"
    assert synced[0].turn.metadata_json["prompt_source"] == "conversation_message_session"

    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        turn.prompt = "# AGENTS.md instructions for /workspace/example"
        session.commit()

    assert sync.backfill_missing_prompts(branch_id=branch["id"]) == 1
    with client.app.state.rops.database.session_factory() as session:
        turn = session.scalar(select(models.CodexTurn))
        assert turn is not None
        assert turn.prompt == "The actual browser query"
        assert turn.metadata_json["prompt_source"] == "conversation_message_session"


def test_storage_failure_does_not_advance_cursor(client, settings, monkeypatch) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="retry-turn", started_at=now + 1),
            _event("user_message", message="Retry safely"),
            _event(
                "task_complete",
                turn_id="retry-turn",
                last_agent_message="Recovered result",
                completed_at=now + 2,
            ),
        ],
    )
    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    original = sync._upsert_turn

    def fail_store(*_args, **_kwargs):
        raise RuntimeError("injected storage failure")

    monkeypatch.setattr(sync, "_upsert_turn", fail_store)
    assert sync.sync_all() == []
    with client.app.state.rops.database.session_factory() as session:
        assert session.scalar(select(models.CodexSyncCursor)) is None
        assert session.scalar(select(models.CodexTurn)) is None

    monkeypatch.setattr(sync, "_upsert_turn", original)
    assert [item.turn.final_answer for item in sync.sync_all()] == ["Recovered result"]


def test_sync_recovers_when_rollout_device_identity_changes(client, settings) -> None:
    """A remounted session filesystem must not strand appended completed turns."""
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="first-turn", started_at=now + 1),
            _event("user_message", message="First request"),
            _event(
                "task_complete",
                turn_id="first-turn",
                last_agent_message="First result",
                completed_at=now + 2,
            ),
        ],
    )
    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    assert [item.turn.final_answer for item in sync.sync_all()] == ["First result"]

    with client.app.state.rops.database.session_factory() as session:
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert cursor is not None
        cursor.st_dev += 1
        session.commit()

    _append_records(
        rollout,
        [
            _event("task_started", turn_id="second-turn", started_at=now + 3),
            _event("user_message", message="Second request"),
            _event(
                "task_complete",
                turn_id="second-turn",
                last_agent_message="Second result",
                completed_at=now + 4,
            ),
        ],
    )
    assert [item.turn.final_answer for item in sync.sync_all()] == ["Second result"]

    with client.app.state.rops.database.session_factory() as session:
        turns = list(
            session.scalars(select(models.CodexTurn).order_by(models.CodexTurn.completed_at))
        )
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert [turn.external_turn_id for turn in turns] == [
            "first-turn",
            "second-turn",
        ]
        assert cursor is not None
        assert cursor.st_dev == rollout.stat().st_dev


def test_sync_reconciles_aborted_and_empty_turns(client, settings) -> None:
    """An interrupted local turn must not permanently lock the Web composer."""
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    thread_id = attempt["codex_thread_id"]
    rollout = _rollout_path(settings.codex_home, thread_id)
    now = int(datetime.now(UTC).timestamp())
    _write_records(
        rollout,
        [
            _session_meta(thread_id, branch["cwd"]),
            _event("task_started", turn_id="empty-turn", started_at=now),
            _event("user_message", message="This turn returned no final message"),
            _event("task_complete", turn_id="empty-turn", completed_at=now + 1),
            _event("task_started", turn_id="aborted-turn", started_at=now + 2),
            _event("user_message", message="This turn was interrupted"),
            _event("turn_aborted", turn_id="aborted-turn", reason="interrupted"),
            _event("task_started", turn_id="finished-turn", started_at=now + 3),
            _event("user_message", message="Keep this result"),
            _event(
                "task_complete",
                turn_id="finished-turn",
                last_agent_message="Done",
                completed_at=now + 4,
            ),
        ],
    )

    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    synced = sync.sync_all()

    assert [item.turn.external_turn_id for item in synced] == ["finished-turn"]
    with client.app.state.rops.database.session_factory() as session:
        cursor = session.scalar(select(models.CodexSyncCursor))
        assert cursor is not None
        assert cursor.pending_json == {}


def test_reconciled_real_thread_retires_placeholder_binding(client, settings) -> None:
    _work, branch = create_work(client)
    attempt = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    placeholder_thread = attempt["codex_thread_id"]
    sync = CodexResultSync(
        client.app.state.rops.database.session_factory, settings.codex_home / "sessions"
    )
    sync.ensure_bindings()
    with client.app.state.rops.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        stored_attempt = session.get(models.SessionAttempt, attempt["id"])
        assert stored_branch and stored_attempt
        stored_branch.codex_thread_id = "real-cli-thread"
        stored_attempt.codex_thread_id = "real-cli-thread"
        session.commit()

    sync.ensure_bindings()

    with client.app.state.rops.database.session_factory() as session:
        bindings = list(
            session.scalars(
                select(models.CodexThreadBinding).where(
                    models.CodexThreadBinding.branch_id == branch["id"]
                )
            )
        )
    status_by_thread = {binding.thread_id: binding.status for binding in bindings}
    assert status_by_thread[placeholder_thread] == "historical"
    assert status_by_thread["real-cli-thread"] == "current"


def _rollout_path(codex_home: Path, thread_id: str) -> Path:
    directory = codex_home / "sessions" / "2026" / "07" / "14"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"rollout-2026-07-14-{thread_id}.jsonl"


def _session_meta(thread_id: str, cwd: str, forked_from_id: str | None = None) -> dict:
    payload = {
        "id": thread_id,
        "session_id": thread_id,
        "cwd": cwd,
        "timestamp": datetime.now(UTC).isoformat(),
        "cli_version": "0.144.3",
    }
    if forked_from_id:
        payload["forked_from_id"] = forked_from_id
    return {"timestamp": datetime.now(UTC).isoformat(), "type": "session_meta", "payload": payload}


def _event(event_type: str, **payload) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": "event_msg",
        "payload": {"type": event_type, **payload},
    }


def _response_user(text: str) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in records))


def _append_records(path: Path, records: list[dict]) -> None:
    with path.open("a") as stream:
        stream.write("".join(json.dumps(item) + "\n" for item in records))
