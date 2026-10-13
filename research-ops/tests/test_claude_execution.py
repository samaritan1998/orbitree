from __future__ import annotations

import asyncio
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from research_ops import cli, models, schemas
from research_ops.api import create_app
from research_ops.codex.claude_execution import ClaudeExecutionPreferences
from sqlalchemy import select
from test_api_workflow import create_work
from test_claude_cli import _fake_claude
from test_claude_handoff import make_adapter


def claude_branch(client):
    work = client.post(
        "/api/work-items",
        json={
            "program_code": "FREE",
            "title": "Claude settings",
            "runtime": "claude",
        },
    )
    assert work.status_code == 200, work.text
    return next(
        branch
        for branch in client.get("/api/programs/FREE/tree").json()["branches"]
        if branch["work_item_id"] == work.json()["id"]
    )


def test_preferences_are_branch_scoped_persisted_and_do_not_change_identity(client):
    first, second = claude_branch(client), claude_branch(client)
    state = client.app.state.rops
    path = f"/api/branches/{first['id']}/claude-execution"
    default = client.get(path).json()
    selected = {"model": "opus", "reasoning_effort": "max"}
    response = client.put(path, json=selected)
    assert response.status_code == 200, response.text
    assert response.json()["preferences"] == selected
    assert client.get(path).json()["effective"]["model"] == "opus"
    assert client.get(f"/api/branches/{second['id']}/claude-execution").json() == default
    assert response.json()["effective"]["label"] == state.settings.claude_account_label
    assert response.json()["effective"]["provider"] == "claude-cli"
    assert state.settings.claude_model == default["effective"]["model"]
    assert state.settings.claude_effort == default["effective"]["reasoning_effort"]
    assert [item["id"] for item in default["models"]] == ["sonnet", "opus", "haiku"]
    assert default["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    with state.database.session_factory() as session:
        assert session.get(models.ExecutionBranch, first["id"]).execution_preferences == selected
        assert not list(session.scalars(select(models.ConversationMessage)))
    assert client.put(path, json={}).json()["effective"] == default["effective"]


@pytest.mark.parametrize(
    "values",
    [
        {"model": ""},
        {"model": "--system-prompt"},
        {"model": "opus\n--other"},
        {"model": "x" * 161},
        {"reasoning_effort": "ultra"},
        {"permission_mode": "allow-everything"},
        {"identity": "api"},
    ],
)
def test_invalid_preferences_cannot_be_saved(client, values):
    branch = claude_branch(client)
    path = f"/api/branches/{branch['id']}/claude-execution"
    before = client.get(path).json()
    assert client.put(path, json=values).status_code == 422
    assert client.get(path).json() == before


def test_claude_endpoint_cannot_switch_a_codex_branch(client):
    _, branch = create_work(client)
    path = f"/api/branches/{branch['id']}/claude-execution"
    assert client.get(path).status_code == 409
    assert client.put(path, json={"model": "opus"}).status_code == 409


async def test_native_flags_are_isolated_and_prompt_stays_verbatim(tmp_path):
    adapter, args_path = make_adapter(tmp_path)
    adapter.settings.claude_permission_mode = "acceptEdits"
    default = (adapter.settings.claude_model, adapter.settings.claude_effort)
    for model, effort, permission in [
        ("opus", "max", "bypassPermissions"), ("sonnet", "low", None)
    ]:
        thread = (await adapter.start_thread(cwd=str(tmp_path), instructions=""))["thread"]["id"]
        prompt = "  Keep this exact query.\n"
        await adapter.send_message(
            thread_id=thread,
            cwd=str(tmp_path),
            content=prompt,
            metadata={
                "research_ops_model": model,
                "research_ops_reasoning_effort": effort,
                **({"research_ops_permission_mode": permission} if permission else {}),
            },
        )
        args = json.loads(args_path.read_text())
        assert args[args.index("--model") + 1] == model
        assert args[args.index("--effort") + 1] == effort
        assert args[args.index("--permission-mode") + 1] == (permission or "acceptEdits")
        assert Path(str(args_path) + ".stdin").read_text() == prompt
        assert "--system-prompt" not in args and "--append-system-prompt" not in args
    assert (adapter.settings.claude_model, adapter.settings.claude_effort) == default
    assert adapter.settings.claude_permission_mode == "acceptEdits"


def test_permission_override_is_branch_scoped_and_survives_model_changes(client):
    first, second = claude_branch(client), claude_branch(client)
    state = client.app.state.rops
    state.settings.claude_permission_mode = "acceptEdits"
    path = f"/api/branches/{first['id']}/claude-execution"
    selected = {"model": "opus", "reasoning_effort": "high", "permission_mode": "bypassPermissions"}
    response = client.put(path, json=selected)
    assert response.status_code == 200, response.text
    assert response.json()["preferences"] == selected
    assert response.json()["effective"]["permission_mode"] == "bypassPermissions"
    assert client.get(f"/api/branches/{second['id']}/claude-execution").json()["effective"][
        "permission_mode"
    ] == "acceptEdits"
    changed = client.put(path, json={"model": "sonnet", "reasoning_effort": "low"}).json()
    assert changed["effective"]["permission_mode"] == "bypassPermissions"
    assert client.get(path).json()["preferences"]["permission_mode"] == "bypassPermissions"
    reset = client.put(path, json={"permission_mode": None}).json()
    assert reset["effective"]["permission_mode"] == "acceptEdits"
    assert "permission_mode" not in reset["preferences"]
    assert state.settings.claude_permission_mode == "acceptEdits"


async def test_accepted_turn_and_local_launch_keep_frozen_settings(client, monkeypatch):
    branch = claude_branch(client)
    state = client.app.state.rops
    service = state.service
    service.update_claude_branch_execution(
        branch["id"], ClaudeExecutionPreferences(
            model="opus", reasoning_effort="max", permission_mode="bypassPermissions"
        )
    )
    attempt, _ = await service._ensure_web_attempt(branch["id"])
    message, prepared = await service._prepare_web_message(
        branch["id"], attempt, schemas.ConversationSend(content="The original query")
    )
    frozen = dict(prepared.message_metadata["execution"])
    service.update_claude_branch_execution(
        branch["id"], ClaudeExecutionPreferences(
            model="sonnet", reasoning_effort="low", permission_mode="acceptEdits"
        )
    )
    send = AsyncMock(return_value={"turn": {"id": "finished"}})
    monkeypatch.setattr(service.claude, "send_message", send)
    await service._dispatch_web_turn(
        branch_id=branch["id"],
        attempt_id=attempt.id,
        thread_id=prepared.thread_id,
        cwd=prepared.cwd,
        work_human_id=prepared.work_human_id,
        brief={},
        content=prepared.content,
        execution=frozen,
    )
    assert send.await_args.kwargs["metadata"]["research_ops_model"] == "opus"
    assert send.await_args.kwargs["metadata"]["research_ops_reasoning_effort"] == "max"
    assert send.await_args.kwargs["metadata"]["research_ops_permission_mode"] == "bypassPermissions"
    assert send.await_args.kwargs["content"] == "The original query"
    assert send.await_args.kwargs.get("additional_context") is None
    with state.database.session_factory() as session:
        assert (
            session.get(models.ConversationMessage, message.id).metadata_json["execution"] == frozen
        )
        assert session.get(models.SessionAttempt, attempt.id).execution_config == frozen
    old_launch = shlex.split(service.codex_terminal_command(branch["id"], execution=frozen))
    next_launch = shlex.split(service.codex_terminal_command(branch["id"]))
    assert old_launch[old_launch.index("--model") + 1] == "opus"
    assert old_launch[old_launch.index("--effort") + 1] == "max"
    assert old_launch[old_launch.index("--permission-mode") + 1] == "bypassPermissions"
    assert next_launch[next_launch.index("--model") + 1] == "sonnet"
    assert next_launch[next_launch.index("--effort") + 1] == "low"
    assert next_launch[next_launch.index("--permission-mode") + 1] == "acceptEdits"


async def test_web_result_and_terminal_attempt_record_claude_execution(
    settings, fake_codex, tmp_path
):
    command, args_path = tmp_path / "fake-claude", tmp_path / "args.json"
    _fake_claude(command, args_path)
    configured = settings.model_copy(
        update={"claude_command": str(command), "claude_turn_timeout_seconds": 10}
    )
    with TestClient(create_app(configured, fake_codex)) as client:
        branch = claude_branch(client)
        state = client.app.state.rops
        selected = {"model": "opus", "reasoning_effort": "xhigh"}
        saved = client.put(f"/api/branches/{branch['id']}/claude-execution", json=selected).json()
        await state.service.send_message(
            branch["id"], schemas.ConversationSend(content="Exact query")
        )
        detail = client.get(f"/api/branches/{branch['id']}").json()
        assert detail["turns"][0]["prompt"] == "Exact query"
        assert detail["turns"][0]["metadata_json"]["execution"] == saved["effective"]
        args = json.loads(args_path.read_text())
        assert args[args.index("--model") + 1] == "opus"
        assert args[args.index("--effort") + 1] == "xhigh"
        attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:999999")
        assert attempt.execution_config == saved["effective"]


def test_scheduled_claude_check_keeps_its_saved_settings(client):
    branch = claude_branch(client)
    path = f"/api/branches/{branch['id']}"
    execution = client.put(
        path + "/claude-execution",
        json={
            "model": "opus",
            "reasoning_effort": "max",
        },
    ).json()["effective"]
    response = client.put(
        path + "/scheduled-check", json={"prompt": "Check job", "interval_minutes": 10}
    )
    assert response.status_code == 200, response.text
    assert response.json()["check"]["execution"] == execution
    client.put(path + "/claude-execution", json={"model": "sonnet", "reasoning_effort": "low"})
    assert client.get(path + "/scheduled-check").json()["check"]["execution"] == execution


async def test_concurrent_branches_dispatch_their_own_models(client, monkeypatch):
    service = client.app.state.rops.service
    branches = [claude_branch(client), claude_branch(client)]
    reached = asyncio.Event()
    seen = []

    async def send(**kwargs):
        seen.append(kwargs["metadata"])
        if len(seen) == 2:
            reached.set()
        await asyncio.wait_for(reached.wait(), 3)
        return {"turn": {"id": kwargs["thread_id"]}}

    monkeypatch.setattr(service.claude, "send_message", send)
    for branch, model, effort in zip(branches, ["opus", "sonnet"], ["max", "low"], strict=True):
        service.update_claude_branch_execution(
            branch["id"], ClaudeExecutionPreferences(model=model, reasoning_effort=effort)
        )
    await asyncio.gather(
        *[
            service.send_message(branch["id"], schemas.ConversationSend(content="Check"))
            for branch in branches
        ]
    )
    assert {
        item["research_ops_branch_id"]: (
            item["research_ops_model"],
            item["research_ops_reasoning_effort"],
        )
        for item in seen
    } == {
        branches[0]["id"]: ("opus", "max"),
        branches[1]["id"]: ("sonnet", "low"),
    }


def test_managed_claude_tui_uses_branch_flags_without_switching_identity(tmp_path, monkeypatch):
    selected = {"model": "opus", "reasoning_effort": "max"}
    branch = SimpleNamespace(cwd=str(tmp_path), execution_preferences={})
    attempt = SimpleNamespace(
        id="attempt",
        lease_id="lease",
        codex_thread_id="native-thread",
        started_at=datetime.now(UTC),
        execution_config=selected,
    )
    service = Mock()
    service.branch_detail.return_value = {"branch": branch, "runtime": "claude"}
    service.claim_terminal_session.return_value = attempt
    command = ["claude-dmit", "--resume", "native-thread", "--model", "opus", "--effort", "max"]
    service.codex_terminal_command.return_value = shlex.join(command)
    state = SimpleNamespace(
        service=service,
        settings=SimpleNamespace(codex_home=tmp_path),
        codex_results=Mock(),
        claude_results=Mock(),
        identities=Mock(),
    )
    monkeypatch.setattr(cli, "runtime", lambda: state)
    monkeypatch.setattr(cli, "run_db", AsyncMock())
    spawn = Mock(return_value=Mock(poll=Mock(return_value=0)))
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    cli.open_branch("branch", model="opus", reasoning_effort="max")
    assert service.update_claude_branch_execution.call_args.args == (
        "branch",
        ClaudeExecutionPreferences(**selected),
    )
    service.codex_terminal_command.assert_called_once_with("branch", execution=selected)
    assert spawn.call_args.args[0] == command
    service.update_branch_execution.assert_not_called()
    state.identities.environment.assert_not_called()
