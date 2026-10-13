"""Durable background Goal orchestration for Research Ops.

Goals intentionally sit outside a Codex conversation.  A model can inspect a
goal and perform a bounded checkpoint, but the lifecycle, lease, cadence and
completion evidence are all persisted by this service.  That makes the system
restart-safe and prevents an assistant sentence such as "I will keep watching"
from being mistaken for a live process.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.codex.identity import CodexIdentityService
from research_ops.events import EventHub
from research_ops.settings import Settings

from .core import ConflictError, NotFoundError, ResearchOpsService
from .external_jobs import external_job_phase

GOAL_ACTIVE_STATUSES = {"active"}
GOAL_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
GoalAction = Literal["agent", "idle", "completed", "needs_decision"]


@dataclass(frozen=True)
class GoalClaim:
    id: str
    branch_id: str
    work_item_id: str
    title: str
    objective: str
    executor: str
    policy: dict[str, Any]
    completion: dict[str, Any]
    state: dict[str, Any]
    cadence_seconds: int
    max_agent_turns: int
    agent_turns_started: int


@dataclass(frozen=True)
class GoalEvaluation:
    claim: GoalClaim
    action: GoalAction
    reason: str


class GoalService:
    """Persist Goal lifecycle state and deterministic completion checks."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def create(self, branch_id: str, payload: schemas.GoalCreate) -> models.GoalRun:
        if not payload.manual_approval:
            raise ConflictError("Manual approval is required before activating a Goal")
        now = datetime.now(UTC)
        cadence = payload.cadence_seconds or self.settings.goal_default_cadence_seconds
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.hidden:
                raise ConflictError("Restore this hidden Branch before activating a Goal")
            if branch.mode == "capture":
                raise ConflictError("Capture-only Branches cannot run Goals")
            if branch.status != "active":
                raise ConflictError(f"Branch is {branch.status}; it cannot run a Goal")
            self._validate_completion_contract(session, branch, payload.completion)
            goal = models.GoalRun(
                work_item_id=branch.work_item_id,
                branch_id=branch.id,
                title=payload.title.strip(),
                objective=payload.objective.strip(),
                executor=payload.executor,
                status="active",
                cadence_seconds=cadence,
                max_agent_turns=payload.max_agent_turns,
                policy_json=payload.policy.model_dump(),
                completion_json=payload.completion.model_dump(exclude_none=True),
                state_json={
                    "phase": "scheduled",
                    "consecutive_errors": 0,
                    "created_by": "user_approved_api",
                    "execution": (CodexIdentityService(self.settings).snapshot(
                        branch.execution_preferences
                    ) if branch.runtime == "codex" else {}),
                },
                next_run_at=now,
            )
            session.add(goal)
            session.flush()
            self._event(
                session,
                goal,
                "goal.created",
                "Goal activated by explicit user approval.",
                {
                    "executor": goal.executor,
                    "cadence_seconds": goal.cadence_seconds,
                    "completion": goal.completion_json,
                    "policy": goal.policy_json,
                },
            )
            session.commit()
            return goal

    def list_for_branch(self, branch_id: str) -> list[models.GoalRun]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(models.GoalRun)
                    .where(models.GoalRun.branch_id == branch_id)
                    .order_by(models.GoalRun.created_at.desc())
                )
            )

    def detail(self, goal_id: str, *, event_limit: int = 100) -> dict[str, Any]:
        with self.session_factory() as session:
            goal = session.get(models.GoalRun, goal_id)
            if goal is None:
                raise NotFoundError("Goal not found")
            events = list(
                session.scalars(
                    select(models.GoalEvent)
                    .where(models.GoalEvent.goal_run_id == goal.id)
                    .order_by(models.GoalEvent.created_at.desc())
                    .limit(event_limit)
                )
            )
            events.reverse()
            return {"goal": goal, "events": events}

    def control(self, goal_id: str, payload: schemas.GoalControl) -> models.GoalRun:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = session.get(models.GoalRun, goal_id)
            if goal is None:
                raise NotFoundError("Goal not found")
            action = payload.action
            state = dict(goal.state_json or {})
            if action == "pause":
                if goal.status not in GOAL_ACTIVE_STATUSES:
                    raise ConflictError(f"Goal is {goal.status}; it cannot be paused")
                goal.status = "paused"
                goal.next_run_at = None
                goal.lease_owner = None
                goal.lease_expires_at = None
                state["phase"] = "paused"
                message = "Goal paused by user."
            elif action == "resume":
                if goal.status not in {"paused", "needs_decision", "failed"}:
                    raise ConflictError(f"Goal is {goal.status}; it cannot be resumed")
                goal.status = "active"
                goal.next_run_at = now
                goal.lease_owner = None
                goal.lease_expires_at = None
                goal.error = None
                state["phase"] = "scheduled"
                state["consecutive_errors"] = 0
                message = "Goal resumed by user."
            elif action == "stop":
                if goal.status in GOAL_TERMINAL_STATUSES:
                    raise ConflictError(f"Goal is already {goal.status}")
                goal.status = "stopped"
                goal.next_run_at = None
                goal.lease_owner = None
                goal.lease_expires_at = None
                goal.completed_at = now
                state["phase"] = "stopped"
                message = "Goal stopped by user."
            elif action == "complete":
                if goal.status in GOAL_TERMINAL_STATUSES:
                    raise ConflictError(f"Goal is already {goal.status}")
                goal.status = "completed"
                goal.next_run_at = None
                goal.lease_owner = None
                goal.lease_expires_at = None
                goal.completed_at = now
                state["phase"] = "completed_by_user"
                message = "Goal marked complete by user."
            else:  # run_now
                if goal.status != "active":
                    raise ConflictError(f"Goal is {goal.status}; resume it before running")
                if goal.lease_expires_at and _as_utc(goal.lease_expires_at) > now:
                    raise ConflictError("Goal is already being checked by the worker")
                goal.next_run_at = now
                state["phase"] = "scheduled"
                message = "Goal scheduled for an immediate check."
            goal.state_json = state
            self._event(session, goal, f"goal.{action}", message, {})
            session.commit()
            return goal

    def claim_due(self, owner: str, *, limit: int | None = None) -> list[GoalClaim]:
        """Atomically lease due Goals so only one worker may run each tick."""

        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self.settings.goal_lease_seconds)
        claimed: list[GoalClaim] = []
        with self.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(models.GoalRun.id)
                    .where(
                        models.GoalRun.status == "active",
                        models.GoalRun.next_run_at.is_not(None),
                        models.GoalRun.next_run_at <= now,
                        or_(
                            models.GoalRun.lease_expires_at.is_(None),
                            models.GoalRun.lease_expires_at <= now,
                        ),
                    )
                    .order_by(models.GoalRun.next_run_at, models.GoalRun.created_at)
                    .limit(limit or self.settings.goal_max_dispatch_per_tick)
                )
            )
            for goal_id in candidates:
                result = session.execute(
                    update(models.GoalRun)
                    .where(
                        models.GoalRun.id == goal_id,
                        models.GoalRun.status == "active",
                        models.GoalRun.next_run_at.is_not(None),
                        models.GoalRun.next_run_at <= now,
                        or_(
                            models.GoalRun.lease_expires_at.is_(None),
                            models.GoalRun.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        lease_owner=owner,
                        lease_expires_at=lease_expires_at,
                        last_started_at=now,
                    )
                )
                if result.rowcount != 1:
                    continue
                goal = session.get(models.GoalRun, goal_id)
                if goal is None:
                    continue
                state = dict(goal.state_json or {})
                state["phase"] = "checking"
                state["last_claimed_at"] = now.isoformat()
                goal.state_json = state
                self._event(
                    session,
                    goal,
                    "goal.claimed",
                    "Persistent worker claimed the next goal check.",
                    {"owner": owner, "lease_expires_at": lease_expires_at.isoformat()},
                )
                claimed.append(self._claim(goal))
            if candidates:
                session.commit()
        return claimed

    def evaluate_claim(self, claim: GoalClaim, owner: str) -> GoalEvaluation:
        """Evaluate mechanical completion before invoking an optional agent tick."""

        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = self._claimed_goal(session, claim.id, owner)
            if goal is None:
                return GoalEvaluation(claim, "idle", "Goal claim was released")
            completion_kind = str((goal.completion_json or {}).get("kind") or "manual")
            evidence: dict[str, Any] | None = None
            if completion_kind == "external_job":
                evidence = self._external_job_evidence(session, goal)
                phase = str(evidence["phase"])
                if phase == "completed":
                    self._complete_goal(session, goal, now, evidence)
                    session.commit()
                    return GoalEvaluation(claim, "completed", "External job completed")
                if phase in {"failed", "cancelled"}:
                    self._needs_decision(
                        session,
                        goal,
                        now,
                        "External job reached a terminal non-success state.",
                        evidence,
                    )
                    session.commit()
                    return GoalEvaluation(claim, "needs_decision", "External job needs review")
            elif completion_kind == "checkpoint":
                evidence = self._checkpoint_evidence(goal)
                if evidence["complete"]:
                    self._complete_goal(session, goal, now, evidence)
                    session.commit()
                    return GoalEvaluation(claim, "completed", "Checkpoint evidence verified")

            if goal.executor == "observer":
                self._finish_observation(
                    session,
                    goal,
                    now,
                    evidence or {"phase": "observing", "complete": False},
                )
                session.commit()
                return GoalEvaluation(claim, "idle", "Observation recorded")
            if goal.agent_turns_started >= goal.max_agent_turns:
                self._needs_decision(
                    session,
                    goal,
                    now,
                    "Goal turn budget reached; user review is required before more model turns.",
                    evidence or {},
                )
                session.commit()
                return GoalEvaluation(claim, "needs_decision", "Goal turn budget reached")

            state = dict(goal.state_json or {})
            state["phase"] = "ready_for_agent"
            if evidence is not None:
                state["last_observation"] = evidence
            goal.state_json = state
            self._event(
                session,
                goal,
                "goal.agent_due",
                "Mechanical checks did not finish the Goal; an agent checkpoint is due.",
                {"completion_kind": completion_kind, "evidence": evidence or {}},
            )
            session.commit()
            return GoalEvaluation(claim, "agent", "Agent checkpoint due")

    def renew_claim(self, goal_id: str, owner: str) -> bool:
        """Extend an in-flight Goal lease without adding noisy history events."""

        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self.settings.goal_lease_seconds)
        with self.session_factory() as session:
            goal = session.get(models.GoalRun, goal_id)
            if goal is None or goal.status != "active" or goal.lease_owner != owner:
                return False
            state = dict(goal.state_json or {})
            state["last_heartbeat_at"] = now.isoformat()
            result = session.execute(
                update(models.GoalRun)
                .where(
                    models.GoalRun.id == goal_id,
                    models.GoalRun.status == "active",
                    models.GoalRun.lease_owner == owner,
                )
                .values(
                    lease_expires_at=lease_expires_at,
                    state_json=state,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    def defer_claim(self, goal_id: str, owner: str, reason: str, *, seconds: int = 30) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = self._claimed_goal(session, goal_id, owner)
            if goal is None:
                return
            state = dict(goal.state_json or {})
            state["phase"] = "waiting_branch"
            state["last_deferred_reason"] = reason
            self._release(goal, now, next_run_at=now + timedelta(seconds=max(1, seconds)))
            goal.state_json = state
            self._event(session, goal, "goal.deferred", reason, {"retry_seconds": seconds})
            session.commit()

    def begin_agent_turn(self, goal_id: str, owner: str) -> GoalClaim | None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = self._claimed_goal(session, goal_id, owner)
            if goal is None or goal.status != "active":
                return None
            if goal.agent_turns_started >= goal.max_agent_turns:
                self._needs_decision(
                    session,
                    goal,
                    now,
                    "Goal turn budget reached; user review is required before more model turns.",
                    {},
                )
                session.commit()
                return None
            goal.agent_turns_started += 1
            state = dict(goal.state_json or {})
            state["phase"] = "running_agent"
            state["last_agent_started_at"] = now.isoformat()
            goal.state_json = state
            self._event(
                session,
                goal,
                "goal.agent_started",
                "Persistent worker started a scheduled agent checkpoint.",
                {"agent_turn": goal.agent_turns_started, "max_agent_turns": goal.max_agent_turns},
            )
            session.commit()
            return self._claim(goal)

    def finish_agent_turn(
        self,
        goal_id: str,
        owner: str,
        *,
        message_id: str | None,
        turn_id: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = self._claimed_goal(session, goal_id, owner)
            if goal is None:
                return
            state = dict(goal.state_json or {})
            state.update(
                {
                    "phase": "scheduled",
                    "consecutive_errors": 0,
                    "last_agent_message_id": message_id,
                    "last_agent_turn_id": turn_id,
                    "last_agent_finished_at": now.isoformat(),
                }
            )
            self._release(goal, now, next_run_at=now + timedelta(seconds=goal.cadence_seconds))
            goal.state_json = state
            self._event(
                session,
                goal,
                "goal.agent_finished",
                "Scheduled agent checkpoint finished; the Goal remains under worker control.",
                {"message_id": message_id, "turn_id": turn_id},
            )
            session.commit()

    def fail_agent_turn(self, goal_id: str, owner: str, error: str) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            goal = self._claimed_goal(session, goal_id, owner)
            if goal is None:
                return
            state = dict(goal.state_json or {})
            errors = int(state.get("consecutive_errors") or 0) + 1
            limit = int((goal.policy_json or {}).get("max_consecutive_errors") or 3)
            state["consecutive_errors"] = errors
            state["last_error"] = error
            if errors >= limit:
                self._needs_decision(
                    session,
                    goal,
                    now,
                    f"Goal worker encountered {errors} consecutive errors: {error}",
                    {"consecutive_errors": errors, "last_error": error},
                )
            else:
                retry_seconds = min(goal.cadence_seconds * (2 ** (errors - 1)), 3600)
                state["phase"] = "retrying"
                self._release(
                    goal,
                    now,
                    next_run_at=now + timedelta(seconds=retry_seconds),
                )
                goal.state_json = state
                goal.error = error
                self._event(
                    session,
                    goal,
                    "goal.agent_failed",
                    "Scheduled agent checkpoint failed; worker will retry with backoff.",
                    {"error": error, "consecutive_errors": errors, "retry_seconds": retry_seconds},
                )
            session.commit()

    def release_owner(self, owner: str, *, reason: str) -> int:
        """Release in-flight leases during graceful shutdown for fast recovery."""

        now = datetime.now(UTC)
        released = 0
        with self.session_factory() as session:
            goals = list(
                session.scalars(
                    select(models.GoalRun).where(
                        models.GoalRun.status == "active",
                        models.GoalRun.lease_owner == owner,
                    )
                )
            )
            for goal in goals:
                state = dict(goal.state_json or {})
                state["phase"] = "scheduled"
                state["last_release_reason"] = reason
                self._release(goal, now, next_run_at=now)
                goal.state_json = state
                self._event(session, goal, "goal.lease_released", reason, {})
                released += 1
            if released:
                session.commit()
        return released

    def _validate_completion_contract(
        self,
        session: Session,
        branch: models.ExecutionBranch,
        completion: schemas.GoalCompletion,
    ) -> None:
        if completion.kind == "external_job":
            assert completion.external_job_id
            job = session.get(models.ExternalJob, completion.external_job_id)
            if job is None:
                raise NotFoundError("External Job not found")
            if job.work_item_id != branch.work_item_id:
                raise ConflictError("External Job belongs to a different WorkItem")
        elif completion.kind == "checkpoint":
            assert completion.checkpoint_path
            self._safe_checkpoint_root(completion.checkpoint_path)

    def _external_job_evidence(
        self, session: Session, goal: models.GoalRun
    ) -> dict[str, Any]:
        completion = goal.completion_json or {}
        job = session.get(models.ExternalJob, str(completion.get("external_job_id") or ""))
        if job is None:
            return {"phase": "unknown", "reason": "External Job record is missing"}
        phase = external_job_phase(job.status)
        return {
            "phase": phase,
            "external_job_id": job.id,
            "provider": job.provider,
            "remote_job_id": job.remote_job_id,
            "status": job.status,
            "last_observed_at": job.last_observed_at.isoformat()
            if job.last_observed_at
            else None,
        }

    def _checkpoint_evidence(self, goal: models.GoalRun) -> dict[str, Any]:
        completion = goal.completion_json or {}
        root = self._safe_checkpoint_root(str(completion.get("checkpoint_path") or ""))
        iteration = int(completion.get("checkpoint_iteration") or 0)
        expected_shards = int(completion.get("expected_shards") or 128)
        iteration_dir = root / f"iter_{iteration:07d}"
        tracker = root / "latest_checkpointed_iteration.txt"
        metadata = iteration_dir / ".metadata"
        common = iteration_dir / "common.pt"
        shards = sorted(iteration_dir.glob("*.distcp")) if iteration_dir.is_dir() else []
        nonempty_shards = [item for item in shards if item.is_file() and item.stat().st_size > 0]
        tracker_value: int | None = None
        tracker_error: str | None = None
        if tracker.is_file():
            try:
                tracker_value = int(tracker.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                tracker_error = type(exc).__name__
        marker_valid = False
        if tracker.is_file() and nonempty_shards:
            try:
                newest_shard = max(item.stat().st_mtime for item in nonempty_shards)
                marker_valid = newest_shard <= tracker.stat().st_mtime + 1
            except OSError:
                marker_valid = False
        evidence = {
            "phase": "checkpoint_verification",
            "checkpoint_path": str(root),
            "iteration": iteration,
            "expected_shards": expected_shards,
            "tracker_value": tracker_value,
            "tracker_error": tracker_error,
            "metadata_present": metadata.is_file() and metadata.stat().st_size > 0,
            "common_present": common.is_file() and common.stat().st_size > 0,
            "shard_count": len(shards),
            "nonempty_shard_count": len(nonempty_shards),
            "publication_marker_valid": marker_valid,
        }
        evidence["complete"] = bool(
            evidence["metadata_present"]
            and evidence["common_present"]
            and tracker_value == iteration
            and len(shards) == expected_shards
            and len(nonempty_shards) == expected_shards
            and marker_valid
        )
        return evidence

    def _safe_checkpoint_root(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser().resolve()
        allowed = [root.expanduser().resolve() for root in self.settings.goal_allowed_roots]
        if not any(_is_relative_to(path, root) for root in allowed):
            raise ConflictError("Checkpoint path is outside the configured Goal read roots")
        return path

    def _finish_observation(
        self,
        session: Session,
        goal: models.GoalRun,
        now: datetime,
        evidence: dict[str, Any],
    ) -> None:
        state = dict(goal.state_json or {})
        state["phase"] = str(evidence.get("phase") or "observing")
        state["last_observation"] = evidence
        self._release(goal, now, next_run_at=now + timedelta(seconds=goal.cadence_seconds))
        goal.state_json = state
        self._event(
            session,
            goal,
            "goal.observed",
            "Persistent worker recorded a mechanical Goal observation.",
            evidence,
        )

    def _complete_goal(
        self,
        session: Session,
        goal: models.GoalRun,
        now: datetime,
        evidence: dict[str, Any],
    ) -> None:
        state = dict(goal.state_json or {})
        state.update({"phase": "completed", "completion_evidence": evidence})
        goal.status = "completed"
        goal.completed_at = now
        goal.error = None
        self._release(goal, now, next_run_at=None)
        goal.state_json = state
        self._event(
            session,
            goal,
            "goal.completed",
            "Goal completion contract verified by the persistent worker.",
            evidence,
        )

    def _needs_decision(
        self,
        session: Session,
        goal: models.GoalRun,
        now: datetime,
        message: str,
        evidence: dict[str, Any],
    ) -> None:
        state = dict(goal.state_json or {})
        state.update({"phase": "needs_decision", "last_observation": evidence})
        goal.status = "needs_decision"
        goal.error = message
        self._release(goal, now, next_run_at=None)
        goal.state_json = state
        self._event(session, goal, "goal.needs_decision", message, evidence)

    @staticmethod
    def _release(
        goal: models.GoalRun, now: datetime, *, next_run_at: datetime | None
    ) -> None:
        goal.lease_owner = None
        goal.lease_expires_at = None
        goal.last_completed_at = now
        goal.next_run_at = next_run_at

    @staticmethod
    def _event(
        session: Session,
        goal: models.GoalRun,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            models.GoalEvent(
                goal_run_id=goal.id,
                event_type=event_type,
                message=message,
                payload_json=payload,
            )
        )

    @staticmethod
    def _claim(goal: models.GoalRun) -> GoalClaim:
        return GoalClaim(
            id=goal.id,
            branch_id=goal.branch_id,
            work_item_id=goal.work_item_id,
            title=goal.title,
            objective=goal.objective,
            executor=goal.executor,
            policy=dict(goal.policy_json or {}),
            completion=dict(goal.completion_json or {}),
            state=dict(goal.state_json or {}),
            cadence_seconds=goal.cadence_seconds,
            max_agent_turns=goal.max_agent_turns,
            agent_turns_started=goal.agent_turns_started,
        )

    @staticmethod
    def _claimed_goal(
        session: Session, goal_id: str, owner: str
    ) -> models.GoalRun | None:
        goal = session.get(models.GoalRun, goal_id)
        if goal is None or goal.lease_owner != owner:
            return None
        if goal.lease_expires_at is None or _as_utc(goal.lease_expires_at) <= datetime.now(UTC):
            return None
        return goal


class GoalSupervisor:
    """Async owner of due Goal claims and scheduled agent checkpoints."""

    def __init__(
        self,
        goals: GoalService,
        branches: ResearchOpsService,
        event_hub: EventHub,
        settings: Settings,
    ) -> None:
        self.goals = goals
        self.branches = branches
        self.event_hub = event_hub
        self.settings = settings
        self.owner = f"{socket.gethostname()}:goal-worker:{uuid.uuid4()}"
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._busy_branches: set[str] = set()

    async def dispatch_due(self) -> None:
        claims = await asyncio.to_thread(
            self.goals.claim_due,
            self.owner,
            limit=self.settings.goal_max_dispatch_per_tick,
        )
        for claim in claims:
            evaluation = await asyncio.to_thread(
                self.goals.evaluate_claim, claim, self.owner
            )
            await asyncio.to_thread(
                self.branches.reconcile_external_activity, {claim.work_item_id}
            )
            await self._publish_goal_update(claim.branch_id, claim.id)
            if evaluation.action != "agent":
                continue
            if claim.branch_id in self._busy_branches:
                await asyncio.to_thread(
                    self.goals.defer_claim,
                    claim.id,
                    self.owner,
                    "Another scheduled Goal is already using this Branch.",
                )
                await self._publish_goal_update(claim.branch_id, claim.id)
                continue
            state = await asyncio.to_thread(
                self.branches.branch_conversation_state, claim.branch_id
            )
            if not state.get("can_send"):
                await asyncio.to_thread(
                    self.goals.defer_claim,
                    claim.id,
                    self.owner,
                    str(state.get("reason") or "Branch is busy"),
                )
                await self._publish_goal_update(claim.branch_id, claim.id)
                continue
            started = await asyncio.to_thread(
                self.goals.begin_agent_turn, claim.id, self.owner
            )
            if started is None:
                await self._publish_goal_update(claim.branch_id, claim.id)
                continue
            self._busy_branches.add(claim.branch_id)
            task = asyncio.create_task(
                self._run_agent_tick(started),
                name=f"research-ops-goal:{started.id}",
            )
            self._tasks[started.id] = task
            task.add_done_callback(
                lambda completed, goal_id=started.id, branch_id=started.branch_id: self._clear_task(
                    goal_id, branch_id, completed
                )
            )
            await self._publish_goal_update(started.branch_id, started.id)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._busy_branches.clear()
        await asyncio.to_thread(
            self.goals.release_owner,
            self.owner,
            reason="Research Ops Goal worker stopped; lease released for recovery.",
        )

    async def _run_agent_tick(self, claim: GoalClaim) -> None:
        try:
            message = await self._send_with_lease_heartbeat(claim)
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.goals.defer_claim,
                claim.id,
                self.owner,
                "Goal worker stopped before its scheduled agent checkpoint finished.",
                seconds=1,
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.goals.fail_agent_turn,
                claim.id,
                self.owner,
                _safe_error(exc),
            )
        else:
            await asyncio.to_thread(
                self.goals.finish_agent_turn,
                claim.id,
                self.owner,
                message_id=message.id,
                turn_id=message.turn_id,
            )
        finally:
            await asyncio.to_thread(
                self.branches.reconcile_external_activity, {claim.work_item_id}
            )
            await self._publish_goal_update(claim.branch_id, claim.id)

    async def _send_with_lease_heartbeat(self, claim: GoalClaim) -> Any:
        send_task = asyncio.create_task(
            self.branches.send_message(
                claim.branch_id,
                schemas.ConversationSend(content=_goal_prompt(claim)),
                origin="goal",
                metadata={
                    "goal_id": claim.id,
                    "goal_title": claim.title,
                    "goal_executor": "persistent_worker",
                    "execution": claim.state.get("execution"),
                },
                supersede_pending=False,
            ),
            name=f"research-ops-goal-send:{claim.id}",
        )
        heartbeat_task = asyncio.create_task(
            self._maintain_claim(claim),
            name=f"research-ops-goal-heartbeat:{claim.id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {send_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                await heartbeat_task
                raise RuntimeError("Goal lease heartbeat stopped unexpectedly")
            return await send_task
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
            if not send_task.done():
                send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await send_task

    async def _maintain_claim(self, claim: GoalClaim) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds())
            renewed = await asyncio.to_thread(
                self.goals.renew_claim,
                claim.id,
                self.owner,
            )
            if not renewed:
                raise RuntimeError(
                    "Goal worker lost its lease while the scheduled agent checkpoint was running"
                )
            await self._publish_goal_update(claim.branch_id, claim.id)

    def _heartbeat_interval_seconds(self) -> float:
        return min(
            self.settings.goal_lease_heartbeat_seconds,
            max(5.0, self.settings.goal_lease_seconds / 3),
        )

    def _clear_task(
        self, goal_id: str, branch_id: str, task: asyncio.Task[None]
    ) -> None:
        if self._tasks.get(goal_id) is task:
            self._tasks.pop(goal_id, None)
        self._busy_branches.discard(branch_id)

    async def _publish_goal_update(self, branch_id: str, goal_id: str) -> None:
        await self.event_hub.publish(
            branch_id,
            {"type": "goal.updated", "goal_id": goal_id},
        )


def _goal_prompt(claim: GoalClaim) -> str:
    return claim.objective


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:2_000]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
