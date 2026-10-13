from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from research_ops import models
from research_ops.services.restart_guard import RestartGuard, idle_restart_lock
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_native_goals import _materialized_branch

THREAD_ID = "01234567-89ab-4cde-8fab-0123456789ab"


def write_events(path: Path, *events: tuple[str, str]) -> None:
    path.write_text("".join(
        json.dumps({"type": "event_msg", "payload": {"type": event, "turn_id": turn}}) + "\n"
        for event, turn in events
    ), encoding="utf-8")


@pytest.fixture
def guard_env(client, fake_codex, tmp_path):
    branch = _materialized_branch(client, fake_codex)
    factory = client.app.state.rops.database.session_factory
    with factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        stored.codex_thread_id = THREAD_ID
        binding = session.scalar(select(models.CodexThreadBinding).where(
            models.CodexThreadBinding.branch_id == stored.id
        ))
        binding.thread_id = THREAD_ID
        session.commit()
    branch["codex_thread_id"] = THREAD_ID
    proc = tmp_path / "proc"
    proc.mkdir()
    return RestartGuard(factory, proc_root=proc), branch, factory


def open_writer(guard: RestartGuard, path: Path, *, app_server: bool = False) -> None:
    proc = guard.proc_root / "1234"
    (proc / "fd").mkdir(parents=True)
    (proc / "cmdline").write_bytes(
        b"/opt/codex\0" + (b"app-server\0" if app_server else b"resume\0")
    )
    (proc / "fd" / "7").symlink_to(path)


def add_goal(factory, branch, *, status="active", execution="idle", thread_id=THREAD_ID):
    with factory() as session:
        session.add(models.NativeGoalMonitor(
            branch_id=branch["id"], thread_id=thread_id, goal_key="test-goal",
            goal_snapshot={"status": status, "objective": "Keep the exact objective"},
            execution_status=execution,
        ))
        session.commit()


def test_active_goal_blocks_even_between_turns(guard_env):
    guard, branch, factory = guard_env
    add_goal(factory, branch)
    blockers = guard.blockers()
    assert [item.reason for item in blockers] == ["native_goal_active_or_retrying"]
    assert "Native Goal API" in blockers[0].label


def test_retired_goal_without_writer_does_not_block_forever(guard_env):
    guard, branch, factory = guard_env
    add_goal(factory, branch, thread_id="historical-thread")
    assert guard.blockers() == ()


def test_retired_goal_with_idle_loaded_writer_still_blocks(guard_env, tmp_path):
    guard, branch, factory = guard_env
    old_thread = "01234567-89ab-4cde-8fab-987654321abc"
    add_goal(factory, branch, thread_id=old_thread)
    path = tmp_path / f"rollout-{old_thread}.jsonl"
    write_events(path, ("task_complete", "old-turn"))
    open_writer(guard, path, app_server=True)
    assert guard.blockers()[0].reason == "native_goal_thread_mismatch"


def test_retired_goal_with_native_lock_but_no_rollout_is_not_idle(guard_env, tmp_path):
    guard, branch, factory = guard_env
    old_thread = "01234567-89ab-4cde-8fab-987654321abc"
    add_goal(factory, branch, thread_id=old_thread)
    path = tmp_path / "thread-writer-locks" / f"{old_thread}.lock"
    open_writer(guard, path, app_server=True)
    assert {item.reason for item in guard.blockers()} == {
        "native_goal_thread_mismatch", "codex_writer_unverified",
    }


def test_retry_scheduled_for_blocked_goal_blocks_restart(guard_env):
    guard, branch, factory = guard_env
    add_goal(factory, branch, status="blocked")
    with factory() as session:
        session.get(models.NativeGoalMonitor, branch["id"]).next_retry_at = (
            datetime.now(UTC) + timedelta(minutes=3)
        )
        session.commit()
    assert guard.blockers()[0].reason == "native_goal_active_or_retrying"


@pytest.mark.parametrize("finish", ["task_complete", "turn_aborted"])
def test_blocked_goal_manual_turn_and_idle_open_terminal(guard_env, tmp_path, finish):
    guard, branch, factory = guard_env
    add_goal(factory, branch, status="blocked")
    path = tmp_path / f"rollout-2026-09-12-{THREAD_ID}.jsonl"
    write_events(path, ("task_started", "new-turn"))
    open_writer(guard, path, app_server=True)
    busy = guard.blockers()
    assert len(busy) == 1
    assert busy[0].reason == "codex_turn_running"
    assert busy[0].branch_id == branch["id"]
    write_events(path, ("task_started", "new-turn"), (finish, "new-turn"))
    assert guard.blockers() == ()


def test_historical_unfinished_log_without_writer_does_not_block(guard_env, tmp_path):
    guard, _, _ = guard_env
    write_events(tmp_path / f"rollout-{THREAD_ID}.jsonl", ("task_started", "abandoned"))
    assert guard.blockers() == ()


def test_web_queue_and_lease_block_without_codex_process(guard_env):
    guard, branch, factory = guard_env
    now = datetime.now(UTC)
    with factory() as session:
        session.add(models.ConversationMessage(
            branch_id=branch["id"], role="user", kind="message", content="Queued request",
            metadata_json={"send_status": "pending"},
        ))
        session.add(models.SessionAttempt(
            branch_id=branch["id"], lease_owner="web", status="active",
            heartbeat_at=now, lease_expires_at=now + timedelta(minutes=1), work_item_revision=1,
        ))
        session.commit()
    assert {b.reason for b in guard.blockers()} == {
        "web_request_pending", "web_turn_lease_active"
    }


def test_open_local_cli_is_checked_without_registry_live_flag(guard_env, tmp_path):
    guard, _, _ = guard_env
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    write_events(path, ("task_started", "local-turn"))
    open_writer(guard, path)
    assert guard.blockers()[0].reason == "codex_turn_running"


def test_starting_cli_with_no_log_is_unverified(guard_env, tmp_path):
    guard, _, _ = guard_env
    path = tmp_path / f"rollout-{THREAD_ID}.jsonl"
    open_writer(guard, path)
    (guard.proc_root / "1234/fd/7").unlink()
    assert guard.blockers()[0].reason == "writer_starting_or_unverified"


def test_invalid_partial_and_missing_logs_fail_closed(tmp_path):
    guard = RestartGuard(sessionmaker())
    path = tmp_path / "rollout-test.jsonl"
    assert guard._rollout_state(path) == "unverified"
    for content in ["broken\n", "{}", "null\n", ""]:
        path.write_text(content)
        assert guard._rollout_state(path) == "unverified"


def test_tail_limit_cannot_mistake_long_silent_turn_for_idle(tmp_path):
    guard = RestartGuard(sessionmaker(), max_tail_bytes=256)
    path = tmp_path / "rollout-test.jsonl"
    write_events(path, ("task_started", "long-turn"))
    with path.open("a") as handle:
        for _ in range(50):
            handle.write(json.dumps({"type": "response_item", "payload": {}}) + "\n")
    assert guard._rollout_state(path) == "unverified"


def test_other_turn_completion_does_not_unlock_latest_turn(tmp_path):
    guard = RestartGuard(sessionmaker())
    path = tmp_path / "rollout-test.jsonl"
    write_events(path, ("task_started", "new"), ("task_complete", "old"))
    assert guard._rollout_state(path) == "running"


def test_replaced_rollout_invalidates_cache(tmp_path):
    guard = RestartGuard(sessionmaker())
    path = tmp_path / "rollout-test.jsonl"
    write_events(path, ("task_complete", "old"))
    assert guard._rollout_state(path) == "idle"
    replacement = tmp_path / "replacement.jsonl"
    write_events(replacement, ("task_started", "new"))
    replacement.replace(path)
    assert guard._rollout_state(path) == "running"


def test_idle_restart_lock_excludes_second_monitor_and_releases(tmp_path):
    path = tmp_path / "restart.lock"
    with (
        idle_restart_lock(path),
        pytest.raises(RuntimeError, match="already running"),
        idle_restart_lock(path),
    ):
        pytest.fail("Second monitor acquired the lock")
    with idle_restart_lock(path):
        assert path.read_text().strip().isdigit()
