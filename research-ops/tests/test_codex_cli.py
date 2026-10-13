from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.cli import CliCodexAdapter, _prompt
from research_ops.settings import Settings


async def _noop_event(_method: str, _params: dict[str, Any]) -> None:
    return None


async def _noop_request(_request_id: str, _method: str, _params: dict[str, Any]) -> None:
    return None


@pytest.mark.parametrize(
    "context", [None, {"policy_only": True}, {"brief": {"objective": "other"}}]
)
def test_prompt_is_verbatim_without_platform_instructions(context) -> None:
    content = "  继续当前实验\n\nKeep this spacing.\n"
    assert _prompt(content, context) == content


@pytest.mark.asyncio
async def test_cli_starts_a_real_thread_only_for_an_unbound_branch(tmp_path) -> None:
    args_file = tmp_path / "args.json"
    prompt_file = tmp_path / "prompt.txt"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!/usr/bin/env python3
import json
import sys

with open({str(args_file)!r}, "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
with open({str(prompt_file)!r}, "w", encoding="utf-8") as handle:
    handle.write(sys.stdin.read())
print(json.dumps({{"type": "thread.started", "thread_id": "thread-real"}}))
print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    events: list[tuple[str, dict[str, Any]]] = []

    async def event_handler(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_home=tmp_path / "codex-home",
            codex_command=str(fake_codex),
            codex_model="gpt-5.6-sol",
            codex_model_provider="openai_gateway",
            codex_disabled_mcp_servers=["openaiDeveloperDocs"],
        ),
        event_handler=event_handler,
        request_handler=_noop_request,
    )
    adapter._started = True

    result = await adapter.send_message(
        thread_id="deferred:thread-without-rollout",
        content="first Web turn",
        cwd=str(tmp_path),
        metadata={"research_ops_branch_id": "branch-1"},
        additional_context={"brief": {"work_id": "ADAPT-0001"}},
    )

    assert result["thread"]["id"] == "thread-real"
    started = [params for method, params in events if method == "thread/started"]
    assert len(started) == 1
    assert started[0]["_researchOpsBranchId"] == "branch-1"
    assert started[0]["threadId"] == "thread-real"
    assert started[0]["thread"] == {"id": "thread-real"}
    assert isinstance(started[0]["turnId"], str)
    args = json.loads(args_file.read_text(encoding="utf-8"))
    assert args[:8] == [
        "-c",
        'model_provider="openai_gateway"',
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="max"',
        "exec",
        "--json",
    ]
    assert "resume" not in args
    prompt = prompt_file.read_text(encoding="utf-8")
    assert prompt == "first Web turn"
    assert not any("mcp_servers." in arg for arg in args)


@pytest.mark.asyncio
async def test_cli_resume_uses_branch_cwd_and_skips_git_trust_check(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args_file = tmp_path / "args.json"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!/usr/bin/env python3
import json
import sys

with open({str(args_file)!r}, "w", encoding="utf-8") as handle:
    json.dump(sys.argv[1:], handle)
print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    rollout = tmp_path / "existing-thread.jsonl"
    monkeypatch.setattr("research_ops.codex.cli.find_rollout", lambda _root, _thread_id: rollout)
    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_home=tmp_path / "codex-home",
            codex_command=str(fake_codex),
            codex_model="gpt-5.6-sol",
            codex_model_provider="openai_gateway",
            codex_disabled_mcp_servers=[],
        ),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter._started = True

    branch_cwd = tmp_path / "branch-without-git"
    branch_cwd.mkdir()
    await adapter.send_message(
        thread_id="thread-existing",
        content="continue",
        cwd=str(branch_cwd),
        metadata={"research_ops_branch_id": "branch-1"},
    )

    args = json.loads(args_file.read_text(encoding="utf-8"))
    assert args[:8] == [
        "-c",
        'model_provider="openai_gateway"',
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="max"',
        "-C",
        str(branch_cwd.resolve()),
    ]
    assert args[8:] == [
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "thread-existing",
        "-",
    ]


@pytest.mark.asyncio
async def test_cli_fork_releases_app_server_writer_after_lineage_operation(tmp_path) -> None:
    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_home=tmp_path / "codex-home",
        ),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    stopped = False

    async def fake_fork(**_kwargs):
        return {"thread": {"id": "child-thread"}}

    async def fake_stop():
        nonlocal stopped
        stopped = True

    adapter.lifecycle.fork_thread = fake_fork  # type: ignore[method-assign]
    adapter.lifecycle.stop = fake_stop  # type: ignore[method-assign]

    result = await adapter.fork_thread(
        thread_id="parent-thread",
        cwd=str(tmp_path),
        instructions="fork",
    )

    assert result == {"thread": {"id": "child-thread"}}
    assert stopped is True


@pytest.mark.asyncio
async def test_native_goal_read_releases_non_active_thread_writer(tmp_path) -> None:
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter.lifecycle.get_goal = AsyncMock(return_value={"goal": None})  # type: ignore[method-assign]
    adapter.lifecycle.read_goal_thread = AsyncMock(
        return_value={"thread": {"status": {"type": "idle"}}}
    )
    adapter.lifecycle.unsubscribe_thread = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "unsubscribed"}
    )

    assert await adapter.get_goal(thread_id="thread-idle") == {"goal": None}
    await asyncio.gather(*adapter._goal_release_tasks)
    adapter.lifecycle.unsubscribe_thread.assert_awaited_once_with(thread_id="thread-idle")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pausing_native_goal_releases_thread_writer(tmp_path) -> None:
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter._native_goal_threads.add("thread-goal")
    adapter.lifecycle.set_goal = AsyncMock(  # type: ignore[method-assign]
        return_value={"goal": {"threadId": "thread-goal", "status": "paused"}}
    )
    adapter.lifecycle.read_goal_thread = AsyncMock(
        return_value={"thread": {"status": {"type": "idle"}}}
    )
    adapter.lifecycle.unsubscribe_thread = AsyncMock(  # type: ignore[method-assign]
        return_value={"status": "unsubscribed"}
    )

    result = await adapter.set_goal(thread_id="thread-goal", status="paused")
    await asyncio.gather(*adapter._goal_release_tasks)

    assert result["goal"]["status"] == "paused"
    assert "thread-goal" not in adapter._native_goal_threads
    adapter.lifecycle.unsubscribe_thread.assert_awaited_once_with(thread_id="thread-goal")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_paused_goal_keeps_final_events_until_current_turn_finishes(tmp_path) -> None:
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter.lifecycle.read_goal_thread = AsyncMock(
        return_value={"thread": {"status": {"type": "active"}}}
    )
    adapter.lifecycle.unsubscribe_thread = AsyncMock()
    await adapter._handle_lifecycle_event(
        "thread/goal/updated",
        {
            "threadId": "thread",
            "goal": {"status": "paused"},
        },
    )
    await asyncio.gather(*adapter._goal_release_tasks)
    adapter.lifecycle.unsubscribe_thread.assert_not_awaited()
    adapter.lifecycle.read_goal_thread.return_value = {"thread": {"status": {"type": "idle"}}}
    await adapter._handle_lifecycle_event("turn/completed", {"threadId": "thread"})
    await asyncio.gather(*adapter._goal_release_tasks)
    adapter.lifecycle.unsubscribe_thread.assert_awaited_once_with(thread_id="thread")


def _owned_goal_adapter(tmp_path, monkeypatch):
    adapter = CliCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter._started = True
    adapter.lifecycle.process = SimpleNamespace(returncode=None)
    adapter.lifecycle.owns_thread = AsyncMock(return_value=True)
    adapter.lifecycle.prepare_goal_thread = AsyncMock(
        return_value={"thread": {"id": "thread", "status": {"type": "idle"}}}
    )
    adapter.lifecycle.read_goal_thread = AsyncMock(
        return_value={"thread": {"status": {"type": "idle"}}}
    )
    adapter.lifecycle.unsubscribe_thread = AsyncMock()
    adapter.lifecycle.interrupt_turn = AsyncMock()
    adapter.lifecycle.set_goal = AsyncMock()
    monkeypatch.setattr("research_ops.codex.cli.find_rollout", lambda *_args: tmp_path / "rollout")
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("Must reuse the existing thread writer")),
    )
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_status", ["blocked", "paused", "complete"])
async def test_web_message_reuses_inactive_goal_writer(tmp_path, monkeypatch, goal_status):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    adapter.lifecycle.get_goal = AsyncMock(return_value={"goal": {"status": goal_status}})
    await adapter.get_goal(thread_id="thread")
    await asyncio.gather(*adapter._goal_release_tasks)
    adapter.lifecycle.unsubscribe_thread.reset_mock()
    seen = []

    async def event(method, params):
        seen.append((method, params))

    adapter.event_handler = event

    async def send(**kwargs):
        assert kwargs["content"].endswith("Use another available cluster")
        assert kwargs["cwd"] == str(tmp_path)
        await adapter._handle_lifecycle_event(
            "turn/started",
            {"threadId": "thread", "turn": {"id": "web-turn", "status": "inProgress"}},
        )
        await asyncio.gather(*adapter._goal_release_tasks)
        adapter.lifecycle.unsubscribe_thread.assert_not_awaited()
        await adapter._handle_lifecycle_event(
            "turn/completed",
            {"threadId": "thread", "turn": {"id": "prior-turn", "status": "failed"}},
        )
        await adapter._handle_lifecycle_event(
            "turn/completed",
            {"threadId": "thread", "turn": {"id": "web-turn", "status": "completed"}},
        )
        return {"turn": {"id": "web-turn"}}

    adapter.lifecycle.send_message = AsyncMock(side_effect=send)
    result = await adapter.send_message(
        thread_id="thread", content="Use another available cluster", cwd=str(tmp_path),
        metadata={"research_ops_branch_id": "branch"},
    )
    assert result == {"thread": {"id": "thread"}, "turn": {"id": "web-turn"}}
    assert all(params["_researchOpsBranchId"] == "branch" for _, params in seen)
    adapter.lifecycle.prepare_goal_thread.assert_awaited_once_with(
        thread_id="thread", cwd=str(tmp_path)
    )
    adapter.lifecycle.send_message.assert_awaited_once()
    adapter.lifecycle.set_goal.assert_not_awaited()
    assert not adapter._lifecycle_web_turns
    await asyncio.gather(*adapter._goal_release_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_owned_goal_web_message_reports_terminal_error(tmp_path, monkeypatch, status):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)

    async def send(**_kwargs):
        await adapter._handle_lifecycle_event(
            "turn/completed",
            {"threadId": "thread", "turn": {
                "id": "web-turn", "status": status, "error": {"message": "Model rejected turn"},
            }},
        )
        return {"turn": {"id": "web-turn"}}

    adapter.lifecycle.send_message = AsyncMock(side_effect=send)
    with pytest.raises(CodexUnavailable, match="Model rejected turn"):
        await adapter.send_message(
            thread_id="thread", content="Continue", cwd=str(tmp_path), metadata={}
        )
    adapter.lifecycle.send_message.assert_awaited_once()
    assert not adapter._lifecycle_web_turns
    await asyncio.gather(*adapter._goal_release_tasks)


@pytest.mark.asyncio
async def test_owned_goal_web_message_can_be_cancelled(tmp_path, monkeypatch):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    adapter.lifecycle.send_message = AsyncMock(return_value={"turn": {"id": "web-turn"}})
    task = asyncio.create_task(adapter.send_message(
        thread_id="thread", content="Continue", cwd=str(tmp_path), metadata={}
    ))
    while not adapter.lifecycle.send_message.await_count:
        await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    adapter.lifecycle.interrupt_turn.assert_awaited_once_with(
        thread_id="thread", turn_id="web-turn"
    )
    assert not adapter._lifecycle_web_turns
    await asyncio.gather(*adapter._goal_release_tasks)


@pytest.mark.asyncio
async def test_owned_goal_web_message_detects_app_server_exit(tmp_path, monkeypatch):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    adapter.lifecycle.send_message = AsyncMock(return_value={"turn": {"id": "web-turn"}})
    adapter.lifecycle.process.returncode = 1
    with pytest.raises(CodexUnavailable, match="App Server exited"):
        await adapter.send_message(
            thread_id="thread", content="Continue", cwd=str(tmp_path), metadata={}
        )
    assert not adapter._lifecycle_web_turns
    await asyncio.gather(*adapter._goal_release_tasks)


@pytest.mark.asyncio
async def test_owned_active_goal_accepts_steering_on_same_turn(tmp_path, monkeypatch):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    adapter._native_goal_threads.add("thread")
    adapter.lifecycle.prepare_goal_thread.return_value["thread"]["status"]["type"] = "active"
    adapter.lifecycle.read_goal_turn = AsyncMock(
        return_value={"id": "goal-turn", "status": "inProgress"}
    )
    adapter.lifecycle.send_message = AsyncMock()

    async def steer(**_kwargs):
        await adapter._handle_lifecycle_event(
            "turn/completed",
            {"threadId": "thread", "turn": {"id": "goal-turn", "status": "completed"}},
        )
        return {"turnId": "goal-turn"}

    adapter.lifecycle.steer_message = AsyncMock(side_effect=steer)
    result = await adapter.send_message(
        thread_id="thread", content="Change cluster", cwd=str(tmp_path), metadata={}
    )
    assert result["turn"]["id"] == "goal-turn"
    assert adapter.lifecycle.steer_message.await_args.kwargs["turn_id"] == "goal-turn"
    assert adapter.lifecycle.steer_message.await_args.kwargs["content"].endswith("Change cluster")
    adapter.lifecycle.send_message.assert_not_awaited()
    adapter.lifecycle.set_goal.assert_not_awaited()
    await asyncio.gather(*adapter._goal_release_tasks)
    adapter.lifecycle.unsubscribe_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_server_thread_ownership_uses_loaded_threads_only(tmp_path, monkeypatch):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    from research_ops.codex.app_server import AppServerCodexAdapter

    adapter.lifecycle._request = AsyncMock(side_effect=[
        {"data": ["another-thread"], "nextCursor": "next"},
        {"data": ["thread"], "nextCursor": None},
    ])
    assert await AppServerCodexAdapter.owns_thread(adapter.lifecycle, thread_id="thread")
    assert adapter.lifecycle._request.await_args.args == (
        "thread/loaded/list", {"cursor": "next"},
    )
    adapter.lifecycle.process = None
    assert not await AppServerCodexAdapter.owns_thread(adapter.lifecycle, thread_id="thread")
    assert adapter.lifecycle._request.await_count == 2


@pytest.mark.asyncio
async def test_interrupt_native_turn_uses_existing_app_server(tmp_path, monkeypatch):
    adapter = _owned_goal_adapter(tmp_path, monkeypatch)
    adapter.lifecycle.interrupt_turn.return_value = {}
    result = await adapter.interrupt_turn(thread_id="thread", turn_id="running-turn")
    assert result == {}
    adapter.lifecycle.interrupt_turn.assert_awaited_once_with(
        thread_id="thread", turn_id="running-turn"
    )


@pytest.mark.asyncio
async def test_cli_surfaces_structured_error_instead_of_only_exit_status(tmp_path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import sys

print(json.dumps({"type": "error", "message": "no rollout found for thread id stale"}))
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_home=tmp_path / "codex-home",
            codex_command=str(fake_codex),
        ),
        event_handler=_noop_event,
        request_handler=_noop_request,
    )
    adapter._started = True

    with pytest.raises(CodexUnavailable, match="no rollout found for thread id stale"):
        await adapter.send_message(
            thread_id="deferred:test",
            content="probe",
            cwd=str(tmp_path),
            metadata={"research_ops_branch_id": "branch-1"},
        )


@pytest.mark.asyncio
async def test_cli_accepts_jsonl_events_larger_than_asyncio_default(tmp_path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json

print(json.dumps({"type": "thread.started", "thread_id": "thread-live"}))
print(json.dumps({
    "type": "item.completed",
    "item": {"id": "item_0", "type": "agent_message", "text": "x" * 70000},
}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    events: list[tuple[str, dict[str, Any]]] = []

    async def event_handler(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    async def request_handler(_request_id: str, _method: str, _params: dict[str, Any]) -> None:
        return None

    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_command=str(fake_codex),
            codex_event_line_limit_bytes=1024 * 1024,
        ),
        event_handler=event_handler,
        request_handler=request_handler,
    )
    adapter._started = True

    result = await adapter.send_message(
        thread_id="deferred:test",
        content="probe",
        cwd=str(tmp_path),
        metadata={"research_ops_branch_id": "branch-1"},
    )

    assert result["thread"]["id"] == "thread-live"
    completed = [params for method, params in events if method == "item/completed"]
    assert len(completed[0]["item"]["text"]) == 70000


@pytest.mark.asyncio
async def test_cli_reaps_process_when_event_exceeds_configured_limit(tmp_path) -> None:
    pid_file = tmp_path / "fake-codex.pid"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import time

with open({str(pid_file)!r}, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
print(json.dumps({{
    "type": "item.completed",
    "item": {{"id": "item_0", "type": "agent_message", "text": "x" * 70000}},
}}), flush=True)
time.sleep(60)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    async def event_handler(_method: str, _params: dict[str, Any]) -> None:
        return None

    async def request_handler(_request_id: str, _method: str, _params: dict[str, Any]) -> None:
        return None

    adapter = CliCodexAdapter(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            codex_command=str(fake_codex),
            codex_event_line_limit_bytes=65536,
        ),
        event_handler=event_handler,
        request_handler=request_handler,
    )
    adapter._started = True

    with pytest.raises(CodexUnavailable, match="longer than limit|exceed the limit"):
        await adapter.send_message(
            thread_id="deferred:test",
            content="probe",
            cwd=str(tmp_path),
            metadata={"research_ops_branch_id": "branch-1"},
        )

    process_id = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
