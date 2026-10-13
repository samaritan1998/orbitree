import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from research_ops import models
from research_ops.api import create_app
from research_ops.codex.base import CodexUnavailable
from test_claude_execution import claude_branch


def permission_cli(tmp_path: Path) -> tuple[Path, Path]:
    command, response = tmp_path / "claude-permission", tmp_path / "response.json"
    command.write_text(
        """#!/usr/bin/env python3
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
if '--version' in sys.argv:
    print('test-claude')
    raise SystemExit(0)
init = json.loads(sys.stdin.readline())
emit({'type': 'control_response', 'response': {
    'subtype': 'success', 'request_id': init['request_id'], 'response': {}}})
user = json.loads(sys.stdin.readline())
emit({'type': 'control_request', 'request_id': 'permission-native', 'request': {
    'subtype': 'can_use_tool', 'tool_name': 'Bash', 'tool_use_id': 'tool-1',
    'input': {'command': 'python inspect.py'}, 'blocked_path': '/source/reference'}})
reply = json.loads(sys.stdin.readline())
with open(RESPONSE_PATH, 'w') as handle:
    json.dump(reply, handle)
emit({'type': 'assistant', 'message': {'id': 'answer', 'content': [
    {'type': 'text', 'text': 'Permission decision received'}]}})
emit({'type': 'result', 'result': 'Permission decision received', 'is_error': False})
""".replace("RESPONSE_PATH", repr(str(response)))
    )
    command.chmod(0o700)
    return command, response


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_web_approval_round_trip_uses_claude_only(settings, fake_codex, tmp_path, decision):
    command, response_path = permission_cli(tmp_path)
    configured = settings.model_copy(
        update={
            "claude_command": str(command),
            "claude_turn_timeout_seconds": 10,
        }
    )
    with TestClient(create_app(configured, fake_codex)) as client:
        branch = claude_branch(client)
        path = f"/api/branches/{branch['id']}"
        sent = client.post(path + "/messages", json={"content": "Inspect the code"})
        assert sent.status_code == 200, sent.text
        deadline = time.monotonic() + 5
        approvals = []
        while time.monotonic() < deadline:
            approvals = client.get(path).json()["approvals"]
            if approvals:
                break
            time.sleep(0.02)
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval["params"]["input"] == {"command": "python inspect.py"}
        assert approval["params"]["blocked_path"] == "/source/reference"
        assert not response_path.exists()
        resolved = client.post(
            f"/api/approvals/{approval['id']}/resolve", json={"decision": decision}
        )
        assert resolved.status_code == 200, resolved.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not client.get(path).json()["turns"]:
            time.sleep(0.02)
        reply = json.loads(response_path.read_text())
        assert reply["response"]["request_id"] == "permission-native"
        actual = reply["response"]["response"]
        assert actual["behavior"] == ("allow" if decision == "accept" else "deny")
        if decision == "accept":
            assert actual["updatedInput"] == {"command": "python inspect.py"}
        else:
            assert "updatedInput" not in actual
        assert client.get(path).json()["approvals"] == []
        assert (
            client.post(
                f"/api/approvals/{approval['id']}/resolve", json={"decision": decision}
            ).json()["status"]
            == "resolved"
        )


async def test_stale_approval_is_never_sent_to_a_new_process(client):
    with pytest.raises(CodexUnavailable, match="expired"):
        await client.app.state.rops.claude.respond("claude:expired", {"decision": "accept"})


def test_restart_expires_only_claude_approvals(client):
    state = client.app.state.rops
    with state.database.session_factory() as session:
        a = models.PendingApproval(
            rpc_request_id="claude:old", method="claude/tool/requestApproval"
        )
        b = models.PendingApproval(rpc_request_id="codex:old", method="execCommandApproval")
        session.add_all([a, b])
        session.commit()
        ids = a.id, b.id
    state.codex_events.expire_detached_claude_approvals()
    with state.database.session_factory() as session:
        assert session.get(models.PendingApproval, ids[0]).status == "expired"
        assert session.get(models.PendingApproval, ids[1]).status == "pending"
