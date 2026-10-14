from __future__ import annotations

import asyncio
import logging
import socket
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.codex.base import CodexAdapter, CodexUnavailable
from research_ops.codex.identity import CodexIdentityService
from research_ops.codex.identity_pool import adapter_for_execution
from research_ops.db import run_db
from research_ops.services.core import ConflictError, NotFoundError
from research_ops.services.native_goal_watch import NativeGoalWatch, control_request_timeout
from research_ops.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GoalTarget:
    branch_id: str
    thread_id: str | None
    supported: bool
    reason: str | None = None
    execution: dict[str, Any] = field(default_factory=dict)
    cwd: str = ""


class NativeGoalService:
    """Expose Codex's single persisted thread Goal without duplicating its state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        codex: CodexAdapter,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.codex = codex
        self.watch = NativeGoalWatch(session_factory, settings)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._observed_threads: set[str] = set()
        self._reconciled_failures: dict[str, tuple[str, str]] = {}

    async def get(self, branch_id: str) -> schemas.NativeGoalState:
        target = await run_db(self._target, branch_id)
        if not target.supported:
            return schemas.NativeGoalState(
                supported=False,
                thread_id=target.thread_id,
                reason=target.reason,
            )
        async with self._locks[branch_id]:
            return await self._inspect(target)

    async def set(
        self,
        branch_id: str,
        payload: schemas.NativeGoalSet,
    ) -> schemas.NativeGoalState:
        async with self._locks[branch_id]:
            return await self._set(branch_id, payload)

    async def _set(self, branch_id: str, payload: schemas.NativeGoalSet) -> schemas.NativeGoalState:
        target = await run_db(self._writable_target, branch_id)
        assert target.thread_id is not None
        adapter = adapter_for_execution(self.codex, target.execution)
        current = await adapter.get_goal(thread_id=target.thread_id)
        current_goal = current.get("goal")
        await run_db(self.watch.observe_goal, branch_id, target.thread_id, current_goal)
        if isinstance(current_goal, dict) and current_goal.get("status") == "complete":
            await adapter.clear_goal(thread_id=target.thread_id)
            await run_db(self.watch.observe_goal, branch_id, target.thread_id, None)
            await run_db(self._remember_execution, branch_id, {}, clear=True)
            target = await run_db(self._writable_target, branch_id)
            adapter = adapter_for_execution(self.codex, target.execution)
        try:
            response = await adapter.set_goal(
                thread_id=target.thread_id,
                objective=payload.objective,
                status="active",
                token_budget=payload.token_budget,
                update_token_budget=True,
            )
        except CodexUnavailable as exc:
            self._raise_goal_conflict(exc)
            raise
        state = self._state(target.thread_id, response.get("goal"))
        self._observed_threads.add(target.thread_id)
        await run_db(self._remember_execution, branch_id, target.execution)
        await run_db(
            self.watch.observe_goal,
            branch_id,
            target.thread_id,
            response.get("goal"),
            notify_initial=True,
        )
        await run_db(self.watch.begin_recovery, branch_id, automatic=False)
        await self._ensure_running(target, automatic=False)
        await run_db(
            self._audit,
            branch_id,
            "codex.goal_set",
            state,
        )
        return await self._with_runtime(branch_id, state)

    async def control(
        self,
        branch_id: str,
        payload: schemas.NativeGoalControl,
    ) -> schemas.NativeGoalState:
        async with self._locks[branch_id]:
            return await self._control(branch_id, payload)

    async def _control(
        self, branch_id: str, payload: schemas.NativeGoalControl
    ) -> schemas.NativeGoalState:
        target = await run_db(self._writable_target, branch_id)
        assert target.thread_id is not None
        adapter = adapter_for_execution(self.codex, target.execution)
        if payload.action == "clear":
            response = await adapter.clear_goal(thread_id=target.thread_id)
            await run_db(self._remember_execution, branch_id, {}, clear=True)
            await run_db(self.watch.observe_goal, branch_id, target.thread_id, None)
            self._observed_threads.discard(target.thread_id)
            state = schemas.NativeGoalState(
                supported=True,
                thread_id=target.thread_id,
            )
            await run_db(
                self._audit,
                branch_id,
                "codex.goal_cleared",
                state,
                {"cleared": bool(response.get("cleared"))},
            )
            return state

        status_by_action = {
            "pause": "paused",
            "resume": "active",
            "complete": "complete",
        }
        try:
            response = await adapter.set_goal(
                thread_id=target.thread_id,
                status=status_by_action[payload.action],
            )
        except CodexUnavailable as exc:
            self._raise_goal_conflict(exc)
            raise
        state = self._state(target.thread_id, response.get("goal"))
        self._observed_threads.add(target.thread_id)
        await run_db(self._remember_execution, branch_id, target.execution)
        await run_db(
            self.watch.observe_goal,
            branch_id,
            target.thread_id,
            response.get("goal"),
            notify_initial=True,
        )
        if payload.action == "resume":
            await run_db(self.watch.begin_recovery, branch_id, automatic=False)
            await self._ensure_running(target, automatic=False)
        await run_db(self._audit, branch_id, f"codex.goal_{payload.action}d", state)
        if payload.action == "pause":
            # Pausing the objective prevents future continuations, but does not
            # cancel an already running turn. Use its existing owner, not a new CLI.
            try:
                turn = await adapter.read_goal_turn(thread_id=target.thread_id)
                if turn.get("status") == "inProgress" and turn.get("id"):
                    await adapter.interrupt_turn(
                        thread_id=target.thread_id, turn_id=turn["id"]
                    )
                    await run_db(
                        self._audit, branch_id, "codex.goal_turn_interrupt_requested", state,
                        {"turn_id": turn["id"]},
                    )
                await run_db(self.watch.pause_control_result, branch_id)
            except CodexUnavailable as exc:
                reason = f"Goal paused, but the current turn could not be interrupted: {exc}"
                await run_db(self.watch.pause_control_result, branch_id, reason)
                raise ConflictError(reason) from exc
        return await self._with_runtime(branch_id, state)

    async def recover_active(self) -> dict[str, int]:
        """Reattach persisted active Goals after the Orbitree process restarts."""
        return await self.monitor_once(discover=True)

    def _candidates(self, discover: bool) -> list[str]:
        with self.session_factory() as session:
            query = (
                select(models.ExecutionBranch.id)
                .join(models.WorkItem)
                .where(
                    models.ExecutionBranch.runtime == "codex",
                    models.ExecutionBranch.mode != "capture",
                    models.ExecutionBranch.codex_thread_id.is_not(None),
                    models.ExecutionBranch.hidden.is_(False),
                    models.WorkItem.hidden.is_(False),
                )
            )
            if not discover:
                query = query.join(
                    models.NativeGoalMonitor,
                    models.NativeGoalMonitor.branch_id == models.ExecutionBranch.id,
                ).where(
                    models.NativeGoalMonitor.thread_id == models.ExecutionBranch.codex_thread_id
                )
            return list(session.scalars(query))

    async def monitor_once(self, *, discover: bool = False) -> dict[str, int]:
        candidates = await run_db(self._candidates, discover)
        # Bound metadata probes only, never the number of executing Goals.
        semaphore = asyncio.Semaphore(4)

        async def recover_one(branch_id: str) -> bool:
            async with semaphore:
                try:
                    async with self._locks[branch_id]:
                        target = await run_db(self._target, branch_id)
                        if not target.supported:
                            return False
                        state = await self._inspect(target)
                        if state.goal is None or state.reason:
                            return False
                        runtime = state.runtime
                        detached = bool(
                            state.goal.status == "active"
                            and runtime
                            and runtime.status in {"detached", "external_owner", "recovering"}
                        )
                        candidate = await run_db(
                            self.watch.recovery_candidate, branch_id, detached=detached
                        )
                        if candidate and self._reconciled_failures.get(branch_id) != candidate:
                            adapter = adapter_for_execution(self.codex, target.execution)
                            try:
                                turn = await adapter.read_goal_turn(thread_id=target.thread_id)
                                await run_db(
                                    self.watch.reconcile_tools,
                                    branch_id,
                                    target.thread_id,
                                    turn,
                                    detached=detached,
                                )
                                self._reconciled_failures[branch_id] = candidate
                            except Exception as exc:
                                logger.info(
                                    "Native Goal tool verification unavailable for %s: %s",
                                    branch_id,
                                    type(exc).__name__,
                                )
                        due = await run_db(self.watch.retry_due, branch_id)
                        if not detached and not (
                            due and state.goal.status in {"active", "blocked"}
                        ):
                            return False
                        if not await run_db(self.watch.begin_recovery, branch_id, automatic=True):
                            return False
                        adapter = adapter_for_execution(self.codex, target.execution)
                        if due and state.goal.status == "blocked":
                            # Continue the persisted conversation only after all
                            # observed tool calls have definitive outcomes.
                            current = await adapter.get_goal(thread_id=target.thread_id)
                            if (current.get("goal") or {}).get("status") != "blocked":
                                return False
                            response = await adapter.set_goal(
                                thread_id=target.thread_id, status="active"
                            )
                            await run_db(
                                self.watch.observe_goal,
                                branch_id,
                                target.thread_id,
                                response.get("goal"),
                            )
                        return await self._ensure_running(target, automatic=True)
                except Exception as exc:
                    await run_db(self.watch.failure, branch_id, str(exc))
                    logger.info(
                        "Native Goal check failed for %s: %s",
                        branch_id,
                        exc,
                    )
                    return False

        recovered = await asyncio.gather(*(recover_one(branch_id) for branch_id in candidates))
        return {"checked": len(candidates), "recovered": sum(recovered)}

    async def _inspect(self, target: _GoalTarget) -> schemas.NativeGoalState:
        assert target.thread_id is not None
        adapter = adapter_for_execution(self.codex, target.execution)
        try:
            response = await adapter.get_goal(thread_id=target.thread_id)
        except CodexUnavailable as exc:
            return await self._observation_unavailable(target, exc)
        state = self._state(target.thread_id, response.get("goal"))
        await run_db(
            self.watch.observe_goal, target.branch_id, target.thread_id, response.get("goal")
        )
        if state.goal is not None:
            self._observed_threads.add(target.thread_id)
            await run_db(self._remember_execution, target.branch_id, target.execution)
            try:
                thread = await adapter.read_goal_thread(thread_id=target.thread_id)
            except CodexUnavailable as exc:
                return await self._observation_unavailable(target, exc)
            await run_db(self.watch.checked, target.branch_id, self._execution_status(thread))
        return await self._with_runtime(target.branch_id, state)

    async def _observation_unavailable(
        self, target: _GoalTarget, error: Exception, *, recovery_started: bool = False
    ) -> schemas.NativeGoalState:
        assert target.thread_id is not None
        logger.warning("Native Goal status query unavailable for %s: %s", target.branch_id, error)
        snapshot = await run_db(
            self.watch.observation_failed,
            target.branch_id,
            str(error),
            recovery_started=recovery_started,
        )
        state = self._state(target.thread_id, snapshot)
        state.reason = (
            "Codex status is temporarily unavailable. The task may still be running; "
            "Orbitree will check again before attempting recovery."
        )
        return await self._with_runtime(target.branch_id, state)

    async def _with_runtime(
        self, branch_id: str, state: schemas.NativeGoalState
    ) -> schemas.NativeGoalState:
        if state.goal:
            state.runtime = await run_db(self.watch.runtime, branch_id)
        return state

    @staticmethod
    def _execution_status(response: dict[str, Any]) -> str:
        raw = (response.get("thread", {}).get("status") or {}).get("type")
        return {
            "active": "running",
            "idle": "idle",
            "notLoaded": "detached",
            "systemError": "error",
        }.get(raw, "unknown")

    async def _ensure_running(self, target: _GoalTarget, *, automatic: bool) -> bool:
        assert target.thread_id is not None
        adapter = adapter_for_execution(self.codex, target.execution)
        try:
            # Recheck persisted state so a pause observed during recovery wins.
            goal = (await adapter.get_goal(thread_id=target.thread_id)).get("goal")
            if not goal or goal.get("status") != "active":
                await run_db(self.watch.observe_goal, target.branch_id, target.thread_id, goal)
                return False
            await adapter.prepare_goal_thread(thread_id=target.thread_id, cwd=target.cwd)
            thread = await adapter.read_goal_thread(thread_id=target.thread_id)
            status = self._execution_status(thread)
            if status not in {"running", "idle"}:
                raise CodexUnavailable("Native Goal runtime is not attached after resume")
            await run_db(self.watch.checked, target.branch_id, status)
            if automatic:
                await run_db(self.watch.recovered, target.branch_id)
                await run_db(
                    self._audit,
                    target.branch_id,
                    "codex.goal_recovered",
                    self._state(target.thread_id, goal),
                    {"execution_status": status},
                )
            return True
        except CodexUnavailable as exc:
            if control_request_timeout(str(exc)):
                await self._observation_unavailable(target, exc, recovery_started=automatic)
            elif "active writer" in str(exc).lower():
                await run_db(self.watch.external_owner, target.branch_id)
            else:
                await run_db(self.watch.failure, target.branch_id, str(exc))
            return False

    async def observe_event(self, branch_id: str, method: str, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            return
        if (
            method not in {"thread/goal/updated", "thread/goal/cleared"}
            and thread_id not in self._observed_threads
        ):
            return
        async with self._locks[branch_id]:
            if method in {"thread/goal/updated", "thread/goal/cleared"}:
                # Notifications can arrive after a replacement or pause. Read
                # Codex's current state instead of restoring an old event payload.
                target = await run_db(self._target, branch_id)
                if not target.supported or target.thread_id != thread_id:
                    return
                adapter = adapter_for_execution(self.codex, target.execution)
                response = await adapter.get_goal(thread_id=thread_id)
                self._state(thread_id, response.get("goal"))
                if response.get("goal"):
                    self._observed_threads.add(thread_id)
                else:
                    self._observed_threads.discard(thread_id)
                await run_db(
                    self.watch.observe_goal,
                    branch_id,
                    thread_id,
                    response.get("goal"),
                    notify_initial=True,
                    turn_id=params.get("turnId")
                    if params.get("goal") == response.get("goal")
                    else None,
                )
            else:
                await run_db(self.watch.observe_event, branch_id, method, params)

    def _target(self, branch_id: str) -> _GoalTarget:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            thread_id = str(branch.codex_thread_id or "").strip() or None
            global_pi = self.settings.pi_enabled or self.settings.codex_backend == "pi"
            if global_pi or branch.runtime in {"pi", "claude"}:
                return _GoalTarget(
                    branch_id,
                    thread_id,
                    False,
                    "Native Goal is only available for Codex Branches.",
                )
            if branch.mode == "capture":
                return _GoalTarget(
                    branch_id,
                    thread_id,
                    False,
                    "Capture-only Branches do not own an executable Codex thread.",
                )
            if not thread_id or thread_id.startswith("deferred:"):
                return _GoalTarget(
                    branch_id,
                    thread_id,
                    False,
                    "Run one Codex turn first so this Branch has a persisted thread.",
                )
            execution = branch.native_goal_execution or (
                CodexIdentityService(self.settings).snapshot(branch.execution_preferences)
            )
            return _GoalTarget(branch_id, thread_id, True, execution=execution, cwd=branch.cwd)

    def _remember_execution(
        self, branch_id: str, execution: dict[str, Any], *, clear: bool = False
    ) -> None:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch and (clear or not branch.native_goal_execution):
                branch.native_goal_execution = dict(execution)
                session.commit()

    def _writable_target(self, branch_id: str) -> _GoalTarget:
        target = self._target(branch_id)
        if not target.supported:
            raise ConflictError(target.reason or "Native Goal is unavailable")
        return target

    @staticmethod
    def _state(thread_id: str, raw_goal: Any) -> schemas.NativeGoalState:
        if raw_goal is None:
            return schemas.NativeGoalState(supported=True, thread_id=thread_id)
        if not isinstance(raw_goal, dict):
            raise CodexUnavailable("Codex returned an invalid native Goal payload")
        try:
            goal = schemas.NativeGoalRead(
                thread_id=str(raw_goal["threadId"]),
                objective=str(raw_goal["objective"]),
                status=raw_goal["status"],
                token_budget=raw_goal.get("tokenBudget"),
                tokens_used=int(raw_goal["tokensUsed"]),
                time_used_seconds=int(raw_goal["timeUsedSeconds"]),
                created_at=int(raw_goal["createdAt"]),
                updated_at=int(raw_goal["updatedAt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodexUnavailable("Codex returned an invalid native Goal payload") from exc
        if goal.thread_id != thread_id:
            raise CodexUnavailable("Codex returned a native Goal for a different thread")
        return schemas.NativeGoalState(supported=True, thread_id=thread_id, goal=goal)

    def _audit(
        self,
        branch_id: str,
        event_type: str,
        state: schemas.NativeGoalState,
        extra: dict[str, Any] | None = None,
    ) -> None:
        goal = state.goal
        payload: dict[str, Any] = {
            "thread_id": state.thread_id,
            "status": goal.status if goal else None,
            "token_budget": goal.token_budget if goal else None,
            "tokens_used": goal.tokens_used if goal else None,
            **(extra or {}),
        }
        with self.session_factory() as session:
            session.add(
                models.ActivityEvent(
                    scope_type="branch",
                    scope_id=branch_id,
                    event_type=event_type,
                    actor=socket.gethostname(),
                    payload=payload,
                )
            )
            session.commit()

    @staticmethod
    def _raise_goal_conflict(exc: CodexUnavailable) -> None:
        message = str(exc)
        conflict_signals = (
            "thread must be idle",
            "active writer",
            "expected active turn",
            "already has an active",
        )
        if any(signal in message.lower() for signal in conflict_signals):
            raise ConflictError(message) from exc
