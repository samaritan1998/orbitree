import json
from datetime import UTC, datetime
from pathlib import Path

from research_ops import models
from research_ops.codex.rollout import read_live_rollout
from sqlalchemy import func, select
from test_api_workflow import create_work


def test_live_rollout_whitelists_visible_events_and_updates_tools(tmp_path: Path) -> None:
    path = tmp_path / "rollout-live.jsonl"
    records = [
        _record("session_meta", {"id": "thread-live", "cwd": str(tmp_path)}),
        _event("task_started", turn_id="turn-live"),
        _record("response_item", {"type": "reasoning", "summary": "hidden reasoning"}),
        _event("agent_message", message="Visible progress", phase="commentary"),
        _record(
            "response_item",
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "uv run pytest -q"}),
            },
        ),
        _record(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": {"exit_code": 0},
            },
        ),
        _event("task_complete", turn_id="turn-live", last_agent_message="Done"),
    ]
    _write_records(path, records)

    batch = read_live_rollout(path, offset=0, current_turn_id=None)

    assert [event.event_type for event in batch.events] == [
        "turn.started",
        "activity",
        "activity",
        "activity",
        "turn.completed",
    ]
    assert all("hidden reasoning" not in event.content for event in batch.events)
    assistant = next(event for event in batch.events if event.role == "assistant")
    assert assistant.content == "Visible progress"
    tools = [event for event in batch.events if event.role == "tool"]
    assert [event.event_id for event in tools] == ["tool:call-1", "tool:call-1"]
    assert tools[0].content == "$ uv run pytest -q"
    assert tools[0].status == "running"
    assert tools[1].status == "completed"
    assert batch.current_turn_id is None
    assert batch.next_offset == path.stat().st_size


def test_live_rollout_skips_oversized_record_and_reaches_completion(tmp_path: Path) -> None:
    path = tmp_path / "rollout-live-oversized.jsonl"
    _write_records(
        path,
        [
            _record("session_meta", {"id": "thread-live", "cwd": str(tmp_path)}),
            _event("task_started", turn_id="turn-live"),
            _event("agent_message", message="Before the large output", phase="commentary"),
            _record(
                "response_item",
                {"type": "function_call_output", "output": "x" * 4096},
            ),
            _event("task_complete", turn_id="turn-live", last_agent_message="Done"),
        ],
    )

    batch = read_live_rollout(
        path, offset=0, current_turn_id=None, max_line_bytes=512
    )

    assert [event.event_type for event in batch.events] == [
        "turn.started",
        "activity",
        "turn.completed",
    ]
    assert batch.skipped_oversized_lines == 1
    assert batch.next_offset == path.stat().st_size
    assert batch.current_turn_id is None


def test_local_live_sync_replays_current_turn_without_persisting_activity(
    client, settings
) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:live")
    thread_id = "019f-local-live-thread"
    state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt.id,
        thread_id=thread_id,
        bound_at=attempt.started_at or datetime.now(UTC),
    )
    path = _rollout_path(settings.codex_home, thread_id)
    _write_records(
        path,
        [
            _record("session_meta", {"id": thread_id, "cwd": branch["cwd"]}),
            _event("task_started", turn_id="turn-local-live"),
            _event("user_message", message="Watch this locally"),
            _record("response_item", {"type": "reasoning", "content": "never publish"}),
            _event("agent_message", message="Local checkpoint reached", phase="commentary"),
        ],
    )
    assert state.codex_results.sync_all() == []

    first = state.codex_live.poll()
    assert [item.event["type"] for item in first] == [
        "local.turn.started",
        "local.live",
    ]
    snapshot = state.codex_live.snapshot(branch["id"])
    assert [event["content"] for event in snapshot] == ["Local checkpoint reached"]
    assert state.codex_live.poll() == []
    with state.database.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(models.ConversationMessage))
        assert count == 0

    _append_records(
        path,
        [
            _record(
                "response_item",
                {
                    "type": "custom_tool_call",
                    "call_id": "call-local",
                    "name": "exec",
                    "input": "const result = await tools.exec_command({cmd: 'date'});",
                },
            ),
            _record(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-local",
                    "output": [{"type": "input_text", "text": '{"exit_code":0}'}],
                },
            ),
        ],
    )
    tool_updates = state.codex_live.poll()
    assert len(tool_updates) == 2
    snapshot = state.codex_live.snapshot(branch["id"])
    tool = next(event for event in snapshot if event["role"] == "tool")
    assert tool["status"] == "completed"
    assert tool["content"].endswith("status=completed")

    _append_records(
        path,
        [
            _record("response_item", {"type": "reasoning", "content": "still hidden"}),
            _event(
                "task_complete",
                turn_id="turn-local-live",
                last_agent_message="Finished",
            ),
        ],
    )
    completed = state.codex_live.poll()
    assert [item.event["type"] for item in completed] == ["local.turn.completed"]
    assert state.codex_live.snapshot(branch["id"]) == []


def _rollout_path(codex_home: Path, thread_id: str) -> Path:
    directory = codex_home / "sessions" / "2026" / "07" / "15"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"rollout-2026-07-15-{thread_id}.jsonl"


def _record(record_type: str, payload: dict) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": record_type,
        "payload": payload,
    }


def _event(event_type: str, **payload) -> dict:
    return _record("event_msg", {"type": event_type, **payload})


def _write_records(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in records))


def _append_records(path: Path, records: list[dict]) -> None:
    with path.open("a") as stream:
        stream.write("".join(json.dumps(item) + "\n" for item in records))
