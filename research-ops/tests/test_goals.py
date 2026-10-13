from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from research_ops import models, schemas
from research_ops.db import Database
from research_ops.services.goals import GoalService, GoalSupervisor
from research_ops.settings import PROJECT_ROOT, Settings


def build_goal_service(tmp_path: Path) -> tuple[GoalService, Database, models.ExecutionBranch]:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'goals.db'}",
        programs_file=PROJECT_ROOT / "config" / "programs.yaml",
        frontend_dist=tmp_path / "dist",
        codex_enabled=False,
        codex_result_sync_enabled=False,
        codex_local_live_enabled=False,
        codex_registry_enabled=False,
        goals_enabled=False,
        goal_allowed_roots=[tmp_path],
    )
    database = Database(settings)
    database.init()
    with database.session_factory() as session:
        program = models.Program(
            code="COMMON",
            slug="common",
            name="Common",
            objective="Goal tests",
            color="#111111",
        )
        session.add(program)
        session.flush()
        work = models.WorkItem(
            human_id="COMMON-0001",
            program_id=program.id,
            title="Goal work",
            objective="Run a durable goal",
            lifecycle="active",
            flow="running",
        )
        session.add(work)
        session.flush()
        branch = models.ExecutionBranch(
            work_item_id=work.id,
            name="Main",
            slug="main",
            mode="write",
            status="active",
            cwd=str(tmp_path / "branch"),
            runtime="codex",
        )
        session.add(branch)
        session.commit()
        branch_id = branch.id
    with database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch_id)
        assert stored is not None
        branch = stored
    return GoalService(database.session_factory, settings), database, branch


def create_goal(
    service: GoalService,
    branch: models.ExecutionBranch,
    *,
    executor: str = "observer",
    completion: schemas.GoalCompletion | None = None,
) -> models.GoalRun:
    return service.create(
        branch.id,
        schemas.GoalCreate(
            title="Finish deterministic verification",
            objective="Persist observations until the completion contract passes.",
            executor=executor,  # type: ignore[arg-type]
            cadence_seconds=60,
            max_agent_turns=2,
            completion=completion or schemas.GoalCompletion(),
            manual_approval=True,
        ),
    )


def test_goal_requires_explicit_user_approval(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)

    with pytest.raises(Exception, match="Manual approval"):
        service.create(
            branch.id,
            schemas.GoalCreate(
                title="Unapproved",
                objective="Must not start.",
            ),
        )


def test_observer_goal_verifies_checkpoint_and_completes(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)
    root = tmp_path / "checkpoints"
    iteration_dir = root / "iter_0000007"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / ".metadata").write_text("ok", encoding="utf-8")
    (iteration_dir / "common.pt").write_bytes(b"ok")
    for index in range(3):
        (iteration_dir / f"rank_{index:05d}.distcp").write_bytes(b"shard")
    # Write the marker last, which is required before the worker accepts an
    # otherwise complete checkpoint as published.
    (root / "latest_checkpointed_iteration.txt").write_text("7\n", encoding="utf-8")

    goal = create_goal(
        service,
        branch,
        completion=schemas.GoalCompletion(
            kind="checkpoint",
            checkpoint_path=str(root),
            checkpoint_iteration=7,
            expected_shards=3,
        ),
    )
    claim = service.claim_due("worker", limit=1)[0]
    evaluation = service.evaluate_claim(claim, "worker")

    assert evaluation.action == "completed"
    detail = service.detail(goal.id)
    assert detail["goal"].status == "completed"
    evidence = detail["goal"].state_json["completion_evidence"]
    assert evidence["nonempty_shard_count"] == 3
    assert evidence["publication_marker_valid"] is True
    assert detail["events"][-1].event_type == "goal.completed"


def test_external_job_failure_becomes_needs_decision(tmp_path: Path) -> None:
    service, database, branch = build_goal_service(tmp_path)
    with database.session_factory() as session:
        job = models.ExternalJob(
            work_item_id=branch.work_item_id,
            provider="ltp",
            remote_job_id="training-1",
            status="Failed",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    goal = create_goal(
        service,
        branch,
        completion=schemas.GoalCompletion(kind="external_job", external_job_id=job_id),
    )
    claim = service.claim_due("worker", limit=1)[0]
    evaluation = service.evaluate_claim(claim, "worker")

    assert evaluation.action == "needs_decision"
    assert service.detail(goal.id)["goal"].status == "needs_decision"


def test_agent_goal_releases_lease_and_schedules_next_tick(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)
    goal = create_goal(service, branch, executor="agent")
    claim = service.claim_due("worker", limit=1)[0]
    evaluation = service.evaluate_claim(claim, "worker")
    assert evaluation.action == "agent"

    started = service.begin_agent_turn(goal.id, "worker")
    assert started is not None
    service.finish_agent_turn(goal.id, "worker", message_id="message-1", turn_id="turn-1")

    stored = service.detail(goal.id)["goal"]
    assert stored.status == "active"
    assert stored.agent_turns_started == 1
    assert stored.lease_owner is None
    assert stored.next_run_at is not None
    assert stored.state_json["last_agent_turn_id"] == "turn-1"


def test_goal_claim_heartbeat_extends_only_the_current_owner_lease(tmp_path: Path) -> None:
    service, database, branch = build_goal_service(tmp_path)
    goal = create_goal(service, branch, executor="agent")
    service.claim_due("worker", limit=1)
    expiring_at = datetime.now(UTC) + timedelta(seconds=1)
    with database.session_factory() as session:
        stored = session.get(models.GoalRun, goal.id)
        assert stored is not None
        stored.lease_expires_at = expiring_at
        session.commit()

    assert service.renew_claim(goal.id, "another-worker") is False
    assert service.renew_claim(goal.id, "worker") is True

    renewed = service.detail(goal.id)["goal"]
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > expiring_at.replace(tzinfo=None)
    assert renewed.state_json["last_heartbeat_at"]


class FakeBranchService:
    def __init__(self, *, send_delay: float = 0.0) -> None:
        self.messages: list[dict[str, object]] = []
        self.send_delay = send_delay

    def branch_conversation_state(self, _branch_id: str) -> dict[str, object]:
        return {"can_send": True}

    def reconcile_external_activity(self, _work_ids: set[str]) -> None:
        return None

    async def send_message(self, branch_id: str, payload, **kwargs):  # type: ignore[no-untyped-def]
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.messages.append({"branch_id": branch_id, "content": payload.content, **kwargs})
        return SimpleNamespace(id="goal-message", turn_id="goal-turn")


class FakeEventHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    async def publish(self, branch_id: str, event: dict[str, object]) -> None:
        self.events.append((branch_id, event))


def test_supervisor_runs_a_persisted_agent_goal(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)
    goal = create_goal(service, branch, executor="agent")
    fake_branches = FakeBranchService()
    fake_hub = FakeEventHub()
    supervisor = GoalSupervisor(  # type: ignore[arg-type]
        service,
        fake_branches,  # type: ignore[arg-type]
        fake_hub,  # type: ignore[arg-type]
        service.settings,
    )

    async def run() -> None:
        await supervisor.dispatch_due()
        await asyncio.gather(*supervisor._tasks.values())

    asyncio.run(run())

    stored = service.detail(goal.id)["goal"]
    assert stored.agent_turns_started == 1
    assert stored.state_json["last_agent_turn_id"] == "goal-turn"
    assert fake_branches.messages[0]["origin"] == "goal"
    assert any(event[1]["type"] == "goal.updated" for event in fake_hub.events)


def test_supervisor_renews_lease_during_a_long_agent_turn(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)
    goal = create_goal(service, branch, executor="agent")
    fake_branches = FakeBranchService(send_delay=0.05)
    fake_hub = FakeEventHub()
    supervisor = GoalSupervisor(  # type: ignore[arg-type]
        service,
        fake_branches,  # type: ignore[arg-type]
        fake_hub,  # type: ignore[arg-type]
        service.settings,
    )
    supervisor._heartbeat_interval_seconds = lambda: 0.01  # type: ignore[method-assign]

    async def run() -> None:
        await supervisor.dispatch_due()
        await asyncio.gather(*supervisor._tasks.values())

    asyncio.run(run())

    stored = service.detail(goal.id)["goal"]
    assert stored.state_json["last_heartbeat_at"]
    assert stored.state_json["last_agent_turn_id"] == "goal-turn"
    assert sum(event[1]["type"] == "goal.updated" for event in fake_hub.events) >= 2


def test_goal_control_releases_paused_goal_for_resume(tmp_path: Path) -> None:
    service, _database, branch = build_goal_service(tmp_path)
    goal = create_goal(service, branch)
    paused = service.control(goal.id, schemas.GoalControl(action="pause"))
    assert paused.status == "paused"
    assert paused.next_run_at is None

    resumed = service.control(goal.id, schemas.GoalControl(action="resume"))
    assert resumed.status == "active"
    assert resumed.next_run_at is not None


def test_goal_api_exposes_branch_goal_and_controls(client: TestClient) -> None:
    work_response = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Goal API work",
            "objective": "Test the durable goal HTTP surface.",
            "branch_mode": "write",
        },
    )
    assert work_response.status_code == 200, work_response.text
    work = work_response.json()
    tree = client.get("/api/programs/HARN/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == work["id"])

    created = client.post(
        f"/api/branches/{branch['id']}/goals",
        json={
            "title": "Observe the job",
            "objective": "Keep a durable observation record.",
            "executor": "observer",
            "cadence_seconds": 60,
            "manual_approval": True,
        },
    )
    assert created.status_code == 200, created.text
    goal = created.json()
    assert goal["status"] == "active"

    detail = client.get(f"/api/branches/{branch['id']}")
    assert detail.status_code == 200, detail.text
    assert [item["id"] for item in detail.json()["goals"]] == [goal["id"]]
    assert detail.json()["work_item"]["flow"] == "running"

    paused = client.post(f"/api/goals/{goal['id']}/control", json={"action": "pause"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    paused_detail = client.get(f"/api/branches/{branch['id']}")
    assert paused_detail.json()["work_item"]["flow"] == "actionable"
    goal_detail = client.get(f"/api/goals/{goal['id']}")
    assert goal_detail.status_code == 200, goal_detail.text
    assert goal_detail.json()["events"][-1]["event_type"] == "goal.pause"
