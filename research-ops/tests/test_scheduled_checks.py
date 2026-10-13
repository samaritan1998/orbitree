import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from research_ops import models
from research_ops.api import create_app
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.rollout import CompletedTurn
from research_ops.services.codex_results import CodexResultSync
from research_ops.services.scheduled_checks import (
    ScheduledCheckService,
    ScheduledCheckWorker,
    check_outcome,
)
from research_ops.services.turn_notifications import TurnNotificationService
from sqlalchemy import select
from test_api_workflow import create_work
from test_turn_notifications import FakeSender, _attempt_and_binding


def create_check(client, **changes):
    work, branch = create_work(client)
    payload = {"prompt": "  检查 job-123\n只检查一次。\n", "interval_minutes": 10, **changes}
    response = client.put(f"/api/branches/{branch['id']}/scheduled-check", json=payload)
    assert response.status_code == 200, response.text
    return work, branch, response.json()["check"]


def due(state, check_id):
    with state.database.session_factory() as session:
        check = session.get(models.ScheduledCheck, check_id)
        check.next_run_at = datetime.now(UTC) - timedelta(minutes=3)
        session.commit()


async def drain(state):
    tasks = list(state.scheduled_check_worker.tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    tasks = list(state.service._web_turn_tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 5)


def answer_with(state, fake_codex, monkeypatch, answer, seen=None):
    async def send(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        turn_id = models.new_id()
        with state.database.session_factory() as session:
            session.add(
                models.ConversationMessage(
                    branch_id=kwargs["metadata"]["research_ops_branch_id"],
                    turn_id=turn_id,
                    role="assistant",
                    kind="message",
                    content=answer,
                )
            )
            session.commit()
        return {"turn": {"id": turn_id}}

    monkeypatch.setattr(fake_codex, "send_message", send)


def test_schedule_is_persistent_and_does_not_start_a_goal_or_query(client, settings):
    _, branch, check = create_check(client)
    assert check["prompt"] == "  检查 job-123\n只检查一次。\n"
    assert check["execution"]["identity"] == "api"
    assert datetime.fromisoformat(check["next_run_at"].replace("Z", "+00:00")) > datetime.now(UTC)
    factory = client.app.state.rops.database.session_factory
    reopened = ScheduledCheckService(factory, settings)
    assert reopened.detail(branch["id"])["check"]["id"] == check["id"]
    with factory() as session:
        for model in (models.GoalRun, models.ConversationMessage, models.SessionAttempt):
            assert not list(session.scalars(select(model)))


@pytest.mark.parametrize(
    "enabled,tick_seconds,heartbeat_age,startup_age,error,expected",
    [
        (False, 15, None, 120, None, "disabled"),
        (True, 15, None, 0, None, "starting"),
        (True, 15, None, 61, None, "stale"),
        (True, 15, 10, 120, None, "healthy"),
        (True, 15, 61, 120, None, "stale"),
        (True, 15, 10, 120, "OperationalError", "error"),
        (True, 120, 180, 600, None, "healthy"),
        (True, 120, 361, 600, None, "stale"),
    ],
)
def test_worker_health_uses_server_time_and_configured_tick(
    settings, enabled, tick_seconds, heartbeat_age, startup_age, error, expected
):
    settings.scheduled_checks_enabled = enabled
    settings.scheduled_check_tick_seconds = tick_seconds
    worker = ScheduledCheckWorker(SimpleNamespace(settings=settings), None, None)
    now = datetime.now(UTC)
    worker.started_at = now - timedelta(seconds=startup_age)
    worker.last_tick_at = (
        now - timedelta(seconds=heartbeat_age) if heartbeat_age is not None else None
    )
    worker.last_error = error

    result = worker.health()

    assert result["status"] == expected
    assert result["enabled"] == enabled
    assert result["error"] == error
    assert datetime.fromisoformat(result["checked_at"]) >= now
    assert result["stale_after_seconds"] == max(60, tick_seconds * 3)


def test_schedule_detail_reports_live_worker_health_without_cache(client):
    _, branch = create_work(client)
    state = client.app.state.rops
    state.settings.scheduled_checks_enabled = True
    state.scheduled_check_worker.last_tick_at = datetime.now(UTC) - timedelta(seconds=5)

    response = client.get(f"/api/branches/{branch['id']}/scheduled-check")

    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["worker"]["status"] == "healthy"
    assert response.json()["check"] is None
    state.scheduled_check_worker.last_tick_at -= timedelta(minutes=2)
    assert client.get(f"/api/branches/{branch['id']}/scheduled-check").json()["worker"][
        "status"
    ] == "stale"


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": " "},
        {"interval_minutes": 0},
        {"interval_minutes": 10081},
        {"stop_mode": "deadline"},
        {"stop_mode": "deadline", "stop_at": "2030-01-01T01:00"},
        {"stop_mode": "job"},
        {"job_id": "unrelated"},
        {"unknown": True},
    ],
)
def test_invalid_schedule_is_rejected(client, payload):
    _, branch = create_work(client)
    response = client.put(
        f"/api/branches/{branch['id']}/scheduled-check", json={"prompt": "Check only", **payload}
    )
    assert response.status_code == 422


async def test_exact_prompt_no_overlap_and_frozen_execution(client, fake_codex, monkeypatch):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    seen = []
    executions = []
    answer_with(state, fake_codex, monkeypatch, "Still running.\n检查结果：无变化", seen)
    monkeypatch.setattr(
        fake_codex,
        "for_execution",
        lambda value: executions.append(value) or fake_codex,
        raising=False,
    )
    with state.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        stored.execution_preferences = {"identity": "api", "model": "new-model"}
        session.commit()
    due(state, check["id"])
    assert len(state.scheduled_checks.claim_due()) == 1
    assert state.scheduled_checks.claim_due() == []
    run_id = state.scheduled_checks.detail(branch["id"])["check"]["active_run_id"]
    await state.scheduled_check_worker._start(run_id)
    await drain(state)
    state.scheduled_checks.reconcile()
    assert len(seen) == 1
    assert seen[0]["content"] == check["prompt"]
    assert "additional_context" not in seen[0]
    assert all(value["model"] == check["execution"]["model"] for value in executions)
    result = state.scheduled_checks.detail(branch["id"])
    assert result["check"]["phase"] == "scheduled"
    assert result["check"]["last_outcome"] == "unchanged"
    assert result["runs"][0]["message_id"]
    assert result["check"]["active_run_id"] is None
    assert not state.scheduled_checks.claim_due()
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ScheduledCheckNotification)))


async def test_busy_branch_defers_without_interrupting(client, monkeypatch):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    due(state, check["id"])
    monkeypatch.setattr(
        state.service,
        "branch_conversation_state",
        lambda _id: {
            "can_send": False,
            "reason": "Local CLI turn running",
        },
    )
    await state.scheduled_check_worker.tick()
    await drain(state)
    detail = state.scheduled_checks.detail(branch["id"])
    assert detail["check"]["phase"] == "waiting_session"
    assert detail["check"]["active_run_id"] is None
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))


async def test_stop_between_claim_and_send_does_not_send(client):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    due(state, check["id"])
    (run_id,) = state.scheduled_checks.claim_due()
    state.scheduled_checks.control(branch["id"], "stop")
    await state.scheduled_check_worker._start(run_id)
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "stopped"
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))


async def test_active_native_goal_defers_without_modifying_goal(client):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        stored.codex_thread_id = "native-thread"
        session.add(
            models.NativeGoalMonitor(
                branch_id=branch["id"],
                thread_id="native-thread",
                goal_key="original",
                goal_snapshot={"status": "active", "objective": "Do work"},
            )
        )
        session.commit()
    due(state, check["id"])
    await state.scheduled_check_worker.tick()
    await drain(state)
    detail = state.scheduled_checks.detail(branch["id"])
    assert detail["check"]["phase"] == "waiting_session"
    assert "Goal" in detail["check"]["reason"]


async def test_pause_does_not_cancel_running_check_or_external_task(
    client, fake_codex, monkeypatch
):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(**_kwargs):
        started.set()
        await finish.wait()
        return {"turn": {"id": "paused-turn"}}

    monkeypatch.setattr(fake_codex, "send_message", send)
    due(state, check["id"])
    await state.scheduled_check_worker.tick()
    await asyncio.wait_for(started.wait(), 5)
    state.scheduled_checks.control(branch["id"], "pause")
    assert any(not task.done() for task in state.service._web_turn_tasks.values())
    finish.set()
    await drain(state)
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "paused"


def test_deadline_and_fresh_job_terminal_stop_without_touching_jobs(client, settings):
    work, branch, check = create_check(client)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        job = models.ExternalJob(
            work_item_id=work["id"],
            provider="ltp",
            remote_job_id="job-1",
            status="failed",
            last_observed_at=now - timedelta(days=1),
        )
        session.add(job)
        session.flush()
        stored = session.get(models.ScheduledCheck, check["id"])
        stored.stop_mode, stored.job_id = "job", job.id
        session.commit()
        job_id = job.id
    state.scheduled_checks.reconcile()
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "active"
    with state.database.session_factory() as session:
        job = session.get(models.ExternalJob, job_id)
        job.last_observed_at = now
        session.commit()
    state.scheduled_checks.reconcile()
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "completed"
    with state.database.session_factory() as session:
        assert session.get(models.ExternalJob, job_id).status == "failed"
        assert len(list(session.scalars(select(models.ScheduledCheckNotification)))) == 1
    state.scheduled_checks.reconcile()
    with state.database.session_factory() as session:
        assert len(list(session.scalars(select(models.ScheduledCheckNotification)))) == 1
        stored = session.get(models.ScheduledCheck, check["id"])
        stored.status, stored.stop_mode = "active", "deadline"
        stored.stop_at = now - timedelta(seconds=1)
        stored.revision += 1
        session.commit()
    state.scheduled_checks.reconcile()
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "completed"


async def test_failures_back_off_pause_and_use_one_notification_channel(
    client, settings, fake_codex, monkeypatch
):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True

    async def fail(**_kwargs):
        raise CodexUnavailable("Authentication rejected")

    monkeypatch.setattr(fake_codex, "send_message", fail)
    for _ in range(3):
        due(state, check["id"])
        await state.scheduled_check_worker.tick()
        await drain(state)
        state.scheduled_checks.reconcile()
    result = state.scheduled_checks.detail(branch["id"])
    assert result["check"]["status"] == "paused"
    assert result["check"]["consecutive_errors"] == 3
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationFailureNotification)))
        assert len(list(session.scalars(select(models.ScheduledCheckNotification)))) == 2


async def test_notification_filter_and_delivery(client, settings, fake_codex, monkeypatch):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True
    for answer in [
        "Still running.\n检查结果：无变化",
        "Step 10.\n检查结果：有进展",
        "Step 10.\n检查结果：有进展",
    ]:
        answer_with(state, fake_codex, monkeypatch, answer)
        due(state, check["id"])
        await state.scheduled_check_worker.tick()
        await drain(state)
        state.scheduled_checks.reconcile()
    sender = FakeSender()
    notifier = TurnNotificationService(state.database.session_factory, settings, sender)
    assert len(notifier.dispatch_pending()) == 1
    assert notifier.dispatch_pending() == []
    assert len(sender.sent) == 1
    assert "定时检查 · 有进展" in sender.sent[0][1]
    assert "Verifier experiment" in sender.sent[0][1]
    assert f"/branches/{branch['id']}/results" in sender.sent[0][1]


def test_outcome_is_explicit_final_line_only():
    assert check_outcome("日志里写了失败，仍需排查。\n检查结果：无变化") == "unchanged"
    assert check_outcome("检查结果：完成\n但这只是引用的旧日志") == "unclassified"
    assert check_outcome("检查结果：需要决定") == "needs_input"


def test_restart_does_not_replay_unconfirmed_run(client, settings):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    due(state, check["id"])
    (run_id,) = state.scheduled_checks.claim_due()
    with state.database.session_factory() as session:
        run = session.get(models.ScheduledCheckRun, run_id)
        run.created_at = datetime.now(UTC) - timedelta(minutes=6)
        session.commit()
    recovered = ScheduledCheckService(state.database.session_factory, settings)
    recovered.reconcile()
    assert recovered.detail(branch["id"])["check"]["status"] == "paused"
    assert recovered.claim_due() == []


def test_bound_job_must_belong_to_the_work_item(client):
    work, _ = create_work(client)
    _, branch = create_work(client)
    with client.app.state.rops.database.session_factory() as session:
        job = models.ExternalJob(work_item_id=work["id"], provider="test", remote_job_id="other")
        session.add(job)
        session.commit()
        job_id = job.id
    response = client.put(
        f"/api/branches/{branch['id']}/scheduled-check",
        json={
            "prompt": "Check",
            "stop_mode": "job",
            "job_id": job_id,
        },
    )
    assert response.status_code == 409


async def test_pause_while_preparing_releases_only_its_new_attempt(client, monkeypatch):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    prepare = state.service._ensure_web_attempt

    async def paused_prepare(branch_id, **kwargs):
        attempt = await prepare(branch_id, **kwargs)
        state.scheduled_checks.control(branch_id, "pause")
        return attempt

    monkeypatch.setattr(state.service, "_ensure_web_attempt", paused_prepare)
    due(state, check["id"])
    (run_id,) = state.scheduled_checks.claim_due()
    await state.scheduled_check_worker._start(run_id)
    with state.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(
            session.scalars(
                select(models.SessionAttempt).where(
                    models.SessionAttempt.status.in_(["starting", "active"]),
                )
            )
        )


async def test_independent_branches_run_in_parallel(client, fake_codex, monkeypatch):
    state = client.app.state.rops
    checks = [create_check(client)[2] for _ in range(3)]
    seen, release, started = [], asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        seen.append(kwargs["metadata"]["research_ops_branch_id"])
        if len(seen) == 3:
            started.set()
        await release.wait()
        return {"turn": {"id": models.new_id()}}

    monkeypatch.setattr(fake_codex, "send_message", send)
    for check in checks:
        due(state, check["id"])
    await state.scheduled_check_worker.tick()
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert len(set(seen)) == 3
        assert len(state.service._web_turn_tasks) == 3
    finally:
        release.set()
        await drain(state)


def test_jsonl_result_keeps_check_provenance_and_suppresses_regular_notice(client, tmp_path):
    _, branch = create_work(client)
    state = client.app.state.rops
    service = CodexResultSync(
        state.database.session_factory, tmp_path, notification_recipient="test"
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    completed = CompletedTurn(
        external_turn_id="native-check-turn",
        prompt="Check exact job",
        final_answer="检查结果：无变化",
        started_at=now,
        completed_at=now + timedelta(seconds=1),
        duration_ms=1000,
        time_to_first_token_ms=10,
        end_offset=1,
    )
    with state.database.session_factory() as session:
        attempt, binding = _attempt_and_binding(session, branch, owner="web")
        attempt.completed_at = now + timedelta(seconds=2)
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                session_id=attempt.id,
                turn_id="native-check-turn",
                role="user",
                content="Check exact job",
                metadata_json={
                    "origin": "web",
                    "scheduled_check_id": "check",
                    "scheduled_check_run_id": "run",
                },
            )
        )
        session.flush()
        turn = service._upsert_turn(
            session,
            binding,
            completed,
            SimpleNamespace(
                metadata=SimpleNamespace(cli_version="test", forked_from_id=None),
            ),
        )
        assert turn.metadata_json["scheduled_check_run_id"] == "run"
        assert turn.prompt == "Check exact job"
        assert not list(session.scalars(select(models.TurnNotification)))


def test_linked_job_uses_newer_ltp_monitor_observation(client):
    work, branch, check = create_check(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        job = models.ExternalJob(
            work_item_id=work["id"],
            provider="ltp",
            remote_job_id="exact-job",
            status="Running",
            last_observed_at=now - timedelta(hours=2),
        )
        session.add(job)
        session.flush()
        stored = session.get(models.ScheduledCheck, check["id"])
        stored.stop_mode, stored.job_id = "job", job.id
        session.add(
            models.LtpMonitoredJob(
                user="alice",
                job_name="exact-job",
                state="succeeded",
                first_observed_at=now,
                last_observed_at=now,
                last_status_at=now,
            )
        )
        session.commit()
    state.scheduled_checks.reconcile()
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "completed"


def test_deleted_job_pauses_and_cannot_resume_without_rebinding(client):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        stored = session.get(models.ScheduledCheck, check["id"])
        stored.stop_mode = "job"
        session.commit()
    state.scheduled_checks.reconcile()
    assert state.scheduled_checks.detail(branch["id"])["check"]["status"] == "paused"
    response = client.post(
        f"/api/branches/{branch['id']}/scheduled-check/control", json={"action": "resume"}
    )
    assert response.status_code == 409


@pytest.mark.parametrize("mode,expected", [("all", 2), ("none", 0)])
async def test_notification_modes(client, settings, fake_codex, monkeypatch, mode, expected):
    _, branch, check = create_check(client, notification_mode=mode)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True
    answer_with(state, fake_codex, monkeypatch, "Still running.\n检查结果：无变化")
    for _ in range(2):
        due(state, check["id"])
        await state.scheduled_check_worker.tick()
        await drain(state)
        state.scheduled_checks.reconcile()
    with state.database.session_factory() as session:
        assert len(list(session.scalars(select(models.ScheduledCheckNotification)))) == expected


def test_lifespan_worker_dispatches_without_browser_polling(settings, fake_codex, monkeypatch):
    configured = settings.model_copy(
        update={
            "scheduled_checks_enabled": True,
            "scheduled_check_tick_seconds": 1,
        }
    )
    with TestClient(create_app(configured, fake_codex)) as client:
        _, branch, check = create_check(client)
        state = client.app.state.rops
        seen = []
        answer_with(state, fake_codex, monkeypatch, "检查结果：无变化", seen)
        due(state, check["id"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if state.scheduled_checks.detail(branch["id"])["check"]["last_outcome"]:
                break
            time.sleep(0.05)
        detail = state.scheduled_checks.detail(branch["id"])
        assert detail["check"]["last_outcome"] == "unchanged"
        assert len(seen) == 1
        assert state.scheduled_check_worker.last_tick_at is not None


async def test_interrupted_check_pauses_future_checks(client, fake_codex, monkeypatch):
    _, branch, check = create_check(client)
    state = client.app.state.rops

    async def interrupted(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(fake_codex, "send_message", interrupted)
    due(state, check["id"])
    await state.scheduled_check_worker.tick()
    tasks = list(state.scheduled_check_worker.tasks.values())
    await asyncio.gather(*tasks)
    await asyncio.gather(*list(state.service._web_turn_tasks.values()), return_exceptions=True)
    state.scheduled_checks.reconcile()
    detail = state.scheduled_checks.detail(branch["id"])
    assert detail["check"]["status"] == "paused"
    assert detail["check"]["last_outcome"] == "interrupted"
    assert detail["check"]["next_run_at"] is None


async def test_notification_failure_retries_only_delivery(
    client, settings, fake_codex, monkeypatch
):
    _, branch, check = create_check(client)
    state = client.app.state.rops
    settings.infoflow_notifications_enabled = True
    answer_with(state, fake_codex, monkeypatch, "服务启动失败。\n检查结果：失败")
    due(state, check["id"])
    await state.scheduled_check_worker.tick()
    await drain(state)
    state.scheduled_checks.reconcile()
    sender = FakeSender("Recipient offline")
    notifier = TurnNotificationService(state.database.session_factory, settings, sender)
    assert [item.status for item in notifier.dispatch_pending()] == ["failed"]
    assert (
        state.scheduled_checks.detail(branch["id"])["notification"]["error"] == "Recipient offline"
    )
    with state.database.session_factory() as session:
        notice = session.scalar(select(models.ScheduledCheckNotification))
        notice.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    sender.error = None
    assert [item.status for item in notifier.dispatch_pending()] == ["sent"]
    assert len(state.scheduled_checks.detail(branch["id"])["runs"]) == 1
