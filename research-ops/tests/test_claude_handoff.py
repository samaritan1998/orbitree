from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from research_ops import models
from research_ops.codex.claude import ClaudeCliAdapter
from research_ops.codex.claude_handoff import (
    bound_claude_handoff,
    handoff_path,
    render_claude_handoff,
    stage_claude_handoff,
)
from research_ops.settings import Settings
from test_api_workflow import create_work
from test_claude_cli import _fake_claude

HANDOFF = {
    "source": {"thread_id": "source-thread", "runtime": "codex"},
    "completed_turns": [{"prompt": "Keep the canary result", "final_answer": "canary=42"}],
    "handoff_hash": "frozen-hash",
}


def make_adapter(tmp_path: Path) -> tuple[ClaudeCliAdapter, Path]:
    command, args = tmp_path / "claude", tmp_path / "args.json"
    _fake_claude(command, args)
    adapter = ClaudeCliAdapter(
        Settings(
            data_dir=tmp_path / "data",
            runtime_dir=tmp_path / "runtime",
            claude_home=tmp_path / "claude-home",
            claude_command=str(command),
        ),
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
    )
    return adapter, args


@pytest.mark.asyncio
async def test_approved_handoff_is_delivered_once_across_adapter_restarts(tmp_path):
    adapter, args = make_adapter(tmp_path)
    thread_id = (await adapter.start_thread(cwd=str(tmp_path), instructions=""))["thread"]["id"]
    stage_claude_handoff(adapter._state_dir, thread_id, HANDOFF)
    path = handoff_path(adapter._state_dir, thread_id)
    assert path.stat().st_mode & 0o777 == 0o600
    query = "  Check the result.\nKeep this query intact.  "
    await adapter.send_message(thread_id=thread_id, content=query, cwd=str(tmp_path), metadata={})
    sent = json.loads(args.read_text())
    assert (
        Path(str(args) + ".stdin").read_text()
        == render_claude_handoff(HANDOFF) + "\n\nCurrent user message:\n" + query
    )
    assert "--append-system-prompt" not in sent
    assert "--system-prompt" not in sent
    assert "--session-id" in sent
    await adapter.stop()
    restarted, _ = make_adapter(tmp_path)
    await restarted.send_message(thread_id=thread_id, content=query, cwd=str(tmp_path), metadata={})
    assert Path(str(args) + ".stdin").read_text() == query
    assert "--resume" in json.loads(args.read_text())


@pytest.mark.asyncio
async def test_local_first_launch_gets_handoff_but_resumed_tui_and_web_do_not(tmp_path):
    adapter, args = make_adapter(tmp_path)
    thread_id = (await adapter.start_thread(cwd=str(tmp_path), instructions=""))["thread"]["id"]
    stage_claude_handoff(adapter._state_dir, thread_id, HANDOFF)
    command = shlex.split(adapter.terminal_command(thread_id=thread_id, cwd=str(tmp_path)))
    assert command[-1] == render_claude_handoff(HANDOFF)
    index = command.index("--add-dir")
    assert command[index + 1] == str(tmp_path.resolve())
    assert command[index + 2].startswith("--")
    assert "--session-id" in command
    session_path = adapter.settings.claude_home / "projects" / "test" / f"{thread_id}.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({"type": "user", "message": {"content": command[-1]}}))
    command = shlex.split(adapter.terminal_command(thread_id=thread_id, cwd=str(tmp_path)))
    assert "--resume" in command
    assert render_claude_handoff(HANDOFF) not in command
    await adapter.send_message(thread_id=thread_id, content="Next", cwd=str(tmp_path), metadata={})
    assert Path(str(args) + ".stdin").read_text() == "Next"


@pytest.mark.asyncio
async def test_large_first_query_uses_stdin_without_losing_context(tmp_path):
    adapter, args = make_adapter(tmp_path)
    thread_id = (await adapter.start_thread(cwd=str(tmp_path), instructions=""))["thread"]["id"]
    stage_claude_handoff(adapter._state_dir, thread_id, HANDOFF)
    query = "\u4e2d" * 50_000
    await adapter.send_message(thread_id=thread_id, content=query, cwd=str(tmp_path), metadata={})
    sent = json.loads(args.read_text())
    assert "--input-format" in sent
    assert query not in sent
    assert Path(str(args) + ".stdin").read_text() == (
        render_claude_handoff(HANDOFF) + "\n\nCurrent user message:\n" + query
    )


@pytest.mark.asyncio
async def test_startup_failure_keeps_handoff_for_retry(tmp_path, monkeypatch):
    adapter, _ = make_adapter(tmp_path)
    thread_id = (await adapter.start_thread(cwd=str(tmp_path), instructions=""))["thread"]["id"]
    stage_claude_handoff(adapter._state_dir, thread_id, HANDOFF)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    with pytest.raises(RuntimeError):
        await adapter.send_message(
            thread_id=thread_id, content="Next", cwd=str(tmp_path), metadata={}
        )
    assert adapter._initial_handoff(thread_id) == render_claude_handoff(HANDOFF)


def test_handoff_is_immutable_and_rejects_path_traversal(tmp_path):
    stage_claude_handoff(tmp_path, "thread", HANDOFF)
    stage_claude_handoff(tmp_path, "thread", HANDOFF)
    with pytest.raises(ValueError, match="frozen"):
        stage_claude_handoff(tmp_path, "thread", {**HANDOFF, "source": {}})
    with pytest.raises(ValueError, match="Invalid"):
        stage_claude_handoff(tmp_path, "../thread", HANDOFF)


def test_multibyte_handoff_is_bounded_before_tui_launch():
    handoff = {
        "completed_turns": [
            {"prompt": "\u4e2d" * 4000, "final_answer": "\u6587" * 10000, "turn_id": str(i)}
            for i in range(6)
        ],
        "latest_handoff": {"conclusion": "\u4e2d" * 4000},
        "upstream_handoff": {"transcript": "\u4e2d" * 12000},
    }
    bound_claude_handoff(handoff)
    assert len(render_claude_handoff(handoff).encode()) < 81_000
    assert handoff["completed_turns"][-1]["turn_id"] == "5"
    assert handoff["truncated"] is True


def seed_source(client):
    _, parent = create_work(client)
    now = datetime.now(UTC)
    with client.app.state.rops.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, parent["id"])
        stored.codex_thread_id = "parent-thread"
        binding = models.CodexThreadBinding(
            branch_id=parent["id"],
            thread_id="parent-thread",
            bound_at=now,
            source="terminal",
        )
        session.add(binding)
        session.flush()
        parent["binding_id"] = binding.id
        session.add(
            models.CodexTurn(
                branch_id=parent["id"],
                thread_binding_id=binding.id,
                thread_id="parent-thread",
                external_turn_id="source-turn",
                status="completed",
                prompt="Keep this original query",
                final_answer="The result is 42",
                completed_at=now,
            )
        )
        session.commit()
    return parent


@pytest.mark.parametrize("include_context", [True, False])
def test_preview_confirm_and_opt_out_leave_source_unchanged(client, fake_codex, include_context):
    parent = seed_source(client)
    Path(parent["cwd"], "README.md").write_text("Parent source snapshot")
    state = client.app.state.rops
    state.service.claude = fake_codex
    preview = client.get(f"/api/branches/{parent['id']}/claude-handoff")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["turn_count"] == 1
    assert "Keep this original query" in body["prompt"]
    assert (
        client.get(f"/api/branches/{parent['id']}/claude-handoff").json()["handoff_hash"]
        == body["handoff_hash"]
    )
    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-claude",
        json={
            "manual_approval": True,
            "include_context": include_context,
            "expected_handoff_hash": body["handoff_hash"],
        },
    )
    assert response.status_code == 200, response.text
    child = response.json()
    staged = handoff_path(
        state.settings.runtime_data_dir / "claude-runtime", child["codex_thread_id"]
    )
    assert staged.exists() == include_context
    assert child["cwd"] != parent["cwd"]
    assert Path(child["cwd"], "README.md").read_text() == "Parent source snapshot"
    assert child["context_snapshot"]["workspace_snapshot"]["state"] == "ready"
    assert child["context_snapshot"]["claude_handoff_delivery"]["approved"] == include_context
    with state.database.session_factory() as session:
        source = session.get(models.ExecutionBranch, parent["id"])
        assert source.status == "active"
        assert source.runtime == "codex"
        assert source.codex_thread_id == "parent-thread"
        assert source.continued_by_branch_id is None


def test_new_source_result_requires_another_preview(client, fake_codex):
    parent = seed_source(client)
    client.app.state.rops.service.claude = fake_codex
    preview = client.get(f"/api/branches/{parent['id']}/claude-handoff").json()
    with client.app.state.rops.database.session_factory() as session:
        session.add(
            models.CodexTurn(
                branch_id=parent["id"],
                thread_binding_id=parent["binding_id"],
                thread_id="parent-thread",
                external_turn_id="later",
                status="completed",
                prompt="New question",
                final_answer="New answer",
                completed_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        session.commit()
    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-claude",
        json={
            "manual_approval": True,
            "expected_handoff_hash": preview["handoff_hash"],
        },
    )
    assert response.status_code == 409, response.text
    assert "Refresh the handoff preview" in response.json()["detail"]


def test_bounded_history_keeps_latest_turn_when_older_answers_are_long(client):
    parent = seed_source(client)
    with client.app.state.rops.database.session_factory() as session:
        for index in range(7):
            session.add(
                models.CodexTurn(
                    branch_id=parent["id"],
                    thread_binding_id=parent["binding_id"],
                    thread_id="parent-thread",
                    external_turn_id=f"long-{index}",
                    status="completed",
                    prompt="x" * 4000,
                    final_answer="y" * 10000,
                    completed_at=datetime.now(UTC) + timedelta(seconds=index + 1),
                )
            )
        session.commit()
        source = session.get(models.ExecutionBranch, parent["id"])
        handoff = client.app.state.rops.service._runtime_handoff(
            session, source, target_runtime="claude"
        )
    assert handoff["completed_turns"][-1]["turn_id"] == "long-6"
    assert (
        sum(len(turn["prompt"]) + len(turn["final_answer"]) for turn in handoff["completed_turns"])
        <= 36_000
    )
