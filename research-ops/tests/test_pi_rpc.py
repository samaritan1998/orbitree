from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from research_ops.codex.pi import PiRpcAdapter
from research_ops.settings import Settings


async def _noop_request(
    _request_id: str, _method: str, _params: dict[str, Any]
) -> None:
    return None


def _fake_pi(path: Path, env_path: Path) -> None:
    script = """#!/usr/bin/env python3
import json
import os
import sys

if '--version' in sys.argv:
    print('pi-test 0.1.0')
    raise SystemExit(0)

with open({str(env_path)!r}, 'w', encoding='utf-8') as handle:
    keys = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')
    json.dump({key: os.environ.get(key) for key in keys}, handle)
with open({str(env_path)!r} + '.args.json', 'w') as handle:
    json.dump(sys.argv, handle)

def argument(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default

session_id = argument('--session-id') or argument('--session') or 'fake-session'

def emit(value):
    print(json.dumps(value), flush=True)

emit({'type': 'session', 'id': session_id, 'cwd': os.getcwd()})

for line in sys.stdin:
    command = json.loads(line)
    with open({str(env_path)!r} + '.commands.jsonl', 'a') as handle:
        handle.write(json.dumps(command) + '\\n')
    kind = command.get('type')
    request_id = command.get('id')
    if kind == 'get_state':
        emit({
            'id': request_id,
            'type': 'response',
            'command': kind,
            'success': True,
            'data': {'sessionId': session_id, 'isStreaming': False},
        })
    elif kind == 'prompt':
        emit({'id': request_id, 'type': 'response', 'command': kind, 'success': True})
        emit({'type': 'turn_start'})
        emit({
            'type': 'message_update',
            'assistantMessageEvent': {'type': 'text_delta', 'delta': 'Pi OK'},
        })
        emit({
            'type': 'tool_execution_start',
            'toolCallId': 'tool-1',
            'toolName': 'read',
            'args': {'path': 'README.md'},
        })
        emit({
            'type': 'tool_execution_end',
            'toolCallId': 'tool-1',
            'toolName': 'read',
            'isError': False,
            'result': {'content': [{'type': 'text', 'text': 'read'}]},
        })
        emit({
            'type': 'message_end',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': 'Pi OK'}],
            },
        })
        emit({
            'type': 'turn_end',
            'message': {'stopReason': 'stop', 'usage': {'totalTokens': 3}},
        })
        emit({'type': 'agent_settled'})
    elif kind in ('steer', 'follow_up', 'abort'):
        emit({'id': request_id, 'type': 'response', 'command': kind, 'success': True})
""".replace("{str(env_path)!r}", repr(str(env_path)))
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.asyncio
async def test_pi_rpc_turn_forwards_safe_events_and_scopes_proxy(tmp_path: Path) -> None:
    command = tmp_path / "fake-pi"
    env_path = tmp_path / "pi-env.json"
    _fake_pi(command, env_path)
    events: list[tuple[str, dict[str, Any]]] = []

    async def event_handler(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    adapter = PiRpcAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_backend="pi",
            codex_home=tmp_path / "codex-home",
            pi_command=str(command),
            pi_session_dir=tmp_path / "pi-sessions",
            pi_http_proxy="http://proxy.test:8891",
            pi_https_proxy="http://proxy.test:8891",
            pi_turn_timeout_seconds=10,
        ),
        event_handler=event_handler,
        request_handler=_noop_request,
    )

    thread = await adapter.start_thread(cwd=str(tmp_path), instructions="system brief")
    query = "  hello\n\nKeep my formatting.  "
    result = await adapter.send_message(
        thread_id=thread["thread"]["id"],
        content=query,
        cwd=str(tmp_path),
        metadata={"research_ops_branch_id": "branch-1"},
        additional_context={"brief": {"objective": "unrelated platform context"}},
    )

    assert result["thread"]["id"] == thread["thread"]["id"]
    args = json.loads(Path(str(env_path) + ".args.json").read_text())
    assert "--append-system-prompt" not in args
    commands = [
        json.loads(line)
        for line in Path(str(env_path) + ".commands.jsonl").read_text().splitlines()
    ]
    assert [cmd["message"] for cmd in commands if cmd["type"] == "prompt"] == [query]
    assert result["turn"]["id"]
    methods = [method for method, _params in events]
    assert "turn/started" in methods
    assert "item/agentMessage/delta" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods
    assert "pi/settled" in methods
    assert all("thinking" not in params for _method, params in events)
    assert any(
        params.get("item", {}).get("text") == "Pi OK"
        for method, params in events
        if method == "item/completed"
    )
    assert json.loads(env_path.read_text(encoding="utf-8")) == {
        "http_proxy": "http://proxy.test:8891",
        "https_proxy": "http://proxy.test:8891",
        "HTTP_PROXY": "http://proxy.test:8891",
        "HTTPS_PROXY": "http://proxy.test:8891",
    }

    await adapter.stop()


@pytest.mark.asyncio
async def test_pi_rpc_exposes_steer_follow_up_abort_and_health(tmp_path: Path) -> None:
    command = tmp_path / "fake-pi"
    _fake_pi(command, tmp_path / "pi-env.json")
    adapter = PiRpcAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_backend="pi",
            pi_command=str(command),
            pi_session_dir=tmp_path / "pi-sessions",
        ),
        event_handler=lambda _method, _params: _noop_event(),
        request_handler=_noop_request,
    )

    health = await adapter.health()
    assert health["available"] is True
    assert health["protocol"] == "pi-rpc-jsonl"

    thread = await adapter.start_thread(cwd=str(tmp_path), instructions="brief")
    thread_id = thread["thread"]["id"]
    assert (await adapter.steer_message(thread_id=thread_id, content="steer", cwd=str(tmp_path)))[
        "success"
    ]
    assert (await adapter.follow_up(thread_id=thread_id, content="later", cwd=str(tmp_path)))[
        "success"
    ]
    assert (await adapter.abort(thread_id=thread_id, cwd=str(tmp_path)))["success"]
    state = await adapter.get_state(thread_id=thread_id, cwd=str(tmp_path))
    assert state["success"] is True
    await adapter.stop()


async def _noop_event() -> None:
    return None
