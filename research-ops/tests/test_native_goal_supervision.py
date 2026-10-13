from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from research_ops import models
from research_ops.codex.app_server import AppServerCodexAdapter
from research_ops.codex.base import CodexUnavailable
from research_ops.services.native_goal_watch import transient_error
from research_ops.services.native_goals import NativeGoalService
from research_ops.services.turn_notifications import TurnNotificationService
from sqlalchemy import select
from test_native_goals import _materialized_branch
from test_turn_notifications import FakeSender


@pytest.fixture
def goal_context(client, fake_codex, settings):
    branch = _materialized_branch(client, fake_codex)
    thread_id = branch["codex_thread_id"]
    fake_codex.goals[thread_id] = {
        "threadId": thread_id,
        "objective": "用户原始目标\n不附加描述",
        "status": "active",
        "tokenBudget": None,
        "tokensUsed": 12,
        "timeUsedSeconds": 3,
        "createdAt": 1,
        "updatedAt": 2,
    }
    configured = settings.model_copy(
        update={
            "infoflow_notifications_enabled": True,
            "infoflow_public_base_url": "http://research-ops.example",
            "native_goal_supervision_enabled": True,
        }
    )
    factory = client.app.state.rops.database.session_factory
    service = NativeGoalService(factory, configured, fake_codex)
    service.watch.observe_goal(branch["id"], thread_id, fake_codex.goals[thread_id])
    return SimpleNamespace(
        branch=branch,
        thread_id=thread_id,
        service=service,
        factory=factory,
        settings=configured,
        adapter=fake_codex,
    )


def _event(context, method, **params):
    asyncio.run(
        context.service.observe_event(
            context.branch["id"],
            method,
            {
                "threadId": context.thread_id,
                **params,
            },
        )
    )


def _due(context):
    with context.factory() as session:
        row = session.get(models.NativeGoalMonitor, context.branch["id"])
        assert row.next_retry_at is not None
        row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def test_objective_is_forwarded_verbatim_without_a_synthetic_turn(client, fake_codex):
    branch = _materialized_branch(client, fake_codex)
    objective = "  实测 256K\n\n保留我的目标原文。  "
    endpoint = f"/api/branches/{branch['id']}/native-goal"
    response = client.put(endpoint, json={"objective": objective})
    assert response.status_code == 200, response.text
    assert fake_codex.goals[branch["codex_thread_id"]]["objective"] == objective
    assert response.json()["goal"]["objective"] == objective
    assert fake_codex.goals[branch["codex_thread_id"]]["tokenBudget"] is None
    with client.app.state.rops.database.session_factory() as session:
        assert session.scalar(select(models.GoalRun)) is None
        assert session.scalar(select(models.ConversationMessage)) is None
    assert client.put(endpoint, json={"objective": " \n "}).status_code == 422


@pytest.mark.parametrize(
    "status", ["paused", "blocked", "usageLimited", "budgetLimited", "complete"]
)
def test_restart_never_reactivates_a_nonactive_goal(goal_context, status):
    c = goal_context
    c.adapter.goals[c.thread_id]["status"] = status
    result = asyncio.run(c.service.recover_active())
    assert result["recovered"] == 0
    assert c.adapter.goal_resumes == []
    assert c.adapter.goals[c.thread_id]["status"] == status


def test_restart_restores_only_current_visible_branch(goal_context):
    c = goal_context
    with c.factory() as session:
        session.get(models.ExecutionBranch, c.branch["id"]).hidden = True
        session.commit()
    assert asyncio.run(c.service.recover_active())["checked"] == 0
    assert c.adapter.goal_resumes == []


def test_transient_failure_waits_then_resumes_unchanged_goal(goal_context):
    c = goal_context
    original = dict(c.adapter.goals[c.thread_id])
    _event(c, "thread/goal/updated", goal=original)
    _event(c, "turn/started", turn={"id": "turn-a"})
    _event(
        c,
        "turn/completed",
        turn={
            "id": "turn-a",
            "status": "failed",
            "error": {"message": "Our servers are overloaded"},
        },
    )
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    assert c.adapter.goal_resumes == []
    assert c.service.watch.runtime(c.branch["id"]).status == "retrying"
    _due(c)
    assert asyncio.run(c.service.monitor_once())["recovered"] == 1
    assert c.adapter.goal_resumes == [c.thread_id]
    assert c.adapter.goals[c.thread_id] == original
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == "running"
    assert runtime.retry_attempts == 1
    assert runtime.next_retry_at is None


@pytest.mark.parametrize(
    "error,tool",
    [
        ("Request blocked", False),
        ("403 Forbidden", False),
        ("usage limit reached", False),
        ("stream disconnected", True),
        ("interrupted", False),
    ],
)
def test_failure_does_not_replay_unsafe_or_ineligible_turn(goal_context, error, tool):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "turn/started", turn={"id": "turn-a"})
    if tool:
        _event(c, "item/started", turnId="turn-a", item={"type": "commandExecution"})
    _event(
        c, "turn/completed", turn={"id": "turn-a", "status": "failed", "error": {"message": error}}
    )
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    asyncio.run(c.service.monitor_once())
    assert c.adapter.goal_resumes == []
    assert c.service.watch.runtime(c.branch["id"]).next_retry_at is None


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Concurrency limit exceeded for user, please retry later", True),
        ("HTTP 429 rate_limit_exceeded", True),
        ("HTTP 429 insufficient_quota", False),
        ("HTTP 429", False),
        ("403 forbidden: concurrency limit exceeded", False),
        ("stream disconnected UsageLimitExceeded", False),
        ("stream disconnected before completion", True),
    ],
)
def test_transient_errors_distinguish_rate_limits_from_quota(error, expected):
    assert transient_error(error) is expected


@pytest.mark.parametrize("status", ["completed", "failed", "declined"])
def test_settled_tools_allow_delayed_resume_without_replaying_work(goal_context, status):
    c = goal_context
    original = dict(c.adapter.goals[c.thread_id])
    _event(c, "thread/goal/updated", goal=original)
    _event(c, "turn/started", turn={"id": "turn-a"})
    item = {"id": "tool-a", "type": "commandExecution"}
    _event(c, "item/started", turnId="turn-a", item=item)
    _event(c, "item/completed", turnId="turn-a", item={**item, "status": status})
    _event(c, "item/started", turnId="turn-a", item=item)
    _event(c, "turn/started", turn={"id": "turn-a"})
    c.adapter.send_message = AsyncMock()
    _event(
        c,
        "error",
        turnId="turn-a",
        error={"message": "HTTP 429 Concurrency limit exceeded for user"},
    )
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        assert row.turn_tool_state == {"tool-a": status}
        assert row.next_retry_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(seconds=55)
        assert row.retry_attempts == 0
    _due(c)
    assert asyncio.run(c.service.monitor_once())["recovered"] == 1
    assert c.adapter.goals[c.thread_id] == original
    assert c.adapter.goal_resumes == [c.thread_id]
    c.adapter.send_message.assert_not_called()
    sender = FakeSender()
    TurnNotificationService(c.factory, c.settings, sender).dispatch_pending()
    assert any("目标等待重试" in text for _, text in sender.sent)
    assert any("目标执行已恢复" in text for _, text in sender.sent)
    assert not any("请核验工具结果" in text for _, text in sender.sent)


def test_retry_waits_for_all_tools_and_ignores_stale_completions(goal_context):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "turn/started", turn={"id": "turn-a"})
    first = {"id": "tool-a", "type": "commandExecution"}
    second = {"id": "tool-b", "type": "fileChange"}
    for item in (first, second):
        _event(c, "item/started", turnId="turn-a", item=item)
    _event(c, "item/completed", turnId="turn-a", item={**first, "status": "completed"})
    _event(c, "error", turnId="turn-a", error={"message": "stream disconnected"})
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    _event(c, "item/completed", turnId="old-turn", item={**second, "status": "completed"})
    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    assert c.service.watch.runtime(c.branch["id"]).next_retry_at is None
    _event(c, "item/completed", turnId="turn-a", item={**second, "status": "completed"})
    _event(c, "item/completed", turnId="turn-a", item={**second, "status": "completed"})
    with c.factory() as session:
        retries = list(
            session.scalars(
                select(models.NativeGoalNotification).where(
                    models.NativeGoalNotification.kind == "retrying"
                )
            )
        )
        assert len(retries) == 1
    _due(c)
    assert asyncio.run(c.service.monitor_once())["recovered"] == 1


@pytest.mark.parametrize("tool_status", [None, "inProgress", "interrupted"])
def test_uncertain_tool_outcome_survives_restart_and_blocks_recovery(goal_context, tool_status):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "turn/started", turn={"id": "turn-a"})
    _event(
        c,
        "item/completed",
        turnId="turn-a",
        item={"id": "tool-a", "type": "dynamicToolCall", "status": tool_status},
    )
    _event(c, "error", turnId="turn-a", error={"message": "Concurrency limit exceeded for user"})
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    restarted = NativeGoalService(c.factory, c.settings, c.adapter)
    assert asyncio.run(restarted.recover_active())["recovered"] == 0
    assert restarted.watch.runtime(c.branch["id"]).next_retry_at is None
    assert not restarted.watch.begin_recovery(c.branch["id"], automatic=True)


def test_old_tool_guard_is_reconciled_from_matching_persisted_failed_turn(goal_context):
    c = goal_context
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        row.turn_id = "turn-a"
        row.turn_has_side_effects = True
        row.turn_tool_state = None
        row.last_error = "stream disconnected: Concurrency limit exceeded for user"
        row.last_error_at = datetime.now(UTC)
        session.commit()
    c.adapter.goal_turns[c.thread_id] = {
        "id": "turn-a",
        "status": "failed",
        "itemsView": "full",
        "items": [{"id": "tool-a", "type": "commandExecution", "status": "completed"}],
    }
    assert asyncio.run(c.service.recover_active())["recovered"] == 0
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        assert row.turn_tool_state == {"tool-a": "completed"}
        assert row.next_retry_at is not None
    _due(c)
    restarted = NativeGoalService(c.factory, c.settings, c.adapter)
    assert asyncio.run(restarted.recover_active())["recovered"] == 1
    assert c.adapter.goal_resumes == [c.thread_id]


@pytest.mark.parametrize("turn_status", ["completed", "inProgress"])
def test_restart_can_verify_settled_tools_for_active_legacy_goals(goal_context, turn_status):
    c = goal_context
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        row.turn_id = "turn-a"
        row.turn_has_side_effects = True
        row.turn_tool_state = None
        session.commit()
    c.adapter.goal_turns[c.thread_id] = {
        "id": "turn-a",
        "status": turn_status,
        "itemsView": "full",
        "items": [{"id": "tool-a", "type": "commandExecution", "status": "completed"}],
    }
    assert asyncio.run(c.service.recover_active())["recovered"] == 1
    assert c.adapter.goal_resumes == [c.thread_id]


@pytest.mark.parametrize(
    "change",
    [
        {"id": "other-turn"},
        {"itemsView": "summary"},
        {"items": []},
        {"status": "interrupted"},
    ],
)
def test_incomplete_or_unrelated_history_does_not_clear_old_guard(goal_context, change):
    c = goal_context
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        row.turn_id = "turn-a"
        row.turn_has_side_effects = True
        row.last_error = "Concurrency limit exceeded for user"
        row.last_error_at = datetime.now(UTC)
        session.commit()
    c.adapter.goal_turns[c.thread_id] = {
        "id": "turn-a",
        "status": "failed",
        "itemsView": "full",
        "items": [{"id": "tool-a", "type": "commandExecution", "status": "completed"}],
        **change,
    }
    assert asyncio.run(c.service.recover_active())["recovered"] == 0
    assert c.service.watch.runtime(c.branch["id"]).next_retry_at is None


def test_tool_state_schema_upgrade_keeps_old_records_unknown(goal_context):
    from research_ops.db import Database

    c = goal_context
    engine = c.factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE native_goal_monitors DROP COLUMN turn_tool_state")
    database = Database(c.settings)
    database.init()
    database.init()
    with database.session_factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        assert row.turn_tool_state is None
        assert row.goal_snapshot["objective"] == "用户原始目标\n不附加描述"
    database.engine.dispose()


def test_turn_verification_reads_one_full_turn_without_retaining_outputs():
    adapter = AppServerCodexAdapter(
        SimpleNamespace(), event_handler=AsyncMock(), request_handler=AsyncMock()
    )
    adapter._request = AsyncMock(
        return_value={
            "data": [
                {
                    "id": "turn-a",
                    "status": "failed",
                    "itemsView": "full",
                    "items": [
                        {
                            "id": "tool-a",
                            "type": "commandExecution",
                            "status": "completed",
                            "aggregatedOutput": "private output",
                        }
                    ],
                }
            ]
        }
    )
    result = asyncio.run(adapter.read_goal_turn(thread_id="thread-a"))
    assert result["items"] == [{"id": "tool-a", "type": "commandExecution", "status": "completed"}]
    adapter._request.assert_awaited_once_with(
        "thread/turns/list",
        {
            "threadId": "thread-a",
            "limit": 1,
            "sortDirection": "desc",
            "itemsView": "full",
        },
    )


def test_manual_pause_wins_over_pending_retry_and_late_events(goal_context):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "error", error={"message": "stream disconnected"}, willRetry=False)
    old = dict(c.adapter.goals[c.thread_id])
    c.adapter.goals[c.thread_id]["status"] = "paused"
    _event(c, "thread/goal/updated", goal=old)
    _event(c, "error", error={"message": "503 service unavailable"}, willRetry=False)
    asyncio.run(c.service.monitor_once())
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == "paused"
    assert runtime.next_retry_at is None
    assert c.adapter.goal_resumes == []


def test_manual_interrupt_is_not_undone_by_a_late_tool_result(goal_context):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "turn/started", turn={"id": "turn-a"})
    item = {"id": "tool-a", "type": "commandExecution"}
    _event(c, "item/started", turnId="turn-a", item=item)
    _event(c, "error", turnId="turn-a", error={"message": "stream disconnected"})
    _event(c, "turn/completed", turn={"id": "turn-a", "status": "interrupted"})
    _event(c, "item/completed", turnId="turn-a", item={**item, "status": "completed"})
    assert asyncio.run(c.service.recover_active())["recovered"] == 0
    assert c.service.watch.runtime(c.branch["id"]).status == "paused"
    assert c.service.watch.runtime(c.branch["id"]).next_retry_at is None
    assert c.adapter.goal_resumes == []


def test_stale_clear_does_not_erase_replacement_goal(goal_context):
    c = goal_context
    c.adapter.goals[c.thread_id].update(objective="新的目标", createdAt=3, updatedAt=3)
    _event(c, "thread/goal/cleared")
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        assert row.goal_snapshot["objective"] == "新的目标"
    c.service.watch.observe_goal(
        c.branch["id"],
        c.thread_id,
        {
            "threadId": c.thread_id,
            "objective": "old",
            "status": "active",
            "createdAt": 1,
            "updatedAt": 2,
        },
    )
    with c.factory() as session:
        assert (
            session.get(models.NativeGoalMonitor, c.branch["id"]).goal_snapshot["objective"]
            == "新的目标"
        )


def test_active_writer_is_visible_without_takeover_or_retry_exhaustion(goal_context):
    c = goal_context
    c.adapter.prepare_goal_thread = AsyncMock(
        side_effect=CodexUnavailable("already has an active writer")
    )
    for _ in range(5):
        assert asyncio.run(c.service.monitor_once())["recovered"] == 0
        runtime = asyncio.run(c.service.get(c.branch["id"])).runtime
        assert runtime.status == "external_owner"
        assert runtime.retry_attempts == 0
        assert runtime.last_error is None
    assert c.adapter.goals[c.thread_id]["status"] == "active"


def test_failed_recovery_is_bounded_and_not_retried_before_due(goal_context):
    c = goal_context
    c.adapter.prepare_goal_thread = AsyncMock(
        side_effect=CodexUnavailable("503 service unavailable")
    )
    asyncio.run(c.service.monitor_once())
    assert c.adapter.prepare_goal_thread.await_count == 1
    asyncio.run(c.service.monitor_once())
    assert c.adapter.prepare_goal_thread.await_count == 1
    for _ in range(2):
        _due(c)
        asyncio.run(c.service.monitor_once())
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.retry_attempts == 3
    assert runtime.next_retry_at is None
    assert runtime.status == "error"
    asyncio.run(c.service.monitor_once())
    assert c.adapter.prepare_goal_thread.await_count == 3


@pytest.mark.parametrize(
    "operation,method", [("get_goal", "thread/goal/get"), ("read_goal_thread", "thread/read")]
)
@pytest.mark.parametrize("goal_status", ["active", "blocked"])
def test_status_timeout_does_not_retry_a_goal_or_send_failure_notices(
    goal_context, operation, method, goal_status
):
    c = goal_context
    c.adapter.goals[c.thread_id]["status"] = goal_status
    c.service.watch.observe_goal(c.branch["id"], c.thread_id, c.adapter.goals[c.thread_id])
    with c.factory() as session:
        for notice in session.scalars(select(models.NativeGoalNotification)):
            session.delete(notice)
        session.commit()
    c.adapter.loaded_goal_threads.add(c.thread_id)
    original_operation = getattr(c.adapter, operation)
    setattr(c.adapter, operation, AsyncMock(
        side_effect=CodexUnavailable(f"Codex request timed out: {method}")
    ))

    for _ in range(3):
        assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    state = asyncio.run(c.service.get(c.branch["id"]))
    assert state.goal.status == goal_status
    assert state.reason
    assert state.runtime.status == "unavailable"
    assert state.runtime.retry_attempts == 0
    assert state.runtime.next_retry_at is None
    assert c.adapter.goal_resumes == []
    with c.factory() as session:
        assert list(session.scalars(select(models.NativeGoalNotification))) == []

    setattr(c.adapter, operation, original_operation)
    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == ("running" if goal_status == "active" else "blocked")
    assert runtime.last_error is None
    assert c.adapter.goals[c.thread_id]["status"] == goal_status
    assert c.adapter.goal_resumes == []


@pytest.mark.parametrize("discover", [False, True])
def test_replaced_session_does_not_observe_or_recover_the_old_goal(goal_context, discover):
    c = goal_context
    with c.factory() as session:
        session.get(models.ExecutionBranch, c.branch["id"]).codex_thread_id = "replacement-thread"
        session.commit()
    c.adapter.get_goal = AsyncMock(
        side_effect=CodexUnavailable("Codex request timed out: thread/goal/get")
    )
    result = asyncio.run(c.service.monitor_once(discover=discover))
    assert result == {"checked": int(discover), "recovered": 0}
    assert c.adapter.goal_resumes == []
    assert not c.service.watch.begin_recovery(c.branch["id"], automatic=True)
    c.service.watch.failure(c.branch["id"], "503 service unavailable")
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        assert row.thread_id == c.thread_id
        assert row.last_error is None
        assert row.next_retry_at is None
        assert row.retry_attempts == 0
        assert list(session.scalars(select(models.NativeGoalNotification))) == []


def test_resume_timeout_waits_for_status_before_resuming_again(goal_context):
    c = goal_context

    async def delayed_resume(*, thread_id, cwd):
        c.adapter.loaded_goal_threads.add(thread_id)
        raise CodexUnavailable("Codex request timed out: thread/resume")

    c.adapter.prepare_goal_thread = AsyncMock(side_effect=delayed_resume)
    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == "unavailable"
    assert runtime.retry_attempts == 0
    assert runtime.next_retry_at is None

    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    assert c.adapter.prepare_goal_thread.await_count == 1
    assert c.service.watch.runtime(c.branch["id"]).status == "running"
    with c.factory() as session:
        assert list(session.scalars(select(models.NativeGoalNotification))) == []


def test_status_check_clears_legacy_rpc_failure_without_resuming(goal_context):
    c = goal_context
    c.adapter.loaded_goal_threads.add(c.thread_id)
    with c.factory() as session:
        row = session.get(models.NativeGoalMonitor, c.branch["id"])
        row.last_error = "Codex request timed out: thread/read"
        row.execution_status = "retrying"
        row.retryable_error = True
        row.retry_attempts = 3
        row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    assert c.adapter.goal_resumes == []
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == "running"
    assert runtime.last_error is None
    assert runtime.retry_attempts == 0
    assert runtime.next_retry_at is None


def test_status_timeout_preserves_a_confirmed_execution_failure(goal_context):
    c = goal_context
    c.service.watch.failure(c.branch["id"], "503 service unavailable")
    c.adapter.get_goal = AsyncMock(
        side_effect=CodexUnavailable("Codex request timed out: thread/goal/get")
    )
    for _ in range(2):
        assert asyncio.run(c.service.monitor_once())["recovered"] == 0
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.last_error == "503 service unavailable"
    assert runtime.retry_attempts == 0
    assert runtime.next_retry_at is not None
    with c.factory() as session:
        notices = list(session.scalars(select(models.NativeGoalNotification)))
        assert len(notices) == 1
        assert notices[0].payload["error"] == "503 service unavailable"


@pytest.mark.parametrize("status", ["complete", "paused", "blocked", None])
def test_confirmed_goal_transition_still_notifies_after_status_timeout(goal_context, status):
    c = goal_context
    c.service.watch.observation_failed(
        c.branch["id"], "Codex request timed out: thread/read"
    )
    goal = {**c.adapter.goals[c.thread_id], "status": status} if status else None
    c.service.watch.observe_goal(c.branch["id"], c.thread_id, goal)
    with c.factory() as session:
        notices = list(session.scalars(select(models.NativeGoalNotification)))
        assert len(notices) == 1
        assert notices[0].kind == ("state" if status else "cleared")
        assert notices[0].payload["goal_status"] == (status or "cleared")
        assert notices[0].payload["error"] is None


def test_fallback_failure_handler_treats_rpc_timeout_as_an_observation(goal_context):
    c = goal_context
    assert c.service.watch.begin_recovery(c.branch["id"], automatic=True)
    c.service.watch.failure(c.branch["id"], "Codex request timed out: thread/goal/get")
    runtime = c.service.watch.runtime(c.branch["id"])
    assert runtime.status == "unavailable"
    assert runtime.retry_attempts == 0
    assert runtime.next_retry_at is None
    with c.factory() as session:
        assert list(session.scalars(select(models.NativeGoalNotification))) == []


def test_progress_and_state_notifications_are_durable_and_deduplicated(goal_context):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "item/completed", item={"type": "agentMessage", "text": "已完成第一组对照"})
    with c.factory() as session:
        session.get(models.NativeGoalMonitor, c.branch["id"]).last_progress_at = datetime.now(
            UTC
        ) - timedelta(minutes=16)
        session.commit()
    c.service.watch.checked(c.branch["id"], "running")
    c.service.watch.checked(c.branch["id"], "running")
    c.adapter.goals[c.thread_id]["status"] = "paused"
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    with c.factory() as session:
        notices = list(
            session.scalars(
                select(models.NativeGoalNotification).order_by(
                    models.NativeGoalNotification.created_at
                )
            )
        )
        assert [row.kind for row in notices] == ["progress", "state"]
        assert notices[0].payload["summary"] == "已完成第一组对照"
    sender = FakeSender()
    dispatcher = TurnNotificationService(c.factory, c.settings, sender)
    assert [r.status for r in dispatcher.dispatch_pending()] == ["sent", "sent"]
    assert dispatcher.dispatch_pending() == []
    assert all(c.thread_id in text and c.branch["id"] in text for _, text in sender.sent)
    assert "Native Goal API" in sender.sent[0][1]
    assert "目标进度" in sender.sent[0][1]
    assert "等待手动 Resume" in sender.sent[1][1]
    assert c.service.watch.runtime(c.branch["id"]).notification_status == "sent"


def test_error_notification_retries_delivery_without_retrying_goal(goal_context):
    c = goal_context
    c.service.watch.failure(c.branch["id"], "Request blocked")
    sender = FakeSender("temporary delivery failure")
    dispatcher = TurnNotificationService(c.factory, c.settings, sender)
    assert dispatcher.dispatch_pending()[0].status == "failed"
    assert (
        c.service.watch.runtime(c.branch["id"]).notification_error == "temporary delivery failure"
    )
    assert c.adapter.goal_resumes == []
    with c.factory() as session:
        notice = session.scalar(select(models.NativeGoalNotification))
        notice.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    sender.error = None
    assert dispatcher.dispatch_pending()[0].status == "sent"
    assert dispatcher.dispatch_pending() == []
    assert "目标执行失败" in sender.sent[-1][1]


def test_presampling_error_without_turn_started_is_not_lost(goal_context):
    c = goal_context
    _event(c, "thread/goal/updated", goal=c.adapter.goals[c.thread_id])
    _event(c, "turn/started", turn={"id": "previous"})
    _event(c, "item/started", turnId="previous", item={"type": "commandExecution"})
    _event(c, "turn/completed", turn={"id": "previous", "status": "completed"})
    c.adapter.goals[c.thread_id]["status"] = "blocked"
    _event(c, "thread/goal/updated", turnId="presampling", goal=c.adapter.goals[c.thread_id])
    _event(
        c,
        "error",
        turnId="presampling",
        error={
            "message": "High demand",
            "codexErrorInfo": "internalServerError",
        },
        willRetry=False,
    )
    runtime = c.service.watch.runtime(c.branch["id"])
    assert "High demand" in runtime.last_error
    assert runtime.next_retry_at is not None


def test_resume_rpc_preserves_execution_and_does_not_inject_instructions(settings):
    adapter = AppServerCodexAdapter(
        settings,
        event_handler=AsyncMock(),
        request_handler=AsyncMock(),
        launch=SimpleNamespace(
            execution={
                "model": "unchanged-model",
                "provider": "unchanged-provider",
                "reasoning_effort": "max",
            }
        ),
    )
    adapter._request = AsyncMock(
        side_effect=[
            {"thread": {"status": {"type": "active"}}},
        ]
    )
    asyncio.run(adapter.prepare_goal_thread(thread_id="thread", cwd="/tmp"))
    method, params = adapter._request.call_args.args
    assert method == "thread/resume"
    assert params["excludeTurns"] is True
    assert params["model"] == "unchanged-model"
    assert params["modelProvider"] == "unchanged-provider"
    assert params["config"]["model_reasoning_effort"] == "max"
    assert "developerInstructions" not in params
    assert "baseInstructions" not in params
    assert "input" not in params
