from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from research_ops.api import create_app
from research_ops.codex.claude import ClaudeCliAdapter
from research_ops.settings import Settings


async def _noop_request(
    _request_id: str, _method: str, _params: dict[str, Any]
) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_schedule_wakeup_is_forwarded_only_after_successful_tool_result(settings, failed):
    events = []

    async def record(method, params):
        events.append((method, params))

    adapter = ClaudeCliAdapter(settings, event_handler=record, request_handler=_noop_request)
    kwargs = dict(branch_id="branch", thread_id="thread", turn_id="turn", assistant_texts=[],
                  tool_names={}, wakeup_requests={})
    payload = {"delaySeconds": 1000, "prompt": "  Check once.\n", "reason": "Wait for data"}
    await adapter._translate_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool", "name": "ScheduleWakeup", "input": payload},
    ]}}, **kwargs)
    assert not any(method == "claude/wakeup/scheduled" for method, _ in events)
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool", "content": "Scheduled", "is_error": failed},
    ]}}
    await adapter._translate_event(result, **kwargs)
    await adapter._translate_event(result, **kwargs)
    wakeups = [params for method, params in events if method == "claude/wakeup/scheduled"]
    assert len(wakeups) == (0 if failed else 1)
    if wakeups:
        assert wakeups[0]["input"] == payload
        assert wakeups[0]["threadId"] == "thread"


def _fake_claude(path: Path, args_path: Path, *, wakeup: bool = False) -> None:
    script = f"""#!/usr/bin/env python3
import json
import sys

if '--version' in sys.argv:
    print('claude-test 1.0.0')
    raise SystemExit(0)

if '--permission-prompt-tool' in sys.argv:
    initialize = json.loads(sys.stdin.readline())
    print(json.dumps({{'type': 'control_response', 'response': {{
        'subtype': 'success', 'request_id': initialize['request_id'], 'response': {{}}
    }}}}), flush=True)
    user = json.loads(sys.stdin.readline())
    with open({str(args_path) + '.stdin'!r}, 'w', encoding='utf-8') as handle:
        handle.write(user['message']['content'])
elif '--input-format' in sys.argv:
    with open({str(args_path) + '.stdin'!r}, 'w', encoding='utf-8') as handle:
        handle.write(sys.stdin.read())

with open({str(args_path)!r}, 'w', encoding='utf-8') as handle:
    json.dump(sys.argv, handle)

def argument(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default

session_id = argument('--session-id') or argument('--resume') or 'missing-session'

def emit(value):
    print(json.dumps(value), flush=True)

emit({{'type': 'system', 'subtype': 'init', 'session_id': session_id}})
if {wakeup!r}:
    emit({{'type': 'assistant', 'message': {{'content': [{{
        'type': 'tool_use', 'id': 'schedule-tool', 'name': 'ScheduleWakeup',
        'input': {{'delaySeconds': 1000, 'prompt': '  Check once.\\n'}},
    }}]}}}})
    emit({{'type': 'user', 'message': {{'content': [{{
        'type': 'tool_result', 'tool_use_id': 'schedule-tool', 'content': 'Scheduled',
    }}]}}}})
emit({{
    'type': 'stream_event',
    'event': {{
        'type': 'content_block_delta',
        'delta': {{'type': 'text_delta', 'text': 'Claude OK'}},
    }},
    'session_id': session_id,
}})
emit({{
    'type': 'assistant',
    'session_id': session_id,
    'message': {{
        'id': 'message-1',
        'content': [{{'type': 'text', 'text': 'Claude OK'}}],
    }},
}})
emit({{
    'type': 'result',
    'subtype': 'success',
    'is_error': False,
    'session_id': session_id,
    'result': 'Claude OK',
    'usage': {{'input_tokens': 2, 'output_tokens': 2}},
}})
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_real_adapter_exit_leaves_a_durable_wakeup(settings, fake_codex, tmp_path):
    command = tmp_path / "fake-claude-wakeup"
    _fake_claude(command, tmp_path / "args.json", wakeup=True)
    settings.claude_command = str(command)
    with TestClient(create_app(settings, fake_codex)) as client:
        state = client.app.state.rops
        state.settings.scheduled_checks_enabled = True
        work = client.post("/api/work-items", json={
            "program_code": "FREE", "title": "Wakeup integration", "branch_mode": "write",
            "runtime": "claude",
        }).json()
        branch = next(row for row in client.get("/api/programs/FREE/tree").json()["branches"]
                      if row["work_item_id"] == work["id"])
        response = client.post(
            f"/api/branches/{branch['id']}/messages", json={"content": "Check later"}
        )
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            wakeups = client.get(f"/api/branches/{branch['id']}/scheduled-check").json()["wakeups"]
            if wakeups and wakeups[0]["status"] == "scheduled":
                break
            time.sleep(0.05)
        assert len(wakeups) == 1
        assert wakeups[0]["status"] == "scheduled"
        assert wakeups[0]["prompt"] == "  Check once.\n"


@pytest.mark.asyncio
async def test_claude_cli_streams_events_and_preserves_verbatim_prompt(tmp_path: Path) -> None:
    command = tmp_path / "fake-claude"
    args_path = tmp_path / "claude-args.json"
    _fake_claude(command, args_path)
    events: list[tuple[str, dict[str, Any]]] = []

    async def event_handler(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    settings = Settings(
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        workspace_root=tmp_path / "workspaces",
        claude_home=tmp_path / "claude-home",
        claude_command=str(command),
        claude_model="auto",
        claude_effort="high",
        claude_turn_timeout_seconds=10,
    )
    adapter = ClaudeCliAdapter(
        settings,
        event_handler=event_handler,
        request_handler=_noop_request,
    )

    health = await adapter.health()
    assert health["available"] is True
    assert health["protocol"] == "claude-stream-json"

    thread = await adapter.start_thread(cwd=str(tmp_path), instructions="hidden brief")
    thread_id = thread["thread"]["id"]
    assert "--session-id" in adapter.terminal_command(thread_id=thread_id, cwd=str(tmp_path))
    query = "  hello\n\nKeep my formatting.  "
    result = await adapter.send_message(
        thread_id=thread_id,
        content=query,
        cwd=str(tmp_path),
        metadata={"research_ops_branch_id": "branch-1"},
    )

    assert result["thread"]["id"] == thread_id
    args = json.loads(args_path.read_text(encoding="utf-8"))
    assert Path(str(args_path) + ".stdin").read_text() == query
    assert "--append-system-prompt" not in args
    assert [method for method, _params in events] == [
        "turn/started",
        "item/agentMessage/delta",
        "item/completed",
        "turn/completed",
        "claude/approvals/expired",
    ]
    assert events[-1][1]["runtime"] == "claude"
    assert events[-2][1]["turn"]["status"] == "completed"
    assert adapter.snapshot(thread_id) == {
        "state": "idle",
        "queue": {"steering": [], "follow_up": []},
    }
    assert not (settings.runtime_data_dir / "claude-runtime" / f"{thread_id}.json").exists()
    session_dir = settings.claude_home / "projects" / "project"
    session_dir.mkdir(parents=True)
    (session_dir / f"{thread_id}.jsonl").write_text("", encoding="utf-8")
    assert "--resume" in adapter.terminal_command(thread_id=thread_id, cwd=str(tmp_path))
    await adapter.stop()


def test_claude_web_turn_is_persisted_as_a_completed_result(
    settings: Settings, fake_codex, tmp_path: Path
) -> None:
    command = tmp_path / "fake-claude-web"
    _fake_claude(command, tmp_path / "claude-web-args.json")
    configured = settings.model_copy(
        update={
            "claude_command": str(command),
            "claude_turn_timeout_seconds": 10,
        }
    )
    with TestClient(create_app(configured, fake_codex)) as client:
        created = client.post(
            "/api/work-items",
            json={
                "program_code": "FREE",
                "title": "Claude Web turn",
                "objective": "Verify Claude result persistence",
                "branch_mode": "write",
                "runtime": "claude",
                "priority": 50,
            },
        )
        assert created.status_code == 200, created.text
        tree = client.get("/api/programs/FREE/tree").json()
        branch = next(
            item
            for item in tree["branches"]
            if item["work_item_id"] == created.json()["id"]
        )
        sent = client.post(
            f"/api/branches/{branch['id']}/messages",
            json={"content": "Reply from Claude."},
        )
        assert sent.status_code == 200, sent.text

        deadline = time.monotonic() + 5
        detail: dict[str, Any] = {}
        while time.monotonic() < deadline:
            detail = client.get(f"/api/branches/{branch['id']}").json()
            if detail["turns"]:
                break
            time.sleep(0.05)

        assert detail["runtime"] == "claude"
        assert detail["turns"][0]["final_answer"] == "Claude OK"
        assert detail["turns"][0]["metadata_json"]["runtime"] == "claude"
        assert detail["messages"][0]["content"] == "Reply from Claude."
        assert detail["conversation_state"]["web_turn_status"] == "completed"
        terminal = client.get(f"/api/branches/{branch['id']}/terminal-command").json()
        assert "rops' open" in terminal["command"]
        assert " && " in terminal["codex_command"]
        assert "--resume" in terminal["codex_command"]


def test_completed_local_claude_tui_turn_is_imported(
    settings: Settings, fake_codex, tmp_path: Path
) -> None:
    claude_home = tmp_path / "claude-home"
    configured = settings.model_copy(update={"claude_home": claude_home})
    with TestClient(create_app(configured, fake_codex, claude=fake_codex)) as client:
        created = client.post(
            "/api/work-items",
            json={
                "program_code": "FREE",
                "title": "Claude local turn",
                "objective": "Import the native TUI result",
                "branch_mode": "write",
                "runtime": "claude",
                "priority": 50,
            },
        ).json()
        tree = client.get("/api/programs/FREE/tree").json()
        branch = next(
            item for item in tree["branches"] if item["work_item_id"] == created["id"]
        )
        started = client.post(
            f"/api/branches/{branch['id']}/sessions", json={"owner": "terminal:test"}
        ).json()
        thread_id = started["codex_thread_id"]
        path = claude_home / "projects" / "project" / f"{thread_id}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "\n".join(
                json.dumps(event)
                for event in (
                    {
                        "type": "user",
                        "isSidechain": False,
                        "timestamp": "2026-09-24T09:00:00Z",
                        "message": {"role": "user", "content": "Inspect this Branch."},
                    },
                    {
                        "type": "assistant",
                        "isSidechain": False,
                        "timestamp": "2026-09-24T09:00:02Z",
                        "message": {
                            "id": "claude-local-turn-1",
                            "model": "claude-opus-4-8",
                            "content": [{"type": "text", "text": "Inspection complete."}],
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        imported = client.app.state.rops.claude_results.sync_session(
            branch_id=branch["id"],
            session_id=started["id"],
            thread_id=thread_id,
            started_at=datetime.fromisoformat("2026-09-24T08:59:00+00:00"),
        )

        assert len(imported) == 1
        assert imported[0].prompt == "Inspect this Branch."
        assert imported[0].final_answer == "Inspection complete."
        assert imported[0].metadata_json["runtime"] == "claude"
