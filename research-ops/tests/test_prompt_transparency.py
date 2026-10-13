from __future__ import annotations

import shlex
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from research_ops import cli, models, schemas
from research_ops.codex.app_server import AppServerCodexAdapter
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.cli import CliCodexAdapter
from research_ops.services.briefs import developer_instructions
from research_ops.services.core import ResearchOpsService
from research_ops.services.goals import _goal_prompt
from research_ops.settings import Settings
from test_api_workflow import create_work

QUERY = "  \u7ee7\u7eed\u5f53\u524d\u5b9e\u9a8c\n\nKeep **my** formatting.\n"


def test_health_reports_the_deployed_input_contract(client):
    assert client.get("/api/health").json()["conversation_input_mode"] == "verbatim"


async def test_web_new_and_resumed_turns_send_only_the_user_text(client, fake_codex, monkeypatch):
    _work, branch = create_work(client)
    service = client.app.state.rops.service
    start = AsyncMock(wraps=fake_codex.start_thread)
    send = AsyncMock(return_value={"turn": {"id": "transparent-turn"}})
    monkeypatch.setattr(fake_codex, "start_thread", start)
    monkeypatch.setattr(fake_codex, "send_message", send)
    for _ in range(2):
        await service.send_message(branch["id"], schemas.ConversationSend(content=QUERY))
    assert start.await_count == 1
    assert start.await_args.kwargs["instructions"] == ""
    assert send.await_count == 2
    for call in send.await_args_list:
        assert call.kwargs["content"] == QUERY
        assert call.kwargs.get("additional_context") is None
    assert (
        send.await_args_list[0].kwargs["thread_id"] == send.await_args_list[1].kwargs["thread_id"]
    )


async def test_large_rollout_keeps_the_original_thread(client, fake_codex, settings, monkeypatch):
    _work, branch = create_work(client)
    service = client.app.state.rops.service
    settings.codex_thread_rollover_bytes = 256_000
    rollout = settings.data_dir / "large.jsonl"
    rollout.write_bytes(b" " * 300_000)
    monkeypatch.setattr("research_ops.services.core.find_rollout", lambda *_: rollout)
    with client.app.state.rops.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        stored.codex_thread_id = "persisted-thread"
        session.commit()
    fake_codex.threads.add("persisted-thread")
    attempt = await service.start_session(branch["id"], schemas.SessionStart(owner="test"))
    assert attempt.codex_thread_id == "persisted-thread"
    assert "thread_recovery" not in service.branch_detail(branch["id"])["branch"].context_snapshot


@pytest.mark.parametrize("error", [
    "Error running remote compact task",
    "context window exceeded",
    "thread not found",
])
async def test_runtime_error_never_replaces_the_thread(
    client, fake_codex, settings, monkeypatch, error,
):
    _work, branch = create_work(client)
    service = client.app.state.rops.service
    settings.codex_transport_retry_attempts = 0
    start = AsyncMock(wraps=fake_codex.start_thread)
    monkeypatch.setattr(fake_codex, "start_thread", start)
    monkeypatch.setattr(fake_codex, "send_message", AsyncMock(side_effect=CodexUnavailable(error)))
    monkeypatch.setattr(
        fake_codex, "resume_thread", AsyncMock(side_effect=CodexUnavailable("no rollout found"))
    )
    with pytest.raises(CodexUnavailable):
        await service.send_message(branch["id"], schemas.ConversationSend(content=QUERY))
    assert start.await_count == 1
    stored = service.branch_detail(branch["id"])["branch"]
    assert stored.codex_thread_id in fake_codex.threads
    assert "thread_recovery" not in stored.context_snapshot


def test_goal_continuation_and_display_metadata_do_not_add_instructions():
    claim = SimpleNamespace(objective=QUERY, policy={"allow_external_submit": False}, state={})
    assert _goal_prompt(claim) == QUERY
    assert ResearchOpsService._continuation_prompt(QUERY) == QUERY
    for brief in [
        {"program_desk": True},
        {"definition_pending": True},
        {"active_goals": [{"objective": "other"}]},
        {"runtime_handoff": {"transcript_excerpt": "other"}},
        {"memory_context": [{"content": "other"}]},
    ]:
        assert developer_instructions(brief) == ""


async def test_app_server_does_not_send_platform_instructions_or_context(tmp_path, monkeypatch):
    adapter = AppServerCodexAdapter(
        Settings(data_dir=tmp_path, workspace_root=tmp_path),
        event_handler=AsyncMock(), request_handler=AsyncMock(),
    )
    request = AsyncMock(return_value={})
    monkeypatch.setattr(adapter, "_request", request)
    await adapter.start_thread(cwd=str(tmp_path), instructions="platform brief")
    await adapter.fork_thread(thread_id="parent", cwd=str(tmp_path), instructions="platform brief")
    await adapter.resume_thread(thread_id="original", cwd=str(tmp_path))
    await adapter.prepare_goal_thread(thread_id="original", cwd=str(tmp_path))
    await adapter.set_goal(thread_id="original", objective=QUERY, status="active")
    await adapter.send_message(
        thread_id="original", cwd=str(tmp_path), content=QUERY, metadata={},
        additional_context={"brief": {"objective": "other"}},
    )
    for call in request.await_args_list:
        assert "developerInstructions" not in call.args[1]
        assert "baseInstructions" not in call.args[1]
        assert "additionalContext" not in call.args[1]
        assert "approvalPolicy" not in call.args[1]
        assert "sandbox" not in call.args[1]
    assert request.await_args_list[-2].args[1]["objective"] == QUERY
    assert request.await_args.args[1]["input"][0]["text"] == QUERY


async def test_cli_native_owner_receives_verbatim_query(tmp_path, monkeypatch):
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path, workspace_root=tmp_path),
        event_handler=AsyncMock(), request_handler=AsyncMock(),
    )
    adapter._started = True
    monkeypatch.setattr("research_ops.codex.cli.find_rollout", lambda *_: tmp_path / "saved.jsonl")
    monkeypatch.setattr(adapter.lifecycle, "owns_thread", AsyncMock(return_value=True))
    send = AsyncMock(return_value={"turn": {"id": "native-turn"}})
    monkeypatch.setattr(adapter, "_send_lifecycle_message", send)
    await adapter.send_message(
        thread_id="original", cwd=str(tmp_path), content=QUERY, metadata={},
        additional_context={"brief": {"objective": "other"}},
    )
    assert send.await_args.kwargs["prompt"] == QUERY
    assert send.await_args.kwargs["thread_id"] == "original"


async def test_cli_missing_rollout_cannot_silently_start_a_new_conversation(tmp_path, monkeypatch):
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path, workspace_root=tmp_path),
        event_handler=AsyncMock(), request_handler=AsyncMock(),
    )
    adapter._started = True
    monkeypatch.setattr("research_ops.codex.cli.find_rollout", lambda *_: None)
    spawn = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(CodexUnavailable, match="refusing to replace its history"):
        await adapter.send_message(
            thread_id="original", cwd=str(tmp_path), content=QUERY, metadata={},
        )
    spawn.assert_not_awaited()


@pytest.mark.parametrize("bound", [False, True])
def test_managed_tui_does_not_receive_a_synthetic_first_prompt(tmp_path, monkeypatch, bound):
    thread_id = "original" if bound else "deferred:new"
    branch = SimpleNamespace(cwd=str(tmp_path), execution_preferences={})
    attempt = SimpleNamespace(
        id="attempt", lease_id="lease", codex_thread_id=thread_id,
        started_at=datetime.now(UTC), execution_config={"identity": "api"},
        brief={"objective": "other"},
    )
    command = ["codex", "-C", str(tmp_path)] + (["resume", thread_id] if bound else [])
    service = Mock()
    service.branch_detail.return_value = {"branch": branch, "runtime": "codex"}
    service.claim_terminal_session.return_value = attempt
    service.codex_terminal_command.return_value = shlex.join(command)
    adapter = SimpleNamespace(
        start=AsyncMock(), stop=AsyncMock(),
        start_thread=AsyncMock(return_value={"thread": {"id": "deferred:new"}}),
    )
    results = Mock()
    results.known_thread_ids.return_value = set()
    state = SimpleNamespace(
        service=service, settings=SimpleNamespace(codex_home=tmp_path),
        codex_results=results, codex=adapter, identities=Mock(),
    )
    monkeypatch.setattr(cli, "runtime", lambda: state)
    monkeypatch.setattr(cli, "adapter_for_execution", lambda *_: adapter)
    monkeypatch.setattr(cli, "find_new_rollout", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "run_db", AsyncMock())
    spawn = Mock(return_value=Mock(poll=Mock(return_value=0)))
    monkeypatch.setattr(cli.subprocess, "Popen", spawn)
    cli.open_branch("branch")
    assert spawn.call_args.args[0] == command
    if not bound:
        assert adapter.start_thread.await_args.kwargs["instructions"] == ""
