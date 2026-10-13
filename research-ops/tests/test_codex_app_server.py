from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from research_ops.codex.app_server import AppServerCodexAdapter
from research_ops.settings import Settings


@pytest.mark.asyncio
async def test_stop_cancels_pending_event_callbacks(tmp_path):
    adapter = AppServerCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
    )
    callback = asyncio.create_task(asyncio.sleep(60))
    adapter._handler_tasks.add(callback)
    await adapter.stop()
    assert callback.cancelled()
    assert not adapter._handler_tasks


@pytest.mark.asyncio
async def test_initialize_enables_metadata_only_fork_capability(tmp_path, monkeypatch) -> None:
    adapter = AppServerCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
    )
    process = Mock(returncode=None)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    request = AsyncMock(return_value={})
    monkeypatch.setattr(adapter, "_request", request)
    monkeypatch.setattr(adapter, "_notify", AsyncMock())
    monkeypatch.setattr(adapter, "_read_stdout", AsyncMock())
    monkeypatch.setattr(adapter, "_read_stderr", AsyncMock())

    try:
        await adapter.start()
        method, params = request.await_args.args
        assert method == "initialize"
        assert params["capabilities"]["experimentalApi"] is True
        assert spawn.await_args.kwargs["limit"] == adapter.settings.codex_event_line_limit_bytes
    finally:
        process.returncode = 0
        await adapter.stop()


@pytest.mark.asyncio
async def test_fork_keeps_history_out_of_rpc_response(tmp_path, monkeypatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        codex_model="gpt-6-astra",
        codex_fork_timeout_seconds=120,
    )
    adapter = AppServerCodexAdapter(
        settings,
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
    )
    result = {"thread": {"id": "child-thread", "turns": []}}
    request = AsyncMock(return_value=result)
    monkeypatch.setattr(adapter, "_request", request)

    assert (
        await adapter.fork_thread(
            thread_id="parent-thread", cwd=str(tmp_path), instructions="parallel fork"
        )
        == result
    )

    method, params = request.await_args.args
    assert method == "thread/fork"
    assert params["threadId"] == "parent-thread"
    assert params["excludeTurns"] is True
    assert params["ephemeral"] is False
    assert params["cwd"] == str(tmp_path.resolve())
    assert "developerInstructions" not in params
    assert params["model"] == "gpt-6-astra"
    assert "lastTurnId" not in params
    assert request.await_args.kwargs["timeout"] == 120


@pytest.mark.asyncio
async def test_native_goal_methods_use_codex_thread_goal_rpc(tmp_path, monkeypatch) -> None:
    adapter = AppServerCodexAdapter(
        Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces"),
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
    )
    request = AsyncMock(
        side_effect=[
            {"goal": None},
            {"goal": {"threadId": "thread-1", "status": "active"}},
            {"goal": {"threadId": "thread-1", "status": "paused"}},
            {"cleared": True},
            {"status": "unsubscribed"},
        ]
    )
    monkeypatch.setattr(adapter, "_request", request)

    assert await adapter.get_goal(thread_id="thread-1") == {"goal": None}
    await adapter.set_goal(
        thread_id="thread-1",
        objective="Finish the migration",
        status="active",
        token_budget=250_000,
        update_token_budget=True,
    )
    await adapter.set_goal(thread_id="thread-1", status="paused")
    assert await adapter.clear_goal(thread_id="thread-1") == {"cleared": True}
    assert await adapter.unsubscribe_thread(thread_id="thread-1") == {"status": "unsubscribed"}

    assert request.await_args_list[0].args == (
        "thread/goal/get",
        {"threadId": "thread-1"},
    )
    assert request.await_args_list[1].args == (
        "thread/goal/set",
        {
            "threadId": "thread-1",
            "objective": "Finish the migration",
            "status": "active",
            "tokenBudget": 250_000,
        },
    )
    assert request.await_args_list[2].args == (
        "thread/goal/set",
        {"threadId": "thread-1", "status": "paused"},
    )
    assert request.await_args_list[3].args == (
        "thread/goal/clear",
        {"threadId": "thread-1"},
    )
    assert request.await_args_list[4].args == (
        "thread/unsubscribe",
        {"threadId": "thread-1"},
    )
