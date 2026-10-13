from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import socket
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from research_ops import models, schemas
from research_ops.codex.base import CodexAdapter, CodexUnavailable
from research_ops.codex.claude_execution import (
    CLAUDE_EFFORTS,
    CLAUDE_MODELS,
    ClaudeExecutionPreferences,
    claude_execution_snapshot,
)
from research_ops.codex.claude_handoff import (
    bound_claude_handoff,
    render_claude_handoff,
    stage_claude_handoff,
)
from research_ops.codex.identity import CodexIdentityService, ExecutionPreferences
from research_ops.codex.identity_pool import adapter_for_execution
from research_ops.codex.rollout import find_rollout
from research_ops.constants import PROGRAM_DESK_KIND
from research_ops.db import is_sqlite_busy, run_db
from research_ops.events import EventHub
from research_ops.services.codex_events import redact
from research_ops.settings import PROJECT_ROOT, Settings

from .briefs import build_brief, developer_instructions
from .external_jobs import external_job_is_active
from .workspace import WorkspaceError, WorkspaceManager, branch_slug
from .workspace_snapshot import apply_workspace_snapshot, plan_workspace_snapshot

NEEDS_DEFINITION_LABEL = "needs_definition"
AUTO_CONTINUATION_KEY = "auto_continuation"
AUTO_CONTINUATION_RECOVERY_MINUTES = 10
BRANCH_DETAIL_MESSAGE_LIMIT = 200
# Terminal Web queries are deliberately retained outside the activity window,
# but the Branch detail endpoint is still a hot-path payload. The latest
# incomplete queries are enough to surface recovery actions; older durable
# records remain in the catalog and can be paged separately when needed.
BRANCH_DETAIL_TERMINAL_QUERY_LIMIT = 24
logger = logging.getLogger(__name__)


class NotFoundError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class _PreparedWebTurn:
    branch_id: str
    attempt_id: str
    attempt_lease_id: str
    message_id: str
    message_metadata: dict[str, Any]
    thread_id: str
    cwd: str
    work_human_id: str
    brief: dict[str, Any]
    content: str


class ResearchOpsService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        workspace: WorkspaceManager,
        codex: CodexAdapter,
        pi: CodexAdapter | None = None,
        claude: CodexAdapter | None = None,
        event_hub: EventHub,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.identities = CodexIdentityService(settings)
        self.workspace = workspace
        self.codex = codex
        self.pi = pi
        self.claude = claude
        self.event_hub = event_hub
        self._web_turn_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._web_turn_tasks: dict[str, asyncio.Task[None]] = {}
        limit = self.settings.codex_max_concurrent_turns
        self._codex_turn_semaphore = asyncio.Semaphore(limit) if limit > 0 else None
        self._continuation_tasks: dict[str, asyncio.Task[None]] = {}

    def _default_runtime(self) -> str:
        return "pi" if self.settings.pi_enabled or self.settings.codex_backend == "pi" else "codex"

    def _runtime_name(self, branch: models.ExecutionBranch | None = None) -> str:
        """Return the Branch runtime, preserving the legacy global Pi canary override."""
        if self._default_runtime() == "pi":
            return "pi"
        if branch is not None and branch.runtime in {"pi", "claude"}:
            return branch.runtime
        return "codex"

    def _adapter_for_runtime(self, runtime: str) -> CodexAdapter:
        if runtime == "pi":
            if self.pi is None:
                raise ConflictError("Pi is not configured for this Research Ops service")
            return self.pi
        if runtime == "claude":
            if self.claude is None:
                raise ConflictError("Claude is not configured for this Research Ops service")
            return self.claude
        return self.codex

    def _adapter_for_branch(self, branch: models.ExecutionBranch) -> CodexAdapter:
        adapter = self._adapter_for_runtime(self._runtime_name(branch))
        if self._runtime_name(branch) == "codex":
            return adapter_for_execution(
                adapter, self.identities.snapshot(branch.execution_preferences)
            )
        return adapter

    def branch_execution(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            return {
                "runtime": self._runtime_name(branch),
                "preferences": branch.execution_preferences,
                "effective": self._execution_snapshot(branch),
                "native_goal_execution": branch.native_goal_execution,
                **self.identities.status(),
            }

    def _execution_snapshot(self, branch: models.ExecutionBranch) -> dict[str, Any]:
        runtime = self._runtime_name(branch)
        if runtime == "claude":
            return claude_execution_snapshot(self.settings, branch.execution_preferences)
        if runtime == "codex":
            return self.identities.snapshot(branch.execution_preferences)
        return {}

    def _web_execution_snapshot(
        self, session: Session, branch: models.ExecutionBranch
    ) -> dict[str, Any]:
        selected = self._execution_snapshot(branch)
        if self._runtime_name(branch) != "codex" or not branch.native_goal_execution:
            return selected
        monitor = session.get(models.NativeGoalMonitor, branch.id)
        if (
            monitor is None or monitor.thread_id != branch.codex_thread_id
            or (monitor.goal_snapshot or {}).get("status") != "active"
        ):
            return selected
        owner = dict(branch.native_goal_execution)
        # Steering is part of the current Goal turn. A next-turn model choice
        # must not launch a competing writer or mislabel the accepted request.
        if any(owner.get(key) != selected.get(key) for key in ("identity", "provider", "base_url")):
            raise ConflictError(
                "Pause the active Goal before switching this conversation's account or provider"
            )
        return owner

    def claude_branch_execution(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) != "claude":
                raise ConflictError("Claude settings apply only to Claude Branches")
            return {
                "runtime": "claude",
                "preferences": branch.execution_preferences,
                "effective": self._execution_snapshot(branch),
                "models": CLAUDE_MODELS,
                "reasoning_efforts": CLAUDE_EFFORTS,
            }

    def update_claude_branch_execution(
        self, branch_id: str, payload: ClaudeExecutionPreferences
    ) -> dict[str, Any]:
        values = payload.model_dump(exclude_none=True)
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) != "claude":
                raise ConflictError("Claude settings apply only to Claude Branches")
            # Model-only clients must not silently reset an explicit permission choice.
            if "permission_mode" not in payload.model_fields_set:
                permission_mode = (branch.execution_preferences or {}).get("permission_mode")
                if permission_mode is not None:
                    values["permission_mode"] = permission_mode
            effective = claude_execution_snapshot(self.settings, values)
            branch.execution_preferences = values
            self._event(session, "branch", branch.id, "execution.model_selected", effective)
            session.commit()
        return self.claude_branch_execution(branch_id)

    def update_branch_execution(
        self, branch_id: str, payload: ExecutionPreferences
    ) -> dict[str, Any]:
        self.identities.ensure_ready(payload.identity)
        values = payload.model_dump(exclude_none=True)
        effective = self.identities.snapshot(values)
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) != "codex":
                raise ConflictError("Codex identity selection does not change Pi authentication")
            branch.execution_preferences = values
            self._event(session, "branch", branch.id, "execution.identity_selected", effective)
            session.commit()
        return self.branch_execution(branch_id)

    def list_programs(self, include_archived: bool = False) -> list[models.Program]:
        with self.session_factory() as session:
            query = select(models.Program).order_by(models.Program.sort_order, models.Program.code)
            if not include_archived:
                query = query.where(models.Program.active.is_(True))
            return list(session.scalars(query))

    def create_program(self, payload: schemas.ProgramCreate) -> models.Program:
        from .bootstrap import _ensure_program_desk

        with self.session_factory() as session:
            if session.scalar(select(models.Program).where(models.Program.code == payload.code)):
                raise ConflictError("This direction code already exists, including archived ones")
            program = models.Program(**payload.model_dump(), slug=payload.code.lower())
            session.add(program)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError("This direction code already exists") from exc
            desk = self.workspace.prepare_program_desk(
                program_code=program.code, program_name=program.name, objective=program.objective,
            )
            _ensure_program_desk(session, program, desk.cwd)
            session.commit()
            return program

    def update_program(self, code: str, payload: schemas.ProgramUpdate) -> models.Program:
        with self.session_factory() as session:
            program = self._program_by_code(session, code)
            if not payload.active and program.active:
                active_count = session.scalar(
                    select(func.count()).select_from(models.Program).where(
                        models.Program.active.is_(True),
                    )
                )
                if active_count <= 1:
                    raise ConflictError("Keep at least one active direction")
            for field, value in payload.model_dump().items():
                setattr(program, field, value)
            self._event(session, "program", program.id, "program.updated", payload.model_dump())
            session.commit()
            return program

    @staticmethod
    def _is_conversation_origin(message: models.ConversationMessage) -> bool:
        return message.metadata_json.get("origin") in {"web", "continuation", "goal"}

    def list_repositories(self) -> list[models.Repository]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(models.Repository)
                    .where(models.Repository.active.is_(True))
                    .order_by(models.Repository.name)
                )
            )

    def create_repository(self, payload: schemas.RepositoryCreate) -> models.Repository:
        with self.session_factory() as session:
            duplicate = session.scalar(
                select(models.Repository).where(models.Repository.name == payload.name)
            )
            if duplicate:
                raise ConflictError(f"Repository already exists: {payload.name}")
            for code in payload.program_codes:
                self._program_by_code(session, code)
            try:
                canonical_path = self.workspace.validate_repository(
                    payload.canonical_path, payload.default_ref
                )
            except WorkspaceError as exc:
                raise ConflictError(str(exc)) from exc
            repository = models.Repository(
                **payload.model_dump(exclude={"canonical_path", "program_codes"}),
                canonical_path=canonical_path,
                program_codes=[code.upper() for code in payload.program_codes],
            )
            session.add(repository)
            session.commit()
            return repository

    def create_work_item(
        self,
        payload: schemas.WorkItemCreate,
        *,
        existing_cwd: str | None = None,
    ) -> tuple[models.WorkItem, models.ExecutionBranch]:
        with self.session_factory() as session:
            program = self._program_by_code(session, payload.program_code)
            if not program.active:
                raise ConflictError("Restore this direction before creating work")
            repository = (
                session.get(models.Repository, payload.repository_id)
                if payload.repository_id
                else None
            )
            if payload.repository_id and repository is None:
                raise NotFoundError("Repository not found")
            if (
                repository
                and repository.program_codes
                and program.code not in repository.program_codes
            ):
                raise ConflictError(
                    f"Repository {repository.name} is not enabled for Program {program.code}"
                )
            parent = (
                session.get(models.WorkItem, payload.primary_parent_id)
                if payload.primary_parent_id
                else None
            )
            if payload.primary_parent_id and parent is None:
                raise NotFoundError("Parent WorkItem not found")
            if parent and parent.program_id != program.id:
                raise ConflictError("Parent WorkItem must belong to the same Program")
            # Imported windows are always Codex sessions. Normal New Work
            # requests may override the deployment default per Main Branch.
            runtime = "codex" if existing_cwd else (payload.runtime or self._default_runtime())
            self._adapter_for_runtime(runtime)
            human_id = f"{program.code}-{program.next_sequence:04d}"
            program.next_sequence += 1
            title = payload.title.strip() if payload.title else ""
            objective = payload.objective.strip() if payload.objective else ""
            definition_pending = not title or not objective
            labels = [label for label in payload.labels if label != NEEDS_DEFINITION_LABEL]
            if definition_pending:
                labels.append(NEEDS_DEFINITION_LABEL)
            work_item = models.WorkItem(
                human_id=human_id,
                program_id=program.id,
                primary_parent_id=payload.primary_parent_id,
                repository_id=payload.repository_id,
                title=title or f"未命名工作 {human_id}",
                objective=objective,
                kind=payload.kind,
                lifecycle="ready" if payload.branch_mode != "capture" else "draft",
                flow="actionable",
                priority=payload.priority,
                acceptance_criteria=[item.model_dump() for item in payload.acceptance_criteria],
                labels=labels,
            )
            session.add(work_item)
            session.flush()
            if existing_cwd:
                if repository is not None or payload.branch_mode != "write":
                    raise ConflictError(
                        "Imported Codex sessions require a writable, unregistered workspace"
                    )
                try:
                    workspace = self.workspace.adopt_existing(existing_cwd)
                except WorkspaceError as exc:
                    raise ConflictError(str(exc)) from exc
                workspace_path = self._workspace_entry(
                    program_code=program.code,
                    work_human_id=human_id,
                    branch_slug="main",
                    repository_name=None,
                    cwd=workspace.cwd,
                )
            else:
                workspace = self.workspace.prepare(
                    program_code=program.code,
                    work_human_id=human_id,
                    branch_slug="main",
                    repository=repository,
                    mode=payload.branch_mode,
                )
                workspace_path = workspace.cwd
            branch = models.ExecutionBranch(
                work_item_id=work_item.id,
                name="Main",
                slug="main",
                runtime=runtime,
                mode=payload.branch_mode,
                status="parked" if payload.branch_mode == "capture" else "active",
                cwd=workspace.cwd,
                workspace_path=workspace_path,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "fork_reason": (
                        "codex_session_imported" if existing_cwd else "work_item_created"
                    ),
                    "work_item_revision": work_item.revision,
                    "memory_snapshot_version": 1,
                    **({"workspace_source": "existing_codex_session"} if existing_cwd else {}),
                },
            )
            session.add(branch)
            session.flush()
            self._event(
                session,
                "work_item",
                work_item.id,
                "work_item.created",
                {"human_id": human_id, "branch_id": branch.id, "runtime": runtime},
            )
            session.commit()
            return work_item, branch

    def create_imported_fork(
        self,
        *,
        parent_branch_id: str,
        thread_id: str,
        parent_thread_id: str,
        cwd: str,
    ) -> models.ExecutionBranch:
        with self.session_factory() as session:
            parent = self._branch(session, parent_branch_id)
            slug = branch_slug(f"codex-{thread_id}")
            existing = session.scalar(
                select(models.ExecutionBranch).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id,
                    models.ExecutionBranch.slug == slug,
                )
            )
            if existing is not None:
                if existing.parent_branch_id != parent.id or existing.cwd != cwd:
                    raise ConflictError("Imported Codex fork Branch conflicts with existing data")
                return existing
            try:
                workspace = self.workspace.adopt_existing(cwd)
            except WorkspaceError as exc:
                raise ConflictError(str(exc)) from exc
            work_item = parent.work_item
            workspace_path = self._workspace_entry(
                program_code=work_item.program.code,
                work_human_id=work_item.human_id,
                branch_slug=slug,
                repository_name=work_item.repository.name if work_item.repository else None,
                cwd=workspace.cwd,
            )
            branch = models.ExecutionBranch(
                work_item_id=parent.work_item_id,
                parent_branch_id=parent.id,
                name=f"Codex fork · {thread_id[:10]}",
                slug=slug,
                runtime="codex",
                status="active",
                mode="write",
                cwd=workspace.cwd,
                workspace_path=workspace_path,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "fork_reason": "external_codex_fork",
                    "parent_thread_id": parent_thread_id,
                    "external_thread_id": thread_id,
                    "workspace_source": "existing_codex_session",
                    "workspace_shared_with_parent": workspace.cwd == parent.cwd,
                    "work_item_revision": parent.work_item.revision,
                    "memory_snapshot_version": parent.memory_cursor,
                },
            )
            session.add(branch)
            session.flush()
            self._event(
                session,
                "branch",
                branch.id,
                "branch.codex_fork_imported",
                {
                    "parent_branch_id": parent.id,
                    "thread_id": thread_id,
                    "parent_thread_id": parent_thread_id,
                },
            )
            session.commit()
            return branch

    def define_work_item(
        self, work_id: str, payload: schemas.WorkItemDefinition
    ) -> models.WorkItem:
        with self.session_factory() as session:
            work_item = self._work(session, work_id)
            if NEEDS_DEFINITION_LABEL not in work_item.labels:
                return work_item
            work_item.title = payload.title.strip()
            work_item.objective = payload.objective.strip()
            work_item.labels = [
                label for label in work_item.labels if label != NEEDS_DEFINITION_LABEL
            ]
            work_item.revision += 1
            self._event(
                session,
                "work_item",
                work_item.id,
                "work_item.defined",
                {"title": work_item.title, "objective": work_item.objective},
            )
            session.commit()
            return work_item

    def hide_work_item(self, work_id: str) -> models.WorkItem:
        with self.session_factory() as session:
            work_item = self._work(session, work_id)
            if work_item.kind == PROGRAM_DESK_KIND:
                raise ConflictError("Program Desk cannot be hidden")
            active = session.scalar(
                select(models.SessionAttempt.id)
                .join(
                    models.ExecutionBranch,
                    models.ExecutionBranch.id == models.SessionAttempt.branch_id,
                )
                .where(
                    models.ExecutionBranch.work_item_id == work_item.id,
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > datetime.now(UTC),
                )
                .limit(1)
            )
            if active:
                raise ConflictError("Exit active local Sessions before undoing this WorkItem")
            if work_item.hidden:
                return work_item
            work_item.hidden = True
            work_item.lifecycle = "cancelled"
            work_item.flow = "actionable"
            work_item.revision += 1
            self._event(
                session,
                "work_item",
                work_item.id,
                "work_item.hidden",
                {"human_id": work_item.human_id},
            )
            session.commit()
            return work_item

    def set_branch_hidden(
        self, branch_id: str, *, hidden: bool
    ) -> models.ExecutionBranch:
        """Soft-hide a child Branch without touching its thread, lineage, or workspace."""
        now = datetime.now(UTC)
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if branch.parent_branch_id is None:
                raise ConflictError("The primary Branch cannot be hidden")
            active = session.scalar(
                select(models.SessionAttempt.id)
                .where(
                    models.SessionAttempt.branch_id == branch.id,
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > now,
                )
                .limit(1)
            )
            if active:
                raise ConflictError("Finish the active Branch turn before hiding it")
            if branch.hidden == hidden:
                return branch

            branch.hidden = hidden
            snapshot = dict(branch.context_snapshot or {})
            raw_visibility = snapshot.get("visibility")
            visibility = dict(raw_visibility) if isinstance(raw_visibility, dict) else {}
            visibility.update(
                {
                    "hidden": hidden,
                    "changed_at": now.isoformat(),
                    "reason": "user_soft_hide" if hidden else "user_restore",
                }
            )
            snapshot["visibility"] = visibility
            branch.context_snapshot = snapshot
            self._event(
                session,
                "branch",
                branch.id,
                "branch.hidden" if hidden else "branch.restored",
                {"hidden": hidden},
            )
            session.commit()
            return branch

    def update_work_item(self, work_id: str, payload: schemas.WorkItemUpdate) -> models.WorkItem:
        with self.session_factory() as session:
            work_item = self._work(session, work_id)
            if work_item.revision != payload.expected_revision:
                raise ConflictError(
                    "Work item changed from revision "
                    f"{payload.expected_revision} to {work_item.revision}"
                )
            values = payload.model_dump(exclude_none=True, exclude={"expected_revision"})
            if "acceptance_criteria" in values:
                values["acceptance_criteria"] = [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in values["acceptance_criteria"]
                ]
            for key, value in values.items():
                setattr(work_item, key, value)
            work_item.revision += 1
            self._event(session, "work_item", work_item.id, "work_item.updated", values)
            session.commit()
            return work_item

    def program_tree(
        self, program_code: str
    ) -> tuple[
        models.Program,
        list[models.WorkItem],
        list[models.ExecutionBranch],
        list[models.ExecutionBranch],
        list[models.WorkEdge],
        list[models.SessionAttempt],
    ]:
        with self.session_factory() as session:
            program = self._program_by_code(session, program_code)
            if self._expire_stale_sessions(session):
                session.commit()
            work_items = list(
                session.scalars(
                    select(models.WorkItem)
                    .where(
                        models.WorkItem.program_id == program.id,
                        models.WorkItem.kind != PROGRAM_DESK_KIND,
                        models.WorkItem.hidden.is_(False),
                    )
                    .order_by(models.WorkItem.priority.desc(), models.WorkItem.created_at)
                )
            )
            work_ids = [item.id for item in work_items]
            all_branches = (
                list(
                    session.scalars(
                        select(models.ExecutionBranch)
                        .where(models.ExecutionBranch.work_item_id.in_(work_ids))
                        .order_by(models.ExecutionBranch.created_at)
                    )
                )
                if work_ids
                else []
            )
            branches = [branch for branch in all_branches if not branch.hidden]
            hidden_branches = [branch for branch in all_branches if branch.hidden]
            edges = (
                list(
                    session.scalars(
                        select(models.WorkEdge).where(
                            models.WorkEdge.from_work_item_id.in_(work_ids)
                        )
                    )
                )
                if work_ids
                else []
            )
            latest_sessions = []
            for branch in all_branches:
                latest = session.scalar(
                    select(models.SessionAttempt)
                    .where(models.SessionAttempt.branch_id == branch.id)
                    .order_by(models.SessionAttempt.created_at.desc())
                    .limit(1)
                )
                if latest:
                    latest_sessions.append(latest)
            return (
                program,
                work_items,
                branches,
                hidden_branches,
                edges,
                latest_sessions,
            )

    def program_desk_branch(self, program_code: str) -> models.ExecutionBranch:
        with self.session_factory() as session:
            program = self._program_by_code(session, program_code)
            desk = session.scalar(
                select(models.WorkItem).where(
                    models.WorkItem.program_id == program.id,
                    models.WorkItem.kind == PROGRAM_DESK_KIND,
                )
            )
            if desk is None:
                raise NotFoundError(f"Program Desk not found: {program.code}")
            branch = session.scalar(
                select(models.ExecutionBranch).where(
                    models.ExecutionBranch.work_item_id == desk.id
                )
            )
            if branch is None:
                raise NotFoundError(f"Program Desk branch not found: {program.code}")
            return branch

    def program_context(self, program_code: str) -> dict[str, Any]:
        with self.session_factory() as session:
            program = self._program_by_code(session, program_code)
            return self._program_context(session, program)

    def branch_detail(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            # Detail reads happen frequently while a Branch is open.  They only
            # need to reconcile this Branch; sweeping every historical session
            # here turns a small composer-state poll into a global maintenance
            # pass.
            expired_changed = self._expire_stale_sessions(session, branch_id=branch_id)
            orphaned_web = self._reconcile_orphaned_web_turns(session, branch_id=branch_id)
            finished_web = self._reconcile_finished_web_sessions(
                session, branch_id=branch_id
            )
            stale_local = self._reconcile_stale_local_turns(session, branch_id=branch_id)
            if expired_changed or orphaned_web or finished_web or stale_local:
                session.commit()
            branch = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(selectinload(models.ExecutionBranch.work_item))
            )
            if branch is None:
                raise NotFoundError("Branch not found")
            latest_session = session.scalar(
                select(models.SessionAttempt)
                .where(models.SessionAttempt.branch_id == branch.id)
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            messages = self._branch_detail_messages(session, branch.id)
            turns = list(
                session.scalars(
                    select(models.CodexTurn)
                    .where(models.CodexTurn.branch_id == branch.id)
                    .order_by(models.CodexTurn.completed_at)
                )
            )
            turns = [turn for turn in turns if _turn_is_visible(turn)]
            memory = list(
                session.scalars(
                    select(models.MemoryEntry)
                    .where(
                        models.MemoryEntry.scope_type == "branch",
                        models.MemoryEntry.scope_id == branch.id,
                    )
                    .order_by(models.MemoryEntry.created_at.desc())
                )
            )
            external_jobs = list(
                session.scalars(
                    select(models.ExternalJob)
                    .where(models.ExternalJob.work_item_id == branch.work_item_id)
                    .order_by(models.ExternalJob.created_at.desc())
                )
            )
            goals = list(
                session.scalars(
                    select(models.GoalRun)
                    .where(models.GoalRun.branch_id == branch.id)
                    .order_by(models.GoalRun.created_at.desc())
                )
            )
            approvals = list(
                session.scalars(
                    select(models.PendingApproval)
                    .where(
                        models.PendingApproval.branch_id == branch.id,
                        models.PendingApproval.status == "pending",
                    )
                    .order_by(models.PendingApproval.created_at)
                )
            )
            activity = list(
                session.scalars(
                    select(models.ActivityEvent)
                    .where(
                        models.ActivityEvent.scope_type == "branch",
                        models.ActivityEvent.scope_id == branch.id,
                    )
                    .order_by(models.ActivityEvent.created_at.desc())
                    .limit(100)
                )
            )
            return {
                "branch": branch,
                "work_item": branch.work_item,
                "runtime": self._runtime_name(branch),
                "session": latest_session,
                "turns": turns,
                "messages": messages,
                "memory": memory,
                "external_jobs": external_jobs,
                "goals": goals,
                "approvals": approvals,
                "activity": activity,
                "conversation_state": self._conversation_state(session, branch),
            }

    def _branch_detail_messages(
        self, session: Session, branch_id: str
    ) -> list[models.ConversationMessage]:
        """Return the bounded transcript plus recent terminal Web queries.

        Tool and live-activity records can be numerous enough to push an
        interrupted query out of the normal detail window. Terminal queries
        are small, durable user records and must remain visible after a
        refresh, so merge the latest recovery candidates back into the
        bounded transcript without loading the complete activity history.
        """
        recent = list(
            session.scalars(
                select(models.ConversationMessage)
                .where(models.ConversationMessage.branch_id == branch_id)
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(BRANCH_DETAIL_MESSAGE_LIMIT)
            )
        )
        terminal_queries = list(
            session.scalars(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch_id,
                    models.ConversationMessage.role == "user",
                    models.ConversationMessage.metadata_json["origin"]
                    .as_string()
                    .in_(["web", "continuation", "goal"]),
                    models.ConversationMessage.metadata_json["send_status"]
                    .as_string()
                    .in_(["interrupted", "failed"]),
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(BRANCH_DETAIL_TERMINAL_QUERY_LIMIT)
            )
        )
        by_id = {message.id: message for message in recent}
        by_id.update({message.id: message for message in terminal_queries})
        return sorted(by_id.values(), key=lambda message: message.created_at)

    def branch_conversation_state(self, branch_id: str) -> dict[str, Any]:
        """Return the small, reconciled state payload used by the Web composer."""
        with self.session_factory() as session:
            # This endpoint is intentionally polled more often than the full
            # Branch payload, so keep its reconciliation strictly scoped.
            expired_changed = self._expire_stale_sessions(session, branch_id=branch_id)
            orphaned_web = self._reconcile_orphaned_web_turns(session, branch_id=branch_id)
            finished_web = self._reconcile_finished_web_sessions(
                session, branch_id=branch_id
            )
            stale_local = self._reconcile_stale_local_turns(session, branch_id=branch_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            if expired_changed or orphaned_web or finished_web or stale_local:
                session.commit()
            return self._conversation_state(session, branch)

    def runtime_state(self, branch_id: str) -> dict[str, Any]:
        """Return runtime capabilities without changing the legacy conversation-state shape."""
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            attempt = self._active_attempt(session, branch_id)
            latest = self._latest_web_message(session, branch_id)
            runtime = self._runtime_name(branch)
            state = "idle"
            if latest is not None:
                status = str(latest.metadata_json.get("send_status") or "")
                state = {
                    "pending": "running",
                    "submitted": "settled",
                    "failed": "failed",
                    "interrupted": "aborted",
                }.get(status, "idle")
            owner = attempt.lease_owner if attempt is not None else None
            session_id = (
                attempt.codex_thread_id
                if attempt is not None and attempt.codex_thread_id
                else branch.codex_thread_id
            )
            queue: dict[str, list[str]] = {"steering": [], "follow_up": []}
            snapshot = getattr(self._adapter_for_runtime(runtime), "snapshot", None)
            if callable(snapshot) and session_id:
                live = snapshot(session_id)
                if isinstance(live, dict):
                    live_state = str(live.get("state") or "")
                    if live_state in {"idle", "running", "settled", "failed", "aborted"}:
                        state = live_state
                    if isinstance(live.get("queue"), dict):
                        queue = live["queue"]
            capabilities = ["prompt"]
            if runtime == "pi":
                capabilities.extend(["steer", "follow_up", "abort"])
            elif runtime == "claude":
                capabilities.append("abort")
            return {
                "runtime": runtime,
                "state": state,
                "capabilities": capabilities,
                "session_id": session_id,
                "owner": owner,
                "queue": queue,
            }

    async def fork_branch(
        self, branch_id: str, payload: schemas.BranchForkCreate
    ) -> models.ExecutionBranch:
        with self.session_factory() as session:
            parent = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    ),
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.repository
                    ),
                )
            )
            if parent is None:
                raise NotFoundError("Parent branch not found")
            if parent.hidden:
                raise ConflictError("Restore this hidden Branch before forking from it")
            if not payload.manual_approval:
                raise ConflictError(
                    "Manual approval is required before creating a fork; "
                    "use the Web Fork action or explicitly approve the CLI command"
                )
            if payload.start_session:
                raise ConflictError(
                    "Fork creation cannot start the child automatically; create the fork first, "
                    "then use an explicit child continuation or local Session action"
                )
            if payload.continuation == "continue" and parent.continued_by_branch_id:
                continued = session.get(
                    models.ExecutionBranch, parent.continued_by_branch_id
                )
                if continued is not None:
                    raise ConflictError(
                        f'Branch already continues in "{continued.name}"; reopen the parent '
                        "or fork from the continued Branch"
                    )
                parent.continued_by_branch_id = None
            slug = branch_slug(payload.name)
            normalized_name = payload.name.strip().casefold()
            duplicate_name = session.scalar(
                select(models.ExecutionBranch).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id,
                    func.lower(models.ExecutionBranch.name) == normalized_name,
                )
            )
            if duplicate_name:
                raise ConflictError(f'Branch already exists: "{duplicate_name.name}"')
            duplicate = session.scalar(
                select(models.ExecutionBranch).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id,
                    models.ExecutionBranch.slug == slug,
                )
            )
            if duplicate:
                raise ConflictError(
                    f'Branch name conflicts with existing branch "{duplicate.name}"'
                )
            work_item = parent.work_item
            if work_item.kind == PROGRAM_DESK_KIND:
                raise ConflictError("Program Desk cannot be forked; create a WorkItem instead")
            program = work_item.program
            inherited_memory = self._memory_context(session, parent)
            parent_snapshot = self._parent_fork_snapshot(session, parent)
            workspace = self.workspace.prepare(
                program_code=program.code,
                work_human_id=work_item.human_id,
                branch_slug=slug,
                repository=work_item.repository,
                mode=payload.mode,
            )
            branch = models.ExecutionBranch(
                work_item_id=work_item.id,
                parent_branch_id=parent.id,
                name=payload.name,
                slug=slug,
                runtime=self._runtime_name(parent),
                execution_preferences=dict(parent.execution_preferences or {}),
                mode=payload.mode,
                status="parked" if payload.mode == "capture" else "provisioning",
                cwd=workspace.cwd,
                workspace_path=workspace.cwd,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "parent_branch_id": parent.id,
                    "parent_thread_id": parent.codex_thread_id,
                    "parent_runtime": self._runtime_name(parent),
                    "parent_head_sha": self.workspace.current_head(parent.cwd),
                    "work_item_revision": work_item.revision,
                    "fork_objective": payload.objective,
                    "continuation": (
                        "capture" if payload.mode == "capture" else payload.continuation
                    ),
                    "forked_at": datetime.now(UTC).isoformat(),
                    "memory_snapshot_version": 1,
                    "inherited_memory": inherited_memory,
                    "parent_snapshot": parent_snapshot,
                    "provisioning": {
                        "state": (
                            "capture" if payload.mode == "capture" else "provisioning"
                        ),
                        "strategy": None,
                        "error": None,
                    },
                },
                memory_cursor=parent.memory_cursor,
            )
            session.add(branch)
            session.flush()
            self._event(
                session,
                "branch",
                branch.id,
                "branch.forked",
                {"parent_branch_id": parent.id, "objective": payload.objective},
            )
            session.commit()
            parent_thread_id = parent.codex_thread_id
            instructions = developer_instructions(
                build_brief(
                    program,
                    work_item,
                    branch,
                    memory_context=inherited_memory,
                )
            )

        if payload.mode != "capture":
            branch = await self._provision_fork(
                branch_id=branch.id,
                parent_thread_id=parent_thread_id,
                instructions=instructions,
                continuation=payload.continuation,
            )
        await self.event_hub.publish(branch.id, {"type": "branch.created", "branch_id": branch.id})
        return branch

    async def continue_with_pi(
        self, branch_id: str, payload: schemas.PiContinuationCreate
    ) -> models.ExecutionBranch:
        """Create a Pi sidecar from a frozen, safe Codex handoff.

        Codex and Pi persist incompatible JSONL formats, so this operation never
        tries to reuse a Codex rollout as a Pi session. Instead, it freezes the
        completed, redacted task context that a new Pi runtime can safely inspect.
        The default is a parallel Branch: the originating Codex Branch remains
        available for normal work and Pi is an explicitly bounded fallback.
        """
        if not payload.manual_approval:
            raise ConflictError("Manual approval is required before continuing with Pi")
        pi = self._adapter_for_runtime("pi")
        health = await pi.health()
        if not health.get("available"):
            raise ConflictError(
                f"Pi is unavailable: {redact(str(health.get('error') or 'health check failed'))}"
            )
        with self.session_factory() as session:
            parent = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    ),
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.repository
                    ),
                )
            )
            if parent is None:
                raise NotFoundError("Branch not found")
            if self._runtime_name(parent) != "codex":
                raise ConflictError("Only a Codex Branch can create a Pi sidecar")
            if parent.hidden:
                raise ConflictError("Restore this hidden Branch before continuing with Pi")
            if parent.mode == "capture" or parent.work_item.kind == PROGRAM_DESK_KIND:
                raise ConflictError("This Branch cannot create a Pi sidecar")
            if parent.status != "active":
                raise ConflictError(f"Branch is {parent.status}; it cannot continue with Pi")
            if payload.continuation == "continue" and parent.continued_by_branch_id:
                raise ConflictError("This Branch already has a continuation")
            if self._active_attempt(session, parent.id) is not None:
                raise ConflictError("Finish the active Codex session before continuing with Pi")
            if self._pending_web_message(session, parent.id) is not None:
                raise ConflictError("Wait for the current Codex Web turn before continuing with Pi")
            if not parent.codex_thread_id or parent.codex_thread_id.startswith("deferred:"):
                raise ConflictError("The Codex Branch has no persisted session to hand off")

            runtime_handoff = self._codex_to_pi_handoff(session, parent)
            if not runtime_handoff["completed_turns"]:
                raise ConflictError("Complete at least one Codex turn before continuing with Pi")
            name = self._next_pi_continuation_name(session, parent, payload.name)
            slug = branch_slug(name)
            # Scratch work is intentionally allowed to start without an objective.
            # A Pi continuation still needs a bounded task description for its
            # child brief, so derive one from the most durable context available
            # instead of turning a valid completed scratch branch into a dead end.
            objective = self._pi_continuation_objective(
                parent=parent,
                handoff=runtime_handoff,
                requested=payload.objective,
            )
            workspace = self.workspace.prepare(
                program_code=parent.work_item.program.code,
                work_human_id=parent.work_item.human_id,
                branch_slug=slug,
                repository=parent.work_item.repository,
                mode="write",
            )
            inherited_memory = self._memory_context(session, parent)
            parent_snapshot = self._parent_fork_snapshot(session, parent)
            child = models.ExecutionBranch(
                work_item_id=parent.work_item_id,
                parent_branch_id=parent.id,
                name=name,
                slug=slug,
                runtime="pi",
                execution_preferences=dict(parent.execution_preferences or {}),
                mode="write",
                status="provisioning",
                cwd=workspace.cwd,
                workspace_path=workspace.cwd,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "parent_branch_id": parent.id,
                    "parent_thread_id": parent.codex_thread_id,
                    "parent_runtime": "codex",
                    "parent_head_sha": self.workspace.current_head(parent.cwd),
                    "work_item_revision": parent.work_item.revision,
                    "fork_objective": objective,
                    "continuation": payload.continuation,
                    "forked_at": datetime.now(UTC).isoformat(),
                    "memory_snapshot_version": 1,
                    "inherited_memory": inherited_memory,
                    "parent_snapshot": parent_snapshot,
                    "runtime_handoff": runtime_handoff,
                    "provisioning": {
                        "state": "provisioning",
                        "strategy": "codex_to_pi_handoff",
                        "error": None,
                    },
                },
                memory_cursor=parent.memory_cursor,
            )
            session.add(child)
            session.flush()
            self._event(
                session,
                "branch",
                child.id,
                "branch.runtime_continuation_created",
                {
                    "parent_branch_id": parent.id,
                    "source_runtime": "codex",
                    "target_runtime": "pi",
                    "continuation": payload.continuation,
                    "handoff_hash": runtime_handoff["handoff_hash"],
                },
            )
            session.commit()
            instructions = developer_instructions(
                build_brief(
                    parent.work_item.program,
                    parent.work_item,
                    child,
                    memory_context=inherited_memory,
                )
            )
            parent_thread_id = parent.codex_thread_id

        child = await self._provision_fork(
            branch_id=child.id,
            parent_thread_id=parent_thread_id,
            instructions=instructions,
            continuation=payload.continuation,
        )
        await self.event_hub.publish(child.id, {"type": "branch.created", "branch_id": child.id})
        return child

    async def continue_with_codex(
        self, branch_id: str, payload: schemas.CodexContinuationCreate
    ) -> models.ExecutionBranch:
        """Create a Codex sidecar from a frozen Pi handoff.

        Pi and Codex sessions use incompatible persistence.  This deliberately
        starts a fresh Codex thread in a separate workspace rather than trying
        to resume a Pi session id as a Codex session.  The source Pi Branch
        remains active by default and its evidence is retained in the handoff.
        """
        if not payload.manual_approval:
            raise ConflictError("Manual approval is required before continuing with Codex")
        codex = self._adapter_for_runtime("codex")
        health = await codex.health()
        if not health.get("available"):
            raise ConflictError(
                f"Codex is unavailable: {redact(str(health.get('error') or 'health check failed'))}"
            )
        with self.session_factory() as session:
            parent = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    ),
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.repository
                    ),
                )
            )
            if parent is None:
                raise NotFoundError("Branch not found")
            source_runtime = self._runtime_name(parent)
            if source_runtime not in {"pi", "claude"}:
                raise ConflictError("Only a Pi or Claude Branch can create a Codex continuation")
            if parent.hidden:
                raise ConflictError("Restore this hidden Branch before continuing with Codex")
            if parent.mode == "capture" or parent.work_item.kind == PROGRAM_DESK_KIND:
                raise ConflictError("This Branch cannot create a Codex continuation")
            if parent.status != "active":
                raise ConflictError(f"Branch is {parent.status}; it cannot continue with Codex")
            if payload.continuation == "continue" and parent.continued_by_branch_id:
                raise ConflictError("This Branch already has a continuation")
            if self._active_attempt(session, parent.id) is not None:
                raise ConflictError("Finish the active Pi session before continuing with Codex")
            if self._pending_web_message(session, parent.id) is not None:
                raise ConflictError("Wait for the current Pi Web turn before continuing with Codex")
            if not parent.codex_thread_id:
                raise ConflictError("The Pi Branch has no persisted session to hand off")

            runtime_handoff = (
                self._pi_to_codex_handoff(session, parent)
                if source_runtime == "pi"
                else self._runtime_handoff(session, parent, target_runtime="codex")
            )
            if not runtime_handoff["completed_turns"]:
                raise ConflictError("Complete at least one Pi turn before continuing with Codex")
            name = self._next_codex_continuation_name(session, parent, payload.name)
            slug = branch_slug(name)
            objective = self._codex_continuation_objective(
                parent=parent,
                handoff=runtime_handoff,
                requested=payload.objective,
            )
            workspace = self.workspace.prepare(
                program_code=parent.work_item.program.code,
                work_human_id=parent.work_item.human_id,
                branch_slug=slug,
                repository=parent.work_item.repository,
                mode="write",
            )
            inherited_memory = self._memory_context(session, parent)
            parent_snapshot = self._parent_fork_snapshot(session, parent)
            child = models.ExecutionBranch(
                work_item_id=parent.work_item_id,
                parent_branch_id=parent.id,
                name=name,
                slug=slug,
                runtime="codex",
                execution_preferences=dict(parent.execution_preferences or {}),
                mode="write",
                status="provisioning",
                cwd=workspace.cwd,
                workspace_path=workspace.cwd,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "parent_branch_id": parent.id,
                    "parent_thread_id": parent.codex_thread_id,
                    "parent_runtime": source_runtime,
                    "parent_head_sha": self.workspace.current_head(parent.cwd),
                    "work_item_revision": parent.work_item.revision,
                    "fork_objective": objective,
                    "continuation": payload.continuation,
                    "forked_at": datetime.now(UTC).isoformat(),
                    "memory_snapshot_version": 1,
                    "inherited_memory": inherited_memory,
                    "parent_snapshot": parent_snapshot,
                    "runtime_handoff": runtime_handoff,
                    "provisioning": {
                        "state": "provisioning",
                        "strategy": f"{source_runtime}_to_codex_handoff",
                        "error": None,
                    },
                },
                memory_cursor=parent.memory_cursor,
            )
            session.add(child)
            session.flush()
            self._event(
                session,
                "branch",
                child.id,
                "branch.runtime_continuation_created",
                {
                    "parent_branch_id": parent.id,
                    "source_runtime": source_runtime,
                    "target_runtime": "codex",
                    "continuation": payload.continuation,
                    "handoff_hash": runtime_handoff["handoff_hash"],
                },
            )
            session.commit()
            instructions = developer_instructions(
                build_brief(
                    parent.work_item.program,
                    parent.work_item,
                    child,
                    memory_context=inherited_memory,
                )
            )
            parent_thread_id = parent.codex_thread_id

        child = await self._provision_fork(
            branch_id=child.id,
            parent_thread_id=parent_thread_id,
            instructions=instructions,
            continuation=payload.continuation,
        )
        await self.event_hub.publish(child.id, {"type": "branch.created", "branch_id": child.id})
        return child

    def preview_claude_handoff(self, branch_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            handoff = self._runtime_handoff(session, branch, target_runtime="claude")
        return {
            "handoff_hash": handoff["handoff_hash"],
            "turn_count": len(handoff["completed_turns"]),
            "prompt": render_claude_handoff(handoff),
            "account": self.settings.claude_account_label,
            "model": self.settings.claude_model,
            "effort": self.settings.claude_effort,
        }

    def prepare_claude_workspace_handoff(
        self, branch_id: str, *, manual_approval: bool, take_over: bool = False
    ) -> dict[str, Any]:
        if not manual_approval:
            raise ConflictError("Manual approval is required for a workspace handoff")
        with self.session_factory() as session:
            child = self._branch(session, branch_id)
            if child.runtime != "claude" or not child.parent_branch_id:
                raise ConflictError("Choose a Claude child Branch")
            parent = self._branch(session, child.parent_branch_id)
            for branch in (parent, child):
                if branch.status != "active" or branch.hidden:
                    raise ConflictError("Both Branches must be active and visible")
                state = self._conversation_state(session, branch)
                if self._active_attempt(session, branch.id) or not state["can_send"]:
                    raise ConflictError("Finish the current turn before handing off files")
                monitor = session.get(models.NativeGoalMonitor, branch.id)
                if monitor and (
                    monitor.goal_snapshot.get("status") == "active" or monitor.next_retry_at
                ):
                    raise ConflictError("Pause the active Goal before handing off files")
                if session.scalar(select(models.GoalRun.id).where(
                    models.GoalRun.branch_id == branch.id, models.GoalRun.status == "active"
                ).limit(1)):
                    raise ConflictError("Pause the active Goal before handing off files")
            if child.context_snapshot.get("workspace_snapshot"):
                raise ConflictError("This Branch already has a workspace snapshot")
            source, destination, parent_id = Path(parent.cwd), Path(child.cwd), parent.id
            parent.status = child.status = "provisioning"
            session.commit()
        try:
            snapshot = apply_workspace_snapshot(plan_workspace_snapshot(source, destination))
            with self.session_factory() as session:
                child, parent = self._branch(session, branch_id), self._branch(session, parent_id)
                child.context_snapshot = {
                    **child.context_snapshot, "workspace_snapshot": snapshot,
                    "continuation": "continue" if take_over else "parallel",
                }
                child.status = "active"
                parent.status = "continued" if take_over else "active"
                if take_over:
                    parent.continued_by_branch_id = child.id
                    check = session.scalar(select(models.ScheduledCheck).where(
                        models.ScheduledCheck.branch_id == parent.id,
                        models.ScheduledCheck.status == "active",
                    ))
                    if check:
                        check.status = check.phase = "paused"
                        check.next_run_at = None
                        check.reason = "Work continued in a child Branch"
                    self._event(session, "branch", parent.id, "branch.continued", {
                        "continued_by_branch_id": child.id,
                    })
                self._event(session, "branch", child.id, "workspace.snapshot_created", snapshot)
                session.commit()
            return snapshot
        except Exception:
            with self.session_factory() as session:
                for identity in (branch_id, parent_id):
                    branch = self._branch(session, identity)
                    if branch.status == "provisioning":
                        branch.status = "active"
                session.commit()
            raise

    async def continue_with_claude(
        self, branch_id: str, payload: schemas.ClaudeContinuationCreate
    ) -> models.ExecutionBranch:
        """Create a parallel Claude Branch from a bounded runtime handoff."""
        if not payload.manual_approval:
            raise ConflictError("Manual approval is required before continuing with Claude")
        claude = self._adapter_for_runtime("claude")
        health = await claude.health()
        if not health.get("available"):
            raise ConflictError(
                "Claude is unavailable: "
                f"{redact(str(health.get('error') or 'health check failed'))}"
            )
        with self.session_factory() as session:
            parent = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    ),
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.repository
                    ),
                )
            )
            if parent is None:
                raise NotFoundError("Branch not found")
            source_runtime = self._runtime_name(parent)
            if source_runtime == "claude":
                raise ConflictError("This Branch already uses Claude")
            if parent.hidden:
                raise ConflictError("Restore this hidden Branch before continuing with Claude")
            if parent.mode == "capture" or parent.work_item.kind == PROGRAM_DESK_KIND:
                raise ConflictError("This Branch cannot create a Claude continuation")
            if parent.status != "active":
                raise ConflictError(f"Branch is {parent.status}; it cannot continue with Claude")
            if payload.continuation == "continue" and parent.continued_by_branch_id:
                raise ConflictError("This Branch already has a continuation")
            if self._active_attempt(session, parent.id) is not None:
                raise ConflictError(
                    f"Finish the active {source_runtime} session before continuing with Claude"
                )
            if self._pending_web_message(session, parent.id) is not None:
                raise ConflictError(
                    f"Wait for the current {source_runtime} Web turn before continuing with Claude"
                )
            if not parent.codex_thread_id or parent.codex_thread_id.startswith("deferred:"):
                raise ConflictError(
                    f"The {source_runtime} Branch has no persisted session to hand off"
                )

            runtime_handoff = self._runtime_handoff(
                session, parent, target_runtime="claude"
            )
            if (
                payload.include_context
                and payload.expected_handoff_hash
                and payload.expected_handoff_hash != runtime_handoff["handoff_hash"]
            ):
                raise ConflictError("The source conversation changed. Refresh the handoff preview.")
            if not runtime_handoff["completed_turns"]:
                raise ConflictError(
                    f"Complete at least one {source_runtime} turn before continuing with Claude"
                )
            name = self._next_runtime_continuation_name(
                session, parent, payload.name, runtime="Claude"
            )
            slug = branch_slug(name)
            objective = self._runtime_continuation_objective(
                parent=parent,
                handoff=runtime_handoff,
                requested=payload.objective,
                source_runtime=source_runtime,
            )
            workspace = self.workspace.prepare(
                program_code=parent.work_item.program.code,
                work_human_id=parent.work_item.human_id,
                branch_slug=slug,
                repository=parent.work_item.repository,
                mode="write",
            )
            workspace_snapshot_source = parent.cwd if parent.work_item.repository is None else None
            inherited_memory = self._memory_context(session, parent)
            parent_snapshot = self._parent_fork_snapshot(session, parent)
            child = models.ExecutionBranch(
                work_item_id=parent.work_item_id,
                parent_branch_id=parent.id,
                name=name,
                slug=slug,
                runtime="claude",
                execution_preferences={},
                mode="write",
                status="provisioning",
                cwd=workspace.cwd,
                workspace_path=workspace.cwd,
                git_branch=workspace.git_branch,
                base_sha=workspace.base_sha,
                context_snapshot={
                    "parent_branch_id": parent.id,
                    "parent_thread_id": parent.codex_thread_id,
                    "parent_runtime": source_runtime,
                    "parent_head_sha": self.workspace.current_head(parent.cwd),
                    "work_item_revision": parent.work_item.revision,
                    "fork_objective": objective,
                    "continuation": payload.continuation,
                    "forked_at": datetime.now(UTC).isoformat(),
                    "memory_snapshot_version": 1,
                    "inherited_memory": inherited_memory,
                    "parent_snapshot": parent_snapshot,
                    "runtime_handoff": runtime_handoff,
                    "claude_handoff_delivery": {
                        "approved": payload.include_context,
                        "mode": "first_user_message",
                    },
                    "workspace_snapshot": None,
                    "provisioning": {
                        "state": "provisioning",
                        "strategy": f"{source_runtime}_to_claude_handoff",
                        "error": None,
                    },
                },
                memory_cursor=parent.memory_cursor,
            )
            session.add(child)
            session.flush()
            self._event(
                session,
                "branch",
                child.id,
                "branch.runtime_continuation_created",
                {
                    "parent_branch_id": parent.id,
                    "source_runtime": source_runtime,
                    "target_runtime": "claude",
                    "continuation": payload.continuation,
                    "handoff_hash": runtime_handoff["handoff_hash"],
                },
            )
            session.commit()
            instructions = developer_instructions(
                build_brief(
                    parent.work_item.program,
                    parent.work_item,
                    child,
                    memory_context=inherited_memory,
                )
            )
            parent_thread_id = parent.codex_thread_id

        if workspace_snapshot_source:
            # File I/O must not hold a SQLite read transaction across the later write.
            snapshot = await asyncio.to_thread(
                lambda: apply_workspace_snapshot(plan_workspace_snapshot(
                    Path(workspace_snapshot_source), Path(workspace.cwd)
                ))
            )
            with self.session_factory() as session:
                child = self._branch(session, child.id)
                child.context_snapshot = {**child.context_snapshot, "workspace_snapshot": snapshot}
                session.commit()
        child = await self._provision_fork(
            branch_id=child.id,
            parent_thread_id=parent_thread_id,
            instructions=instructions,
            continuation=payload.continuation,
        )
        await self.event_hub.publish(child.id, {"type": "branch.created", "branch_id": child.id})
        return child

    def _next_runtime_continuation_name(
        self,
        session: Session,
        parent: models.ExecutionBranch,
        requested_name: str | None,
        *,
        runtime: str,
    ) -> str:
        base = str(requested_name or "").strip()
        if not base:
            parent_label = parent.name.strip()
            if not parent_label or parent_label.casefold() == "main":
                parent_label = parent.work_item.title.strip() or parent.work_item.human_id
            base = f"{runtime} · {parent_label}"
        base = base[:160].rstrip()
        names = {
            str(name).casefold()
            for name in session.scalars(
                select(models.ExecutionBranch.name).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id
                )
            )
        }
        if base.casefold() not in names:
            return base
        suffix = 2
        while True:
            candidate = f"{base[: max(1, 160 - len(str(suffix)) - 1)]} {suffix}"
            if candidate.casefold() not in names:
                return candidate
            suffix += 1

    @staticmethod
    def _runtime_continuation_objective(
        *,
        parent: models.ExecutionBranch,
        handoff: dict[str, Any],
        requested: str | None,
        source_runtime: str,
    ) -> str:
        candidates = (
            requested,
            parent.context_snapshot.get("fork_objective"),
            parent.work_item.objective,
            parent.work_item.title,
        )
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value[:4_000]
        for turn in reversed(handoff.get("completed_turns") or []):
            if isinstance(turn, dict) and str(turn.get("prompt") or "").strip():
                prompt = str(turn["prompt"]).strip()
                return f"Continue the latest {source_runtime} request:\n{prompt[:3_800]}"
        return f"Continue the current work from the completed {source_runtime} handoff."

    def _runtime_handoff(
        self,
        session: Session,
        parent: models.ExecutionBranch,
        *,
        target_runtime: str,
    ) -> dict[str, Any]:
        source_runtime = self._runtime_name(parent)
        turns = list(
            session.scalars(
                select(models.CodexTurn)
                .where(
                    models.CodexTurn.branch_id == parent.id,
                    models.CodexTurn.status == "completed",
                )
                .order_by(models.CodexTurn.completed_at.desc())
                .limit(6)
            )
        )
        remaining = 36_000
        completed_turns: list[dict[str, Any]] = []
        for turn in turns:
            prompt = redact(turn.prompt or "")[-4_000:]
            answer_budget = max(0, min(10_000, remaining - len(prompt)))
            if answer_budget <= 0:
                break
            answer = redact(turn.final_answer or "")[-answer_budget:]
            remaining -= len(prompt) + len(answer)
            completed_turns.append(
                {
                    "turn_id": turn.external_turn_id,
                    "completed_at": turn.completed_at.isoformat(),
                    "prompt": prompt,
                    "final_answer": answer,
                }
            )
        completed_turns.reverse()
        latest_attempt = session.scalar(
            select(models.SessionAttempt)
            .where(models.SessionAttempt.branch_id == parent.id)
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )
        latest_handoff = latest_attempt.handoff if latest_attempt and latest_attempt.handoff else {}
        handoff = {
            "schema_version": "1.0",
            "kind": f"{source_runtime}_to_{target_runtime}",
            "source": {
                "branch_id": parent.id,
                "branch_name": parent.name,
                "work_id": parent.work_item.human_id,
                "runtime": source_runtime,
                "thread_id": parent.codex_thread_id,
                "session_id": latest_attempt.id if latest_attempt else None,
                "workspace_path": parent.cwd,
                "exported_at": datetime.now(UTC).isoformat(),
            },
            "completed_turns": completed_turns,
            "latest_handoff": {
                key: redact(str(latest_handoff.get(key) or ""))[:4_000]
                for key in ("outcome", "conclusion", "next_action")
                if latest_handoff.get(key) is not None
            },
            "upstream_handoff": self._runtime_handoff_lineage(
                parent.context_snapshot.get("runtime_handoff")
            ),
        }
        if target_runtime == "claude":
            bound_claude_handoff(handoff)
        hash_input = {**handoff, "source": {
            key: value for key, value in handoff["source"].items() if key != "exported_at"
        }}
        handoff["handoff_hash"] = hashlib.sha256(
            json.dumps(hash_input, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return handoff

    def _next_pi_continuation_name(
        self,
        session: Session,
        parent: models.ExecutionBranch,
        requested_name: str | None,
    ) -> str:
        base = str(requested_name or "").strip()
        if not base:
            parent_label = parent.name.strip()
            if not parent_label or parent_label.casefold() == "main":
                parent_label = (
                    parent.work_item.title.strip()
                    or parent.work_item.human_id
                )
            base = f"Pi · {parent_label}"
        base = base[:160].rstrip()
        names = {
            str(name).casefold()
            for name in session.scalars(
                select(models.ExecutionBranch.name).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id
                )
            )
        }
        if base.casefold() not in names:
            return base
        suffix = 2
        while True:
            candidate = f"{base[: max(1, 160 - len(str(suffix)) - 1)]} {suffix}"
            if candidate.casefold() not in names:
                return candidate
            suffix += 1

    def _next_codex_continuation_name(
        self,
        session: Session,
        parent: models.ExecutionBranch,
        requested_name: str | None,
    ) -> str:
        base = str(requested_name or "").strip()
        if not base:
            parent_label = parent.name.strip()
            if parent_label.startswith("Pi · "):
                parent_label = parent_label.removeprefix("Pi · ").strip()
            if not parent_label or parent_label.casefold() == "main":
                parent_label = parent.work_item.title.strip() or parent.work_item.human_id
            base = f"Codex · {parent_label}"
        base = base[:160].rstrip()
        names = {
            str(name).casefold()
            for name in session.scalars(
                select(models.ExecutionBranch.name).where(
                    models.ExecutionBranch.work_item_id == parent.work_item_id
                )
            )
        }
        if base.casefold() not in names:
            return base
        suffix = 2
        while True:
            candidate = f"{base[: max(1, 160 - len(str(suffix)) - 1)]} {suffix}"
            if candidate.casefold() not in names:
                return candidate
            suffix += 1

    @staticmethod
    def _pi_continuation_objective(
        *,
        parent: models.ExecutionBranch,
        handoff: dict[str, Any],
        requested: str | None,
    ) -> str:
        """Choose a safe, useful objective for a Pi child Branch.

        WorkItems created as scratch/emerging work may have an empty objective.
        The handoff is already redacted and bounded, so it is the right fallback
        source before a generic title.  The returned text is used as context,
        not as a claim that the user explicitly defined a formal objective.
        """

        candidates = (
            requested,
            parent.work_item.objective,
            (handoff.get("latest_handoff") or {}).get("next_action")
            if isinstance(handoff.get("latest_handoff"), dict)
            else None,
            (handoff.get("latest_handoff") or {}).get("conclusion")
            if isinstance(handoff.get("latest_handoff"), dict)
            else None,
        )
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value[:4_000]

        completed_turns = handoff.get("completed_turns")
        if isinstance(completed_turns, list):
            for turn in reversed(completed_turns):
                if not isinstance(turn, dict):
                    continue
                prompt = str(turn.get("prompt") or "").strip()
                if prompt:
                    return (
                        "Continue the current work described by the latest completed "
                        f"request:\n{prompt[:3_800]}"
                    )

        for candidate in (parent.work_item.title, parent.name):
            value = str(candidate or "").strip()
            if value:
                return f"Continue the current work: {value}"[:4_000]

        return "Continue the current work from the completed Codex handoff."

    @staticmethod
    def _codex_continuation_objective(
        *,
        parent: models.ExecutionBranch,
        handoff: dict[str, Any],
        requested: str | None,
    ) -> str:
        """Choose a bounded Codex objective from the latest Pi evidence."""
        if str(requested or "").strip():
            return str(requested).strip()[:4_000]
        latest_handoff = handoff.get("latest_handoff")
        if isinstance(latest_handoff, dict):
            for key in ("next_action", "conclusion"):
                value = str(latest_handoff.get(key) or "").strip()
                if value:
                    return value[:4_000]
        completed_turns = handoff.get("completed_turns")
        if isinstance(completed_turns, list):
            for turn in reversed(completed_turns):
                if not isinstance(turn, dict):
                    continue
                prompt = str(turn.get("prompt") or "").strip()
                if prompt:
                    return (
                        "Continue the current work described by the latest completed Pi request:\n"
                        f"{prompt[:3_800]}"
                    )
        for candidate in (
            parent.context_snapshot.get("fork_objective"),
            parent.work_item.objective,
            parent.work_item.title,
            parent.name,
        ):
            value = str(candidate or "").strip()
            if value:
                return f"Continue the current work: {value}"[:4_000]
        return "Continue the current work from the completed Pi handoff."

    def _codex_to_pi_handoff(
        self, session: Session, parent: models.ExecutionBranch
    ) -> dict[str, Any]:
        rollout_path = (
            find_rollout(self.settings.codex_home / "sessions", parent.codex_thread_id)
            if parent.codex_thread_id and not parent.codex_thread_id.startswith("deferred:")
            else None
        )
        turns = list(
            session.scalars(
                select(models.CodexTurn)
                .where(
                    models.CodexTurn.branch_id == parent.id,
                    models.CodexTurn.status == "completed",
                )
                .order_by(models.CodexTurn.completed_at.desc())
                .limit(6)
            )
        )
        turns.reverse()
        remaining = 36_000
        completed_turns: list[dict[str, Any]] = []
        for turn in turns:
            prompt = redact(turn.prompt or "")[-4_000:]
            answer = redact(turn.final_answer or "")
            answer_budget = max(0, min(10_000, remaining - len(prompt)))
            if answer_budget <= 0:
                break
            answer = answer[-answer_budget:]
            remaining -= len(prompt) + len(answer)
            completed_turns.append(
                {
                    "turn_id": turn.external_turn_id,
                    "completed_at": turn.completed_at.isoformat(),
                    "prompt": prompt,
                    "final_answer": answer,
                }
            )
        latest_attempt = session.scalar(
            select(models.SessionAttempt)
            .where(models.SessionAttempt.branch_id == parent.id)
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )
        latest_handoff = latest_attempt.handoff if latest_attempt and latest_attempt.handoff else {}
        handoff = {
            "schema_version": "1.0",
            "kind": "codex_to_pi",
            "source": {
                "branch_id": parent.id,
                "branch_name": parent.name,
                "work_id": parent.work_item.human_id,
                "runtime": "codex",
                "thread_id": parent.codex_thread_id,
                "session_id": latest_attempt.id if latest_attempt else None,
                # Pi runs in an independent workspace. These references let it
                # inspect the originating work only when the bounded handoff
                # needs corroboration; they are never a license to write there.
                "workspace_path": parent.cwd,
                "rollout_path": str(rollout_path) if rollout_path else None,
                "exported_at": datetime.now(UTC).isoformat(),
            },
            "completed_turns": completed_turns,
            "latest_handoff": {
                key: redact(str(latest_handoff.get(key) or ""))[:4_000]
                for key in ("outcome", "conclusion", "next_action")
                if latest_handoff.get(key) is not None
            },
        }
        handoff["handoff_hash"] = hashlib.sha256(
            json.dumps(handoff, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return handoff

    def _pi_to_codex_handoff(
        self, session: Session, parent: models.ExecutionBranch
    ) -> dict[str, Any]:
        """Produce a bounded, traceable Pi-to-Codex handoff."""
        turns = list(
            session.scalars(
                select(models.CodexTurn)
                .where(
                    models.CodexTurn.branch_id == parent.id,
                    models.CodexTurn.status == "completed",
                )
                .order_by(models.CodexTurn.completed_at.desc())
                .limit(6)
            )
        )
        turns.reverse()
        remaining = 36_000
        completed_turns: list[dict[str, Any]] = []
        for turn in turns:
            prompt = redact(turn.prompt or "")[-4_000:]
            answer = redact(turn.final_answer or "")
            answer_budget = max(0, min(10_000, remaining - len(prompt)))
            if answer_budget <= 0:
                break
            answer = answer[-answer_budget:]
            remaining -= len(prompt) + len(answer)
            completed_turns.append(
                {
                    "turn_id": turn.external_turn_id,
                    "completed_at": turn.completed_at.isoformat(),
                    "prompt": prompt,
                    "final_answer": answer,
                }
            )
        latest_attempt = session.scalar(
            select(models.SessionAttempt)
            .where(models.SessionAttempt.branch_id == parent.id)
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )
        latest_handoff = latest_attempt.handoff if latest_attempt and latest_attempt.handoff else {}
        upstream = self._runtime_handoff_lineage(parent.context_snapshot.get("runtime_handoff"))
        handoff = {
            "schema_version": "1.0",
            "kind": "pi_to_codex",
            "source": {
                "branch_id": parent.id,
                "branch_name": parent.name,
                "work_id": parent.work_item.human_id,
                "runtime": "pi",
                "thread_id": parent.codex_thread_id,
                "session_id": latest_attempt.id if latest_attempt else None,
                "workspace_path": parent.cwd,
                "exported_at": datetime.now(UTC).isoformat(),
            },
            "completed_turns": completed_turns,
            "latest_handoff": {
                key: redact(str(latest_handoff.get(key) or ""))[:4_000]
                for key in ("outcome", "conclusion", "next_action")
                if latest_handoff.get(key) is not None
            },
            "upstream_handoff": upstream,
        }
        handoff["handoff_hash"] = hashlib.sha256(
            json.dumps(handoff, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return handoff

    @staticmethod
    def _runtime_handoff_lineage(value: Any) -> dict[str, Any]:
        """Keep a small trace of an earlier cross-runtime handoff."""
        if not isinstance(value, dict):
            return {}
        source = value.get("source") if isinstance(value.get("source"), dict) else {}
        upstream_turns = value.get("completed_turns")
        excerpts: list[dict[str, Any]] = []
        if isinstance(upstream_turns, list):
            for turn in upstream_turns[-2:]:
                if not isinstance(turn, dict):
                    continue
                excerpts.append(
                    {
                        "turn_id": turn.get("turn_id"),
                        "completed_at": turn.get("completed_at"),
                        "prompt": redact(str(turn.get("prompt") or ""))[-1_500:],
                        "final_answer": redact(str(turn.get("final_answer") or ""))[-4_500:],
                    }
                )
        return {
            "kind": value.get("kind"),
            "source": {
                key: source.get(key)
                for key in ("branch_id", "branch_name", "work_id", "runtime", "thread_id")
                if source.get(key) is not None
            },
            "latest_completed_turns": excerpts,
        }

    def continuation_state(self, branch_id: str) -> dict[str, Any] | None:
        """Return the durable child-continuation state, if one was queued."""
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            value = (branch.context_snapshot or {}).get(AUTO_CONTINUATION_KEY)
            return dict(value) if isinstance(value, dict) else None

    def queue_branch_continuation(
        self, branch_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Persist a request for the first turn on a child Branch.

        Fork provisioning and turn execution are separate operations.  Keeping
        the request in the Branch snapshot makes it survive a CLI exit or a
        Research Ops restart, while the dispatcher below owns the actual Codex
        call.  The stored prompt is redacted before it enters durable state.
        """
        now = datetime.now(UTC)
        with self.session_factory() as session:
            branch = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(selectinload(models.ExecutionBranch.work_item))
            )
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.mode == "capture":
                raise ConflictError("Capture-only Branches cannot continue")
            if branch.hidden:
                raise ConflictError("Restore this hidden Branch before continuing it")
            if branch.status != "active":
                raise ConflictError(f"Branch is {branch.status}; it cannot continue")
            if not branch.codex_thread_id or branch.codex_thread_id.startswith("deferred:"):
                raise ConflictError("The child Codex thread is not ready")

            snapshot = dict(branch.context_snapshot or {})
            current = snapshot.get(AUTO_CONTINUATION_KEY)
            if isinstance(current, dict):
                current_status = str(current.get("status", ""))
                if current_status in {"queued", "running"}:
                    return dict(current)
                if current_status == "completed" and not force:
                    return dict(current)

            objective = str(
                snapshot.get("fork_objective") or branch.work_item.objective or ""
            ).strip()
            if not objective:
                raise ConflictError("The child Branch has no continuation objective")
            request = {
                "status": "queued",
                "prompt": redact(self._continuation_prompt(objective)),
                "prompt_format": "verbatim",
                "requested_at": now.isoformat(),
                "started_at": None,
                "completed_at": None,
                "message_id": None,
                "turn_id": None,
                "error": None,
            }
            snapshot[AUTO_CONTINUATION_KEY] = request
            branch.context_snapshot = snapshot
            self._event(
                session,
                "branch",
                branch.id,
                "branch.continuation_queued",
                {"branch_id": branch.id},
            )
            session.commit()
            return request

    @staticmethod
    def _continuation_prompt(objective: str) -> str:
        return objective

    def _claim_queued_continuations(self) -> list[tuple[str, str]]:
        """Atomically claim queued requests for the in-process dispatcher."""
        now = datetime.now(UTC)
        claimed: list[tuple[str, str]] = []
        with self.session_factory() as session:
            branches = list(
                session.scalars(
                    select(models.ExecutionBranch).where(
                        models.ExecutionBranch.status == "active"
                    )
                )
            )
            for branch in branches:
                snapshot = dict(branch.context_snapshot or {})
                raw_request = snapshot.get(AUTO_CONTINUATION_KEY)
                if not isinstance(raw_request, dict):
                    continue
                # Copy the nested JSON object before mutating it; otherwise
                # SQLAlchemy sees the original dictionary change in place and
                # does not mark the JSON column dirty.
                request = dict(raw_request)
                status = str(request.get("status", ""))
                if status == "running":
                    # A live lease means the worker is still doing useful work.
                    active = session.scalar(
                        select(models.SessionAttempt.id)
                        .where(
                            models.SessionAttempt.branch_id == branch.id,
                            models.SessionAttempt.status.in_(["starting", "active"]),
                            models.SessionAttempt.lease_expires_at > now,
                        )
                        .limit(1)
                    )
                    if active is not None:
                        continue
                    # Do not reclaim a just-started request: a short Web turn
                    # can finish its Session between two dispatcher ticks.
                    # Only recover a genuinely abandoned request after the
                    # grace period, which also covers a hard process crash.
                    started_at = _parse_utc(request.get("started_at"))
                    if (
                        started_at is None
                        or now - started_at
                        < timedelta(minutes=AUTO_CONTINUATION_RECOVERY_MINUTES)
                    ):
                        continue
                    status = "queued"
                    request["status"] = status
                if status != "queued":
                    continue
                prompt = str(request.get("prompt") or "")
                if request.get("prompt_format") != "verbatim":
                    prompt = str(snapshot.get("fork_objective") or branch.work_item.objective or "")
                    request["prompt"] = redact(prompt)
                    request["prompt_format"] = "verbatim"
                if not prompt or not branch.codex_thread_id:
                    request["status"] = "failed"
                    request["error"] = "The child Codex thread is not ready"
                    request["completed_at"] = now.isoformat()
                    snapshot[AUTO_CONTINUATION_KEY] = request
                    branch.context_snapshot = snapshot
                    continue
                request["status"] = "running"
                request["started_at"] = now.isoformat()
                request["error"] = None
                snapshot[AUTO_CONTINUATION_KEY] = request
                branch.context_snapshot = snapshot
                claimed.append((branch.id, prompt))
            if claimed:
                session.commit()
            else:
                # Persist recovered/invalid requests too.  SQLAlchemy tracks
                # JSON mutations only after assigning the full snapshot above.
                session.commit()
        return claimed

    async def dispatch_queued_continuations(self) -> None:
        """Start at most one durable Codex turn per queued child Branch."""
        for branch_id, prompt in await asyncio.to_thread(self._claim_queued_continuations):
            existing = self._continuation_tasks.get(branch_id)
            if existing is not None and not existing.done():
                continue
            task = asyncio.create_task(self._run_continuation(branch_id, prompt))
            self._continuation_tasks[branch_id] = task

            def clear(completed: asyncio.Task[None], *, key: str = branch_id) -> None:
                current = self._continuation_tasks.get(key)
                if current is completed:
                    self._continuation_tasks.pop(key, None)

            task.add_done_callback(clear)

    async def _run_continuation(self, branch_id: str, prompt: str) -> None:
        try:
            message = await self.send_message(
                branch_id,
                schemas.ConversationSend(content=prompt),
                origin="continuation",
                metadata={"auto_continuation": True},
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self._finish_continuation,
                branch_id,
                "failed",
                None,
                None,
                "Continuation worker stopped before the child turn completed",
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self._finish_continuation,
                branch_id,
                "failed",
                None,
                None,
                redact(str(exc) or type(exc).__name__),
            )
            return
        await asyncio.to_thread(
            self._finish_continuation,
            branch_id,
            "completed",
            message.id,
            message.turn_id,
            None,
        )

    def _finish_continuation(
        self,
        branch_id: str,
        status: str,
        message_id: str | None,
        turn_id: str | None,
        error: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                return
            snapshot = dict(branch.context_snapshot or {})
            raw_request = snapshot.get(AUTO_CONTINUATION_KEY)
            if not isinstance(raw_request, dict):
                return
            request = dict(raw_request)
            request["status"] = status
            request["completed_at"] = now.isoformat()
            request["message_id"] = message_id
            request["turn_id"] = turn_id
            request["error"] = error
            snapshot[AUTO_CONTINUATION_KEY] = request
            branch.context_snapshot = snapshot
            self._event(
                session,
                "branch",
                branch.id,
                f"branch.continuation_{status}",
                {"message_id": message_id, "turn_id": turn_id, "error": error},
            )
            session.commit()

    async def repair_fork(
        self, branch_id: str, payload: schemas.BranchForkRepair
    ) -> models.ExecutionBranch:
        if not payload.manual_approval:
            raise ConflictError(
                "Manual approval is required before repairing a fork"
            )
        with self.session_factory() as session:
            branch = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    )
                )
            )
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.parent_branch_id is None:
                raise ConflictError("Only child Branches can repair a fork")
            if branch.mode == "capture":
                raise ConflictError("Capture-only Branches do not have a Codex thread")
            if branch.codex_thread_id and branch.status != "blocked":
                return branch
            parent = self._branch(session, branch.parent_branch_id)
            snapshot = dict(branch.context_snapshot or {})
            snapshot["continuation"] = payload.continuation
            snapshot["parent_snapshot"] = self._repair_parent_snapshot(
                session, parent, branch
            )
            snapshot["provisioning"] = {
                "state": "provisioning",
                "strategy": "repair",
                "error": None,
            }
            branch.context_snapshot = snapshot
            branch.status = "provisioning"
            session.commit()
            parent_thread_id = parent.codex_thread_id
            instructions = developer_instructions(
                self._build_branch_brief(session, branch)
            )

        repaired = await self._provision_fork(
            branch_id=branch_id,
            parent_thread_id=parent_thread_id,
            instructions=instructions,
            continuation=payload.continuation,
        )
        await self.event_hub.publish(
            branch_id,
            {"type": "branch.repaired", "branch_id": branch_id},
        )
        return repaired

    def reopen_branch_path(self, branch_id: str) -> models.ExecutionBranch:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if branch.status != "continued":
                return branch
            previous_target = branch.continued_by_branch_id
            branch.status = "active"
            branch.continued_by_branch_id = None
            self._event(
                session,
                "branch",
                branch.id,
                "branch.reopened",
                {"previous_continuation_branch_id": previous_target},
            )
            session.commit()
            return branch

    async def _provision_fork(
        self,
        *,
        branch_id: str,
        parent_thread_id: str | None,
        instructions: str,
        continuation: str,
    ) -> models.ExecutionBranch:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            cwd = branch.cwd
            runtime = self._runtime_name(branch)
            adapter = self._adapter_for_branch(branch)
            parent_runtime = str(branch.context_snapshot.get("parent_runtime") or runtime)
            approved_claude_handoff = (
                branch.context_snapshot.get("runtime_handoff")
                if runtime == "claude"
                and (branch.context_snapshot.get("claude_handoff_delivery") or {}).get("approved")
                else None
            )

        thread_id: str | None = None
        strategy: str | None = None
        thread_fork_error: str | None = None
        if parent_thread_id and parent_runtime == runtime:
            try:
                result = await adapter.fork_thread(
                    thread_id=parent_thread_id,
                    cwd=cwd,
                    instructions=instructions,
                )
                candidate = str(result["thread"]["id"])
                if candidate == parent_thread_id:
                    raise CodexUnavailable("Codex returned the parent thread for a fork")
                thread_id = candidate
                strategy = "thread_fork"
            except Exception as exc:
                thread_fork_error = redact(str(exc))

        fallback_error: str | None = None
        if thread_id is None and not thread_fork_error:
            try:
                result = await adapter.start_thread(
                    cwd=cwd,
                    instructions=instructions,
                )
                thread_id = str(result["thread"]["id"])
                if approved_claude_handoff:
                    stage_claude_handoff(
                        self.settings.runtime_data_dir / "claude-runtime",
                        thread_id,
                        approved_claude_handoff,
                    )
                strategy = "new_thread"
            except Exception as exc:
                thread_id = None
                fallback_error = redact(str(exc))

        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            snapshot = dict(branch.context_snapshot or {})
            if thread_id is not None:
                snapshot["provisioning"] = {
                    "state": "ready",
                    "strategy": strategy,
                    "error": thread_fork_error,
                }
                branch.context_snapshot = snapshot
                branch.codex_thread_id = thread_id
                branch.status = "active"
                if thread_fork_error:
                    self._event(
                        session,
                        "branch",
                        branch.id,
                            f"{runtime}.fork_fallback",
                        {
                            "error": thread_fork_error,
                            "strategy": strategy,
                            "thread_id": thread_id,
                        },
                    )
                self._event(
                    session,
                    "branch",
                    branch.id,
                    f"{runtime}.fork_ready",
                    {"strategy": strategy, "thread_id": thread_id},
                )
                if continuation == "continue" and branch.parent_branch_id:
                    parent = self._branch(session, branch.parent_branch_id)
                    parent.status = "continued"
                    parent.continued_by_branch_id = branch.id
                    self._event(
                        session,
                        "branch",
                        parent.id,
                        "branch.continued",
                        {"continued_by_branch_id": branch.id},
                    )
            else:
                errors = [item for item in (thread_fork_error, fallback_error) if item]
                error = " | ".join(errors) or f"Unable to create a {runtime} session"
                snapshot["provisioning"] = {
                    "state": "failed",
                    "strategy": None,
                    "error": error,
                }
                branch.context_snapshot = snapshot
                branch.status = "blocked"
                self._event(
                    session,
                    "branch",
                    branch.id,
                    f"{runtime}.fork_failed",
                    {"error": error},
                )
            session.commit()
            return branch

    def _parent_fork_snapshot(
        self, session: Session, parent: models.ExecutionBranch
    ) -> dict[str, Any]:
        turn = session.scalar(
            select(models.CodexTurn)
            .where(
                models.CodexTurn.branch_id == parent.id,
                models.CodexTurn.status == "completed",
            )
            .order_by(models.CodexTurn.completed_at.desc())
            .limit(1)
        )
        if turn is None:
            return {}
        return {
            "source_branch_id": parent.id,
            "source_turn_id": turn.external_turn_id,
            "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
            "prompt": redact(turn.prompt or "")[-4_000:],
            "conclusion": redact(turn.final_answer or "")[-12_000:],
        }

    def _repair_parent_snapshot(
        self,
        session: Session,
        parent: models.ExecutionBranch,
        branch: models.ExecutionBranch,
    ) -> dict[str, Any]:
        marker = session.scalar(
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == parent.id,
                models.ConversationMessage.role == "tool",
                or_(
                    models.ConversationMessage.content.contains(branch.id),
                    models.ConversationMessage.content.contains(branch.name),
                    models.ConversationMessage.content.contains(branch.slug),
                ),
            )
            .order_by(models.ConversationMessage.created_at.asc())
            .limit(1)
        )
        if marker is not None and marker.turn_id:
            conclusion = session.scalar(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == parent.id,
                    models.ConversationMessage.turn_id == marker.turn_id,
                    models.ConversationMessage.role == "assistant",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            if conclusion is not None:
                return {
                    "source_branch_id": parent.id,
                    "source_turn_id": marker.turn_id,
                    "completed_at": conclusion.created_at.isoformat(),
                    "conclusion": redact(conclusion.content)[-12_000:],
                }
        return self._parent_fork_snapshot(session, parent)

    def _workspace_entry(
        self,
        *,
        program_code: str,
        work_human_id: str,
        branch_slug: str,
        repository_name: str | None,
        cwd: str,
    ) -> str:
        try:
            return self.workspace.ensure_workspace_entry(
                program_code=program_code,
                work_human_id=work_human_id,
                branch_slug=branch_slug,
                repository_name=repository_name,
                existing_cwd=cwd,
            )
        except WorkspaceError:
            return cwd

    async def start_session(
        self, branch_id: str, payload: schemas.SessionStart,
        *, execution: dict[str, Any] | None = None,
    ) -> models.SessionAttempt:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            self._expire_stale_sessions(session, now=now)
            branch = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    )
                )
            )
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.hidden:
                raise ConflictError("Restore this hidden Branch before starting a Session")
            if branch.mode == "capture":
                raise ConflictError("Capture-only branches cannot start a Session")
            if branch.status != "active":
                raise ConflictError(f"Branch is {branch.status}; it cannot start a Session")
            runtime = self._runtime_name(branch)
            active = session.scalar(
                select(models.SessionAttempt)
                .where(
                    models.SessionAttempt.branch_id == branch.id,
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > now,
                )
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            if active:
                return active
            previous = session.scalar(
                select(models.SessionAttempt)
                .where(models.SessionAttempt.branch_id == branch.id)
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            brief = self._build_branch_brief(
                session,
                branch,
                previous_handoff=previous.handoff if previous else None,
            )
            attempt = models.SessionAttempt(
                branch_id=branch.id,
                codex_thread_id=branch.codex_thread_id,
                runtime=runtime,
                execution_config=execution or self._execution_snapshot(branch),
                status="starting",
                lease_owner=payload.owner,
                lease_expires_at=now + timedelta(minutes=self.settings.lease_minutes),
                heartbeat_at=now,
                work_item_revision=branch.work_item.revision,
                brief=brief,
                started_at=now,
            )
            session.add(attempt)
            branch.work_item.lifecycle = "active"
            branch.work_item.flow = "running"
            self._event(
                session, "branch", branch.id, "session.starting", {"session_id": attempt.id}
            )
            session.commit()

        try:
            adapter = adapter_for_execution(
                self._adapter_for_runtime(runtime), attempt.execution_config
            )
            if branch.codex_thread_id and not branch.codex_thread_id.startswith("deferred:"):
                result = await adapter.resume_thread(
                    thread_id=branch.codex_thread_id,
                    cwd=branch.cwd,
                )
            else:
                result = await adapter.start_thread(
                    cwd=branch.cwd,
                    instructions=developer_instructions(brief),
                )
            thread_id = result["thread"]["id"]
            with self.session_factory() as session:
                stored_attempt = session.get(models.SessionAttempt, attempt.id)
                stored_branch = session.get(models.ExecutionBranch, branch.id)
                assert stored_attempt and stored_branch
                stored_attempt.codex_thread_id = thread_id
                stored_attempt.status = "active"
                stored_branch.codex_thread_id = thread_id
                self._event(
                    session,
                    "branch",
                    branch.id,
                    "session.started",
                    {"session_id": attempt.id, "thread_id": thread_id},
                )
                session.commit()
                attempt = stored_attempt
        except Exception as exc:
            with self.session_factory() as session:
                stored_attempt = session.get(models.SessionAttempt, attempt.id)
                assert stored_attempt
                stored_attempt.status = "error"
                stored_attempt.error = str(exc)
                stored_attempt.completed_at = datetime.now(UTC)
                session.flush()
                self._reconcile_work_activity(
                    session,
                    now=stored_attempt.completed_at,
                    work_ids={branch.work_item_id},
                )
                self._event(
                    session,
                    "branch",
                    branch.id,
                    "session.failed",
                    {"session_id": attempt.id, "error": str(exc)},
                )
                session.commit()
                attempt = stored_attempt
        await self.event_hub.publish(
            branch.id,
            {"type": "session.status", "session_id": attempt.id, "status": attempt.status},
        )
        return attempt

    def claim_terminal_session(self, branch_id: str, owner: str) -> models.SessionAttempt:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            self._expire_stale_sessions(session, now=now)
            branch = session.scalar(
                select(models.ExecutionBranch)
                .where(models.ExecutionBranch.id == branch_id)
                .options(
                    selectinload(models.ExecutionBranch.work_item).selectinload(
                        models.WorkItem.program
                    )
                )
            )
            if branch is None:
                raise NotFoundError("Branch not found")
            if branch.hidden:
                raise ConflictError("Restore this hidden Branch before opening Codex")
            if branch.mode == "capture":
                raise ConflictError("Capture-only branches cannot open Codex")
            if self._runtime_name(branch) == "pi":
                raise ConflictError(
                    "Pi Branches run through the Web Pi runtime, not a Codex terminal"
                )
            if branch.status != "active":
                raise ConflictError(f"Branch is {branch.status}; it cannot open Codex")
            self._interrupt_pending_web(
                session,
                branch.id,
                "Interrupted because the local CLI took priority",
            )
            active = session.scalar(
                select(models.SessionAttempt)
                .where(
                    models.SessionAttempt.branch_id == branch.id,
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > now,
                )
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            if active is not None:
                if active.lease_owner.startswith("terminal:"):
                    raise ConflictError(
                        f"Branch is already open in terminal session {active.id}"
                    )
                active.status = "completed"
                active.completed_at = now
                active.error = "Superseded by the managed terminal runtime"
            previous = session.scalar(
                select(models.SessionAttempt)
                .where(models.SessionAttempt.branch_id == branch.id)
                .order_by(models.SessionAttempt.created_at.desc())
                .limit(1)
            )
            brief = self._build_branch_brief(
                session,
                branch,
                previous_handoff=previous.handoff if previous else None,
            )
            thread_id = branch.codex_thread_id
            if thread_id and thread_id.startswith("deferred:"):
                thread_id = None
            attempt = models.SessionAttempt(
                branch_id=branch.id,
                codex_thread_id=thread_id,
                runtime=self._runtime_name(branch),
                execution_config=self._execution_snapshot(branch),
                status="active",
                lease_owner=owner,
                lease_expires_at=now + timedelta(minutes=self.settings.lease_minutes),
                heartbeat_at=now,
                work_item_revision=branch.work_item.revision,
                brief=brief,
                started_at=now,
            )
            session.add(attempt)
            branch.work_item.lifecycle = "active"
            branch.work_item.flow = "running"
            self._event(
                session,
                "branch",
                branch.id,
                "session.terminal_started",
                {"session_id": attempt.id, "owner": owner},
            )
            session.commit()
            return attempt

    def finish_terminal_session(
        self,
        branch_id: str,
        session_id: str,
        lease_id: str,
        return_code: int,
    ) -> models.SessionAttempt:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            attempt = session.get(models.SessionAttempt, session_id)
            branch = self._branch(session, branch_id)
            if attempt is None or attempt.branch_id != branch_id:
                raise NotFoundError("Session not found")
            if attempt.lease_id != lease_id:
                raise ConflictError("Session lease does not match")
            if attempt.status not in {"active", "starting", "expired"}:
                return attempt
            attempt.status = "completed" if return_code == 0 else "error"
            attempt.completed_at = now
            if return_code != 0:
                attempt.error = f"Codex terminal exited with status {return_code}"
            branch.head_sha = self.workspace.current_head(branch.cwd)
            session.flush()
            self._reconcile_work_activity(
                session,
                now=now,
                work_ids={branch.work_item_id},
            )
            self._event(
                session,
                "branch",
                branch.id,
                "session.terminal_exited",
                {"session_id": attempt.id, "return_code": return_code},
            )
            session.commit()
            return attempt

    def heartbeat_session(
        self,
        branch_id: str,
        session_id: str,
        payload: schemas.SessionHeartbeat,
    ) -> models.SessionAttempt:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            expired_changed = self._expire_stale_sessions(session, now=now, branch_id=branch_id)
            attempt = session.get(models.SessionAttempt, session_id)
            if attempt is None or attempt.branch_id != branch_id:
                raise NotFoundError("Session not found")
            if attempt.lease_id != payload.lease_id:
                raise ConflictError("Session lease does not match")
            if attempt.status not in {"starting", "active"}:
                if expired_changed:
                    session.commit()
                raise ConflictError(f"Session is not active: {attempt.status}")
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(minutes=self.settings.lease_minutes)
            session.commit()
            return attempt

    async def send_message(
        self,
        branch_id: str,
        payload: schemas.ConversationSend,
        *,
        origin: str = "web",
        metadata: dict[str, Any] | None = None,
        supersede_pending: bool = True,
    ) -> models.ConversationMessage:
        """Run one Web turn inline.

        This remains available for internal workflows that need the completed
        result before proceeding.  The HTTP Web composer uses
        :meth:`queue_web_message` so a slow model turn never holds its POST
        request open.
        """
        lock = self._web_turn_locks[branch_id]
        if lock.locked() or self._web_turn_task_active(branch_id):
            if not supersede_pending:
                raise ConflictError("The Branch is busy with another conversation turn")
            await self._supersede_pending_web_turn(branch_id)
        async with lock:
            frozen_execution = (metadata or {}).get("execution") if (
                origin == "goal" or (metadata or {}).get("scheduled_check_run_id")
                or (metadata or {}).get("claude_wakeup_id")
            ) else None
            attempt, web_owned_session = await self._ensure_web_attempt(
                branch_id, execution=frozen_execution,
            )
            try:
                message, prepared = await self._prepare_web_message(
                    branch_id,
                    attempt,
                    payload,
                    origin=origin,
                    metadata=metadata,
                )
            except ConflictError:
                if web_owned_session:
                    with suppress(ConflictError, NotFoundError):
                        await run_db(
                            self.finish_terminal_session,
                            branch_id, attempt.id, attempt.lease_id, 0,
                        )
                raise
            del message
            return await self._run_prepared_web_turn(prepared, web_owned_session)

    async def queue_web_message(
        self,
        branch_id: str,
        payload: schemas.ConversationSend,
        *,
        origin: str = "web",
        metadata: dict[str, Any] | None = None,
        supersede_pending: bool = True,
    ) -> models.ConversationMessage:
        """Persist and acknowledge a Web turn before invoking the runtime.

        A Web request is a durable command, not a long-poll transport for an
        entire model response.  This avoids proxy timeouts and makes an
        accepted prompt visible even if Codex later fails.
        """
        lock = self._web_turn_locks[branch_id]
        if lock.locked() or self._web_turn_task_active(branch_id):
            if not supersede_pending:
                raise ConflictError("The Branch is busy with another conversation turn")
            await self._supersede_pending_web_turn(branch_id)
        async with lock:
            if metadata and metadata.get("scheduled_check_run_id"):
                with self.session_factory() as session:
                    self._validate_scheduled_check(session, branch_id, metadata)
            if metadata and metadata.get("claude_wakeup_id"):
                with self.session_factory() as session:
                    self._validate_claude_wakeup(session, branch_id, metadata)
            frozen_execution = (metadata or {}).get("execution") if (
                origin == "goal" or (metadata or {}).get("scheduled_check_run_id")
                or (metadata or {}).get("claude_wakeup_id")
            ) else None
            attempt, web_owned_session = await self._ensure_web_attempt(
                branch_id, execution=frozen_execution,
            )
            try:
                message, prepared = await self._prepare_web_message(
                    branch_id,
                    attempt,
                    payload,
                    origin=origin,
                    metadata=metadata,
                )
            except ConflictError:
                if web_owned_session:
                    with suppress(ConflictError, NotFoundError):
                        await run_db(
                            self.finish_terminal_session,
                            branch_id, attempt.id, attempt.lease_id, 0,
                        )
                raise
            task = asyncio.create_task(
                self._run_queued_web_turn(prepared, web_owned_session),
                name=f"research-ops-web-turn:{branch_id}:{message.id}",
            )
            self._web_turn_tasks[branch_id] = task
            task.add_done_callback(
                lambda completed, target=branch_id: self._forget_web_turn_task(
                    target, completed
                )
            )
            return message

    @staticmethod
    def _validate_claude_wakeup(
        session: Session, branch_id: str, metadata: dict[str, Any]
    ) -> models.ClaudeWakeup:
        row = session.get(models.ClaudeWakeup, metadata["claude_wakeup_id"])
        branch = session.get(models.ExecutionBranch, branch_id)
        if (
            not row or row.branch_id != branch_id or row.status != "claimed" or row.message_id
            or not branch or branch.hidden or branch.status != "active" or branch.mode == "capture"
            or branch.runtime != "claude" or branch.codex_thread_id != row.thread_id
        ):
            raise ConflictError("This Claude wakeup is no longer due")
        return row

    @staticmethod
    def _validate_scheduled_check(
        session: Session, branch_id: str, metadata: dict[str, Any]
    ) -> models.ScheduledCheckRun:
        run = session.get(models.ScheduledCheckRun, metadata["scheduled_check_run_id"])
        check = session.get(models.ScheduledCheck, run.check_id) if run else None
        if (
            run is None or check is None or check.branch_id != branch_id
            or check.status != "active" or run.status != "claimed"
            or check.active_run_id != run.id or check.revision != run.revision
            or (check.stop_at and _as_utc(check.stop_at) <= datetime.now(UTC))
        ):
            raise ConflictError("This scheduled check is no longer due")
        branch = session.get(models.ExecutionBranch, branch_id)
        monitor = session.get(models.NativeGoalMonitor, branch_id)
        if monitor and branch and monitor.thread_id == branch.codex_thread_id and (
            monitor.goal_snapshot.get("status") == "active" or monitor.next_retry_at is not None
        ):
            raise ConflictError("Waiting for the active Goal to stop or pause")
        if session.scalar(select(models.GoalRun.id).where(
            models.GoalRun.branch_id == branch_id, models.GoalRun.status == "active"
        ).limit(1)):
            raise ConflictError("Waiting for the active background Goal to stop or pause")
        return run

    async def _ensure_web_attempt(
        self, branch_id: str, *, execution: dict[str, Any] | None = None
    ) -> tuple[models.SessionAttempt, bool]:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            execution = execution or self._web_execution_snapshot(session, branch)
            state = self._conversation_state(session, branch)
            if not state["can_send"]:
                raise ConflictError(state["reason"] or "The Branch cannot accept a Web turn")
            attempt = self._active_attempt(session, branch.id)

        web_owned_session = attempt is None
        if attempt is None:
            attempt = await self.start_session(
                branch_id, schemas.SessionStart(owner="web"), execution=execution,
            )
        if attempt.status == "active" and attempt.codex_thread_id:
            return attempt, web_owned_session
        if web_owned_session:
            with suppress(ConflictError, NotFoundError):
                self.finish_terminal_session(branch_id, attempt.id, attempt.lease_id, 1)
        raise ConflictError("Unable to prepare a Web turn for this Branch")

    async def runtime_control(
        self,
        branch_id: str,
        command: str,
        *,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Send an explicit control command to a runtime that supports it."""
        normalized = str(command or "").strip().casefold().replace("-", "_")
        if normalized not in {"steer", "follow_up", "abort"}:
            raise ConflictError(f"Unsupported runtime command: {command}")
        if normalized != "abort" and not str(content or "").strip():
            raise ConflictError(f"The {normalized} command requires content")
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            attempt = self._active_attempt(session, branch_id)
            if attempt is None or not attempt.codex_thread_id:
                raise ConflictError("No active runtime session is attached to this Branch")
            thread_id = attempt.codex_thread_id
            cwd = branch.cwd
            runtime = self._runtime_name(branch)
            adapter = self._adapter_for_runtime(runtime)
            if normalized == "steer":
                method = getattr(adapter, "steer_message", None)
            elif normalized == "follow_up":
                method = getattr(adapter, "follow_up", None)
            else:
                method = getattr(adapter, "abort", None)
            if not callable(method):
                raise ConflictError(
                    f"The {runtime} runtime does not support {normalized}"
                )

        if normalized == "abort":
            result = await method(thread_id=thread_id, cwd=cwd)
        else:
            result = await method(thread_id=thread_id, content=str(content), cwd=cwd)

        message: models.ConversationMessage | None = None
        if normalized != "abort":
            with self.session_factory() as session:
                message = models.ConversationMessage(
                    branch_id=branch_id,
                    session_id=attempt.id,
                    role="user",
                    kind="runtime_control",
                    content=redact(str(content)),
                    metadata_json={
                        "origin": "runtime-control",
                        "send_status": "submitted",
                        "runtime": runtime,
                        "delivery": normalized,
                    },
                )
                session.add(message)
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "runtime.command_submitted",
                    {"command": normalized, "message_id": message.id},
                )
                session.commit()
            await self.event_hub.publish(
                branch_id,
                {
                    "type": "message.created",
                    "message": schemas.ConversationMessageRead.model_validate(
                        message
                    ).model_dump(mode="json"),
                },
            )
        else:
            with self.session_factory() as session:
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "runtime.command_submitted",
                    {"command": normalized},
                )
                session.commit()
        await self.event_hub.publish(
            branch_id,
            {
                "type": "runtime.command_submitted",
                "command": normalized,
                "runtime": runtime,
                "result": result,
            },
        )
        return {
            "accepted": True,
            "command": normalized,
            "runtime": runtime,
            "message_id": message.id if message else None,
        }

    async def _prepare_web_message(
        self,
        branch_id: str,
        attempt: models.SessionAttempt,
        payload: schemas.ConversationSend,
        *,
        origin: str = "web",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[models.ConversationMessage, _PreparedWebTurn]:
        message_metadata = {"send_status": "pending", "origin": origin}
        if metadata:
            message_metadata.update(metadata)
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            check_run = self._validate_scheduled_check(session, branch_id, message_metadata) if (
                message_metadata.get("scheduled_check_run_id")
            ) else None
            wakeup = self._validate_claude_wakeup(session, branch_id, message_metadata) if (
                message_metadata.get("claude_wakeup_id")
            ) else None
            state = self._conversation_state(session, branch)
            if not state["can_send"]:
                raise ConflictError(state["reason"] or "The Branch cannot accept a Web turn")
            stored_attempt = session.get(models.SessionAttempt, attempt.id)
            if stored_attempt is None or not stored_attempt.codex_thread_id:
                raise ConflictError("Start or resume the branch Session before sending a message")
            if self._runtime_name(branch) in {"codex", "claude"}:
                message_metadata["execution"] = (
                    message_metadata.get("execution")
                    if origin == "goal" or check_run or wakeup else None
                ) or self._web_execution_snapshot(session, branch)
                if stored_attempt.lease_owner == "web":
                    stored_attempt.execution_config = dict(message_metadata["execution"])
            message = models.ConversationMessage(
                branch_id=branch.id,
                session_id=stored_attempt.id,
                role="user",
                content=redact(payload.content),
                metadata_json=message_metadata,
            )
            session.add(message)
            session.flush()
            if check_run:
                check_run.message_id = message.id
                check_run.status = "running"
                check = session.get(models.ScheduledCheck, check_run.check_id)
                assert check
                check.phase = "checking"
            if wakeup:
                wakeup.message_id, wakeup.status, wakeup.reason = message.id, "running", None
            elif origin == "web" and not check_run:
                session.execute(update(models.ClaudeWakeup).where(
                    models.ClaudeWakeup.branch_id == branch.id,
                    models.ClaudeWakeup.status.in_(("awaiting_turn", "scheduled", "claimed")),
                ).values(
                    status="cancelled", reason="Superseded by a new user message",
                    completed_at=datetime.now(UTC),
                ))
            self._event(
                session,
                "branch",
                branch.id,
                "conversation.user_message",
                {"message_id": message.id},
            )
            session.commit()
            prepared = _PreparedWebTurn(
                branch_id=branch_id,
                attempt_id=stored_attempt.id,
                attempt_lease_id=stored_attempt.lease_id,
                message_id=message.id,
                message_metadata=message_metadata,
                thread_id=stored_attempt.codex_thread_id,
                cwd=branch.cwd,
                work_human_id=branch.work_item.human_id,
                brief=stored_attempt.brief,
                content=payload.content,
            )
            conversation_state = self._conversation_state(session, branch)

        await self.event_hub.publish(
            branch_id,
            {
                "type": "message.created",
                "message": schemas.ConversationMessageRead.model_validate(message).model_dump(
                    mode="json"
                ),
            },
        )
        await self.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": conversation_state,
            },
        )
        return message, prepared

    async def _run_queued_web_turn(
        self, prepared: _PreparedWebTurn, web_owned_session: bool
    ) -> None:
        """Own the Branch lock for the lifetime of one queued runtime turn."""
        lock = self._web_turn_locks[prepared.branch_id]
        try:
            async with lock:
                await self._run_prepared_web_turn(prepared, web_owned_session)
        except asyncio.CancelledError:
            await self._set_web_message_status(
                prepared.branch_id,
                prepared.message_id,
                "interrupted",
                "Web turn cancelled before completion",
            )
            raise
        except Exception as exc:
            # _run_prepared_web_turn persists known Codex errors itself.  This
            # final guard also turns unexpected runner failures into a visible,
            # retryable failed query instead of an unobserved task exception.
            await self._set_web_message_status(
                prepared.branch_id,
                prepared.message_id,
                "failed",
                redact(str(exc) or type(exc).__name__),
            )
            logger.warning(
                "Queued Web turn failed for branch %s: %s",
                prepared.branch_id,
                redact(str(exc) or type(exc).__name__),
            )

    async def _run_prepared_web_turn(
        self, prepared: _PreparedWebTurn, web_owned_session: bool
    ) -> models.ConversationMessage:
        return_code = 1
        try:
            capacity_retry = 0
            transport_retry = 0
            while True:
                try:
                    # This gates only Research Ops Web turns. It prevents a
                    # collection of Branches from stampeding one account/model
                    # pool, while preserving the user's independent local CLI.
                    await self._acquire_codex_capacity(prepared)
                    try:
                        result = await self._dispatch_web_turn_with_heartbeat(prepared)
                    finally:
                        if self._codex_turn_semaphore is not None:
                            self._codex_turn_semaphore.release()
                    break
                except CodexUnavailable as exc:
                    if self._is_model_capacity_error(exc):
                        if capacity_retry >= self.settings.codex_capacity_retry_attempts:
                            raise
                        capacity_retry += 1
                        delay = self._capacity_retry_delay(capacity_retry)
                        await self._record_capacity_retry(
                            prepared,
                            attempt=capacity_retry,
                            delay_seconds=delay,
                        )
                        await self._wait_for_pending_web_turn(prepared, delay)
                        continue
                    if (
                        self._is_retryable_transport_error(exc)
                        and not self._web_turn_started_execution(prepared)
                        and transport_retry < self.settings.codex_transport_retry_attempts
                    ):
                        transport_retry += 1
                        delay = self._transport_retry_delay(transport_retry)
                        await self._record_transport_retry(
                            prepared,
                            attempt=transport_retry,
                            delay_seconds=delay,
                        )
                        await self._wait_for_pending_web_turn(prepared, delay)
                        continue
                    raise
            message = await run_db(self._save_web_turn_result, prepared, result)
            await self._publish_web_message_update(
                prepared.branch_id, prepared.message_id
            )
            return_code = 0
            return message
        except ConflictError:
            raise
        except CodexUnavailable as exc:
            safe_error = redact(str(exc))
            await self._set_web_message_status(
                prepared.branch_id, prepared.message_id, "failed", safe_error
            )
            raise CodexUnavailable(safe_error) from exc
        except asyncio.CancelledError:
            await self._set_web_message_status(
                prepared.branch_id,
                prepared.message_id,
                "interrupted",
                "Web request cancelled",
            )
            raise
        finally:
            if web_owned_session:
                with suppress(ConflictError, NotFoundError):
                    finished = await run_db(
                        self.finish_terminal_session,
                        prepared.branch_id,
                        prepared.attempt_id,
                        prepared.attempt_lease_id,
                        return_code,
                    )
                    await self.event_hub.publish(
                        prepared.branch_id,
                        {
                            "type": "session.status",
                            "session_id": finished.id,
                            "status": finished.status,
                        },
                    )
            await self._publish_web_message_update(
                prepared.branch_id, prepared.message_id
            )

    def _save_web_turn_result(
        self, prepared: _PreparedWebTurn, result: dict[str, Any]
    ) -> models.ConversationMessage:
        turn_id = result["turn"]["id"]
        returned_thread_id = result.get("thread", {}).get("id", prepared.thread_id)
        with self.session_factory() as session:
            stored = session.get(models.ConversationMessage, prepared.message_id)
            attempt = session.get(models.SessionAttempt, prepared.attempt_id)
            assert stored and attempt
            if stored.metadata_json.get("send_status") == "interrupted":
                raise ConflictError("Web turn was interrupted")
            stored.turn_id = turn_id
            stored.metadata_json = {**prepared.message_metadata, "send_status": "submitted"}
            attempt.last_turn_id = turn_id
            if returned_thread_id != prepared.thread_id:
                branch = session.get(models.ExecutionBranch, prepared.branch_id)
                assert branch
                branch.codex_thread_id = returned_thread_id
                attempt.codex_thread_id = returned_thread_id
            session.commit()
            return stored

    async def _dispatch_web_turn_with_heartbeat(
        self, prepared: _PreparedWebTurn
    ) -> dict[str, Any]:
        if not await run_db(
            self._heartbeat_web_attempt, prepared.branch_id, prepared.attempt_id
        ):
            raise ConflictError("The Web Session is no longer active")
        task = asyncio.create_task(
            self._dispatch_web_turn(
                branch_id=prepared.branch_id,
                attempt_id=prepared.attempt_id,
                thread_id=prepared.thread_id,
                cwd=prepared.cwd,
                work_human_id=prepared.work_human_id,
                brief=prepared.brief,
                content=prepared.content,
                execution=prepared.message_metadata.get("execution"),
            )
        )
        loop = asyncio.get_running_loop()
        heartbeat_interval = max(
            0.05,
            min(
                self.settings.web_turn_heartbeat_seconds,
                self.settings.lease_minutes * 20.0,
            ),
        )
        next_heartbeat = loop.time() + heartbeat_interval
        try:
            while not task.done():
                timeout = max(0.01, min(0.5, next_heartbeat - loop.time()))
                await asyncio.wait({task}, timeout=timeout)
                if task.done():
                    break
                try:
                    status = await run_db(self._message_send_status, prepared.message_id)
                    if status == "interrupted":
                        raise ConflictError("Web turn was interrupted")
                    if loop.time() >= next_heartbeat:
                        if not await run_db(
                            self._heartbeat_web_attempt, prepared.branch_id, prepared.attempt_id
                        ):
                            raise ConflictError("The Web Session ended before the turn completed")
                        next_heartbeat = loop.time() + heartbeat_interval
                except OperationalError as exc:
                    if not is_sqlite_busy(exc):
                        raise
                    # A running child is stronger liveness evidence than a
                    # temporarily unwritable lease. Retry the lease next tick.
                    logger.warning("Web heartbeat delayed by SQLite for %s", prepared.branch_id)
                    next_heartbeat = loop.time() + heartbeat_interval
            return await task
        finally:
            if not task.done():
                task.cancel()
            # Retrieve exceptions even if heartbeat/control polling failed.
            await asyncio.gather(task, return_exceptions=True)

    async def _acquire_codex_capacity(self, prepared: _PreparedWebTurn) -> None:
        """Optionally wait for a deployment semaphore without trapping a Branch lock.

        With the default unlimited setting, this only validates the durable
        pending state and lets the provider handle admission. When an operator
        explicitly configures a positive limit, polling keeps interruption and
        supersession responsive while a turn waits for the semaphore.
        """
        loop = asyncio.get_running_loop()
        next_heartbeat = loop.time()
        if self._codex_turn_semaphore is None:
            await self._ensure_pending_web_turn(prepared)
            if not await run_db(
                self._heartbeat_web_attempt, prepared.branch_id, prepared.attempt_id
            ):
                raise ConflictError("The Web Session is no longer active")
            return
        while True:
            await self._ensure_pending_web_turn(prepared)
            if loop.time() >= next_heartbeat:
                if not await run_db(
                    self._heartbeat_web_attempt, prepared.branch_id, prepared.attempt_id
                ):
                    raise ConflictError("The Web Session is no longer active")
                next_heartbeat = loop.time() + self._web_heartbeat_interval()
            try:
                await asyncio.wait_for(self._codex_turn_semaphore.acquire(), timeout=0.5)
                return
            except TimeoutError:
                continue

    async def _wait_for_pending_web_turn(
        self, prepared: _PreparedWebTurn, delay_seconds: float
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay_seconds
        next_heartbeat = loop.time()
        while True:
            await self._ensure_pending_web_turn(prepared)
            if loop.time() >= next_heartbeat:
                if not await run_db(
                    self._heartbeat_web_attempt, prepared.branch_id, prepared.attempt_id
                ):
                    raise ConflictError("The Web Session is no longer active")
                next_heartbeat = loop.time() + self._web_heartbeat_interval()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.5, remaining))

    async def _ensure_pending_web_turn(self, prepared: _PreparedWebTurn) -> None:
        if await run_db(self._message_send_status, prepared.message_id) != "pending":
            raise ConflictError("Web turn was interrupted")

    def _web_heartbeat_interval(self) -> float:
        return max(
            0.05,
            min(
                self.settings.web_turn_heartbeat_seconds,
                self.settings.lease_minutes * 20.0,
            ),
        )

    @staticmethod
    def _is_model_capacity_error(error: Exception) -> bool:
        detail = str(error).casefold()
        # The gateway uses both messages for the same admission failure:
        # model-specific saturation and the account-level concurrent request
        # cap. Both are safe to wait and retry before marking the query failed.
        return any(
            signal in detail
            for signal in (
                "selected model is at capacity",
                "concurrency limit exceeded",
            )
        )

    @staticmethod
    def _is_retryable_transport_error(error: Exception) -> bool:
        """Return true only for transient gateway failures from the CLI stream."""
        detail = str(error).casefold()
        return any(
            signal in detail
            for signal in (
                "stream disconnected",
                "websocket closed",
                "connection reset",
                "connection refused",
                "concurrency limit exceeded",
            )
        )

    def _web_turn_started_execution(self, prepared: _PreparedWebTurn) -> bool:
        """Whether retrying this prompt could repeat an already-started action.

        A CLI transport failure can arrive after Codex has started a command but
        before the process reports its final result. ``item/started`` marks the
        pending user message durably, and completed items provide a fallback for
        older events. In either case the recovery is intentionally manual.
        """
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, prepared.message_id)
            if message is None or message.branch_id != prepared.branch_id:
                return True
            if message.metadata_json.get("runtime_execution_started"):
                return True
            if message.turn_id:
                observed = session.scalar(
                    select(models.ConversationMessage.id)
                    .where(
                        models.ConversationMessage.branch_id == prepared.branch_id,
                        models.ConversationMessage.id != prepared.message_id,
                        models.ConversationMessage.turn_id == message.turn_id,
                        models.ConversationMessage.role.in_(("assistant", "tool")),
                    )
                    .limit(1)
                )
                return observed is not None
            # A missing turn id generally means the request died before Codex
            # accepted it. Remain conservative if the Branch received output
            # after the prompt through a less recent/legacy event path.
            observed = session.scalar(
                select(models.ConversationMessage.id)
                .where(
                    models.ConversationMessage.branch_id == prepared.branch_id,
                    models.ConversationMessage.id != prepared.message_id,
                    models.ConversationMessage.role.in_(("assistant", "tool")),
                    models.ConversationMessage.created_at >= message.created_at,
                )
                .limit(1)
            )
            return observed is not None

    def _capacity_retry_delay(self, attempt: int) -> float:
        initial = self.settings.codex_capacity_retry_initial_seconds
        maximum = self.settings.codex_capacity_retry_max_seconds
        return min(initial * (2 ** max(0, attempt - 1)), maximum)

    def _transport_retry_delay(self, attempt: int) -> float:
        initial = self.settings.codex_transport_retry_initial_seconds
        maximum = self.settings.codex_transport_retry_max_seconds
        return min(initial * (2 ** max(0, attempt - 1)), maximum)

    async def _record_capacity_retry(
        self,
        prepared: _PreparedWebTurn,
        *,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        next_retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, prepared.message_id)
            if message is None or message.branch_id != prepared.branch_id:
                return
            if message.metadata_json.get("send_status") != "pending":
                raise ConflictError("Web turn was interrupted")
            message.metadata_json = {
                **message.metadata_json,
                "capacity_retry": {
                    "attempt": attempt,
                    "max_attempts": self.settings.codex_capacity_retry_attempts,
                    "next_retry_at": next_retry_at.isoformat(),
                },
            }
            self._event(
                session,
                "branch",
                prepared.branch_id,
                "conversation.capacity_retry",
                {
                    "message_id": prepared.message_id,
                    "attempt": attempt,
                    "max_attempts": self.settings.codex_capacity_retry_attempts,
                    "next_retry_at": next_retry_at.isoformat(),
                },
            )
            session.commit()
        await self._publish_web_message_update(
            prepared.branch_id, prepared.message_id
        )

    async def _record_transport_retry(
        self,
        prepared: _PreparedWebTurn,
        *,
        attempt: int,
        delay_seconds: float,
    ) -> None:
        next_retry_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, prepared.message_id)
            if message is None or message.branch_id != prepared.branch_id:
                return
            if message.metadata_json.get("send_status") != "pending":
                raise ConflictError("Web turn was interrupted")
            message.metadata_json = {
                **message.metadata_json,
                "transport_retry": {
                    "attempt": attempt,
                    "max_attempts": self.settings.codex_transport_retry_attempts,
                    "next_retry_at": next_retry_at.isoformat(),
                },
            }
            self._event(
                session,
                "branch",
                prepared.branch_id,
                "conversation.transport_retry",
                {
                    "message_id": prepared.message_id,
                    "attempt": attempt,
                    "max_attempts": self.settings.codex_transport_retry_attempts,
                    "next_retry_at": next_retry_at.isoformat(),
                },
            )
            session.commit()
        await self._publish_web_message_update(
            prepared.branch_id, prepared.message_id
        )

    async def _set_web_message_status(
        self,
        branch_id: str,
        message_id: str,
        send_status: str,
        error: str,
    ) -> None:
        await run_db(self._save_web_message_status, branch_id, message_id, send_status, error)
        await self._publish_web_message_update(branch_id, message_id)

    def _save_web_message_status(
        self, branch_id: str, message_id: str, send_status: str, error: str
    ) -> None:
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, message_id)
            if message is None or message.branch_id != branch_id:
                return
            if message.metadata_json.get("send_status") == "pending":
                message.metadata_json = {
                    **message.metadata_json,
                    "send_status": send_status,
                    "error": redact(error),
                }
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "conversation.send_failed"
                    if send_status == "failed"
                    else "conversation.interrupted",
                    {"error": redact(error), "message_id": message_id},
                )
                if send_status == "failed":
                    self._enqueue_failure_notification(session, message)
                session.commit()
    async def _publish_web_message_update(self, branch_id: str, message_id: str) -> None:
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, message_id)
            branch = session.get(models.ExecutionBranch, branch_id)
            if message is None or branch is None:
                return
            serialized = schemas.ConversationMessageRead.model_validate(message).model_dump(
                mode="json"
            )
            conversation_state = self._conversation_state(session, branch)
        await self.event_hub.publish(
            branch_id, {"type": "message.updated", "message": serialized}
        )
        await self.event_hub.publish(
            branch_id,
            {"type": "conversation.state", "conversation_state": conversation_state},
        )

    def _web_turn_task_active(self, branch_id: str) -> bool:
        task = self._web_turn_tasks.get(branch_id)
        return task is not None and not task.done()

    def _forget_web_turn_task(self, branch_id: str, task: asyncio.Task[None]) -> None:
        if self._web_turn_tasks.get(branch_id) is task:
            self._web_turn_tasks.pop(branch_id, None)

    async def _supersede_pending_web_turn(self, branch_id: str) -> None:
        """Let a newer Web prompt replace the currently running Web turn."""
        serialized: dict[str, Any] | None = None
        conversation_state: dict[str, Any] | None = None
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            pending = self._pending_web_message(session, branch_id)
            if pending is not None:
                reason = "Interrupted by a newer Web message"
                pending.metadata_json = {
                    **pending.metadata_json,
                    "send_status": "interrupted",
                    "error": reason,
                }
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "conversation.interrupted",
                    {"message_id": pending.id, "reason": reason},
                )
                attempt = (
                    session.get(models.SessionAttempt, pending.session_id)
                    if pending.session_id
                    else None
                )
                if attempt is not None and attempt.status in {"starting", "active"}:
                    attempt.status = "completed"
                    attempt.completed_at = datetime.now(UTC)
                    attempt.error = reason
                    self._event(
                        session,
                        "branch",
                        branch_id,
                        "session.interrupted",
                        {"session_id": attempt.id, "reason": reason},
                    )
                session.flush()
                serialized = schemas.ConversationMessageRead.model_validate(
                    pending
                ).model_dump(mode="json")
                conversation_state = self._conversation_state(session, branch)
                session.commit()

        if serialized is not None:
            await self.event_hub.publish(
                branch_id, {"type": "message.updated", "message": serialized}
            )
        if conversation_state is not None:
            await self.event_hub.publish(
                branch_id,
                {
                    "type": "conversation.state",
                    "conversation_state": conversation_state,
                },
            )

    def _heartbeat_web_attempt(self, branch_id: str, attempt_id: str) -> bool:
        """Renew the lease while an in-process Web turn proves the Session is alive."""
        now = datetime.now(UTC)
        with self.session_factory() as session:
            attempt = session.get(models.SessionAttempt, attempt_id)
            if attempt is None or attempt.branch_id != branch_id:
                return False
            if attempt.status not in {"starting", "active", "expired"}:
                return False
            recovered = attempt.status == "expired"
            attempt.status = "active"
            attempt.completed_at = None
            attempt.heartbeat_at = now
            attempt.lease_expires_at = now + timedelta(minutes=self.settings.lease_minutes)
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch and branch.work_item.flow == "actionable":
                branch.work_item.flow = "running"
            if recovered:
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "session.web_recovered",
                    {"session_id": attempt.id},
                )
            session.commit()
            return True

    async def _dispatch_web_turn(
        self,
        *,
        branch_id: str,
        attempt_id: str,
        thread_id: str,
        cwd: str,
        work_human_id: str,
        brief: dict[str, Any],
        content: str,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = {
            "research_ops_work_id": work_human_id,
            "research_ops_branch_id": branch_id,
        }
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            runtime = self._runtime_name(branch)
            if runtime == "claude":
                selected = execution or self._execution_snapshot(branch)
                metadata.update({
                    "research_ops_model": selected["model"],
                    "research_ops_reasoning_effort": selected["reasoning_effort"],
                    "research_ops_permission_mode": selected.get("permission_mode")
                    or self.settings.claude_permission_mode,
                })
        adapter = self._adapter_for_runtime(runtime)
        if runtime == "codex":
            adapter = adapter_for_execution(adapter, execution or self.identities.snapshot())
        try:
            return await adapter.send_message(
                thread_id=thread_id,
                content=content,
                cwd=cwd,
                metadata=metadata,
            )
        except CodexUnavailable as exc:
            if "thread not found" not in str(exc).lower():
                raise
            await adapter.resume_thread(thread_id=thread_id, cwd=cwd)
            return await adapter.send_message(
                thread_id=thread_id,
                content=content,
                cwd=cwd,
                metadata=metadata,
            )

    def reconcile_web_turn_priority(self) -> list[str]:
        """Interrupt Web turns when a different prompt appears in the local rollout."""
        interrupted: list[str] = []
        with self.session_factory() as session:
            changed = self._expire_stale_sessions(session)
            stale_local = self._reconcile_stale_local_turns(session)
            pending = list(
                session.scalars(
                    select(models.ConversationMessage)
                    .where(models.ConversationMessage.role == "user")
                    .order_by(models.ConversationMessage.created_at.desc())
                )
            )
            for message in pending:
                if message.metadata_json.get("send_status") != "pending":
                    continue
                prompts = self._pending_prompts(session, message.branch_id)
                if any(
                    not self._prompt_belongs_to_web(prompt, message) for prompt in prompts
                ):
                    self._interrupt_pending_web(
                        session,
                        message.branch_id,
                        "Interrupted because a local CLI turn started",
                    )
                    interrupted.append(message.branch_id)
            orphaned = self._reconcile_orphaned_web_turns(session)
            interrupted.extend(orphaned)
            interrupted.extend(stale_local)
            if changed or interrupted:
                session.commit()
        return list(dict.fromkeys(interrupted))

    def _conversation_state(
        self, session: Session, branch: models.ExecutionBranch
    ) -> dict[str, Any]:
        pending_web = self._pending_web_message(session, branch.id)
        prompts = self._pending_prompts(session, branch.id)
        local_turn_active = (
            bool(prompts)
            if pending_web is None
            else any(
                not self._prompt_belongs_to_web(prompt, pending_web) for prompt in prompts
            )
        )
        latest_web = self._latest_web_message(session, branch.id)
        raw_status = (
            str(latest_web.metadata_json.get("send_status")) if latest_web else "idle"
        )
        status_map = {
            "pending": "running",
            "submitted": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }
        web_turn_status = status_map.get(raw_status, "idle")
        reason: str | None = None
        if branch.hidden:
            reason = "Restore this hidden Branch before continuing"
        elif branch.mode == "capture":
            reason = "Capture-only Branches cannot run Web turns"
        elif branch.parent_branch_id and not branch.codex_thread_id:
            reason = "This Branch needs its Codex fork repaired"
        elif branch.status == "continued":
            reason = "This Branch continues in its child Branch"
        elif branch.status == "provisioning":
            reason = "Wait for this Branch to finish provisioning"
        elif branch.status == "blocked":
            reason = "This Branch needs its Codex fork repaired"
        elif branch.status in {"review", "parked", "abandoned", "expired"}:
            reason = f"This Branch is {branch.status}"
        elif pending_web is not None:
            reason = "Wait for the current Web turn to finish"
        elif local_turn_active:
            reason = "Wait for the current local CLI turn to finish"
        return {
            "can_send": reason is None,
            "can_interrupt": pending_web is not None,
            "web_turn_status": web_turn_status,
            "local_turn_active": local_turn_active,
            "reason": reason,
        }

    def _active_attempt(
        self, session: Session, branch_id: str
    ) -> models.SessionAttempt | None:
        return session.scalar(
            select(models.SessionAttempt)
            .where(
                models.SessionAttempt.branch_id == branch_id,
                models.SessionAttempt.status == "active",
                models.SessionAttempt.lease_expires_at > datetime.now(UTC),
            )
            .order_by(models.SessionAttempt.created_at.desc())
            .limit(1)
        )

    def _pending_prompts(self, session: Session, branch_id: str) -> list[str]:
        row = session.execute(
            select(models.CodexSyncCursor.pending_json, models.CodexThreadBinding.thread_id)
            .join(
                models.CodexThreadBinding,
                models.CodexThreadBinding.id == models.CodexSyncCursor.thread_binding_id,
            )
            .where(
                models.CodexThreadBinding.branch_id == branch_id,
                models.CodexThreadBinding.status == "current",
            )
            .order_by(models.CodexThreadBinding.bound_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return []
        pending, thread_id = row
        if not isinstance(pending, dict):
            return []
        if not self._thread_has_live_runtime(session, branch_id, thread_id):
            return []
        # A rollout can contain several historical ``task_started`` records when
        # a terminal turn was interrupted or the process was killed before the
        # old parser observed its closing event.  Only the newest pending turn
        # can be occupying the Codex thread; treating every historical prompt as
        # live makes a Web turn look like a competing local turn forever.
        latest_item: dict[str, Any] | None = None
        latest_index = -1
        latest_offset = -1
        for index, item in enumerate(pending.values()):
            if not isinstance(item, dict):
                continue
            raw_offset = item.get("start_offset")
            offset = raw_offset if isinstance(raw_offset, int) else -1
            if offset > latest_offset or (offset == latest_offset and index > latest_index):
                latest_item = item
                latest_offset = offset
                latest_index = index
        if latest_item is None or not latest_item.get("prompt"):
            return []
        return [str(latest_item["prompt"]).strip()]

    def _thread_has_live_runtime(
        self, session: Session, branch_id: str, thread_id: str
    ) -> bool:
        now = datetime.now(UTC)
        active_owner = session.scalar(
            select(models.SessionAttempt.lease_owner)
            .where(
                models.SessionAttempt.branch_id == branch_id,
                models.SessionAttempt.codex_thread_id == thread_id,
                models.SessionAttempt.status.in_(["starting", "active"]),
                models.SessionAttempt.lease_expires_at > now,
            )
            .limit(1)
        )
        # A Web turn owns a temporary Session lease too, but it is not a local
        # terminal. Treating it as one locks the composer after an interrupted
        # Web turn whenever a stale JSONL cursor remains.
        if active_owner is not None and active_owner != "web":
            return True
        live_window_pid = session.scalar(
            select(models.CodexWindow.pid)
            .where(
                models.CodexWindow.thread_id == thread_id,
                models.CodexWindow.live.is_(True),
                models.CodexWindow.pid.is_not(None),
            )
            .limit(1)
        )
        return (
            live_window_pid is not None
            and self._is_live_codex_window_process(live_window_pid)
        )

    def _reconcile_finished_web_sessions(
        self, session: Session, *, branch_id: str | None = None
    ) -> list[str]:
        """Close orphaned Web leases once their durable message is terminal.

        Web Sessions normally finish in the background runner. A service
        restart or interrupted runner can leave an ``active`` lease behind,
        which should never be presented as a local CLI conversation.
        """
        now = datetime.now(UTC)
        query = select(models.SessionAttempt).where(
            models.SessionAttempt.lease_owner == "web",
            models.SessionAttempt.status.in_(["starting", "active"]),
        )
        if branch_id is not None:
            query = query.where(models.SessionAttempt.branch_id == branch_id)
        completed: list[models.SessionAttempt] = []
        for attempt in session.scalars(query):
            latest = session.scalar(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == attempt.branch_id,
                    models.ConversationMessage.session_id == attempt.id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            if latest is None or not self._is_conversation_origin(latest):
                continue
            send_status = str(latest.metadata_json.get("send_status") or "")
            if send_status == "pending":
                continue
            attempt.status = "completed"
            attempt.completed_at = now
            if send_status in {"failed", "interrupted"}:
                attempt.error = str(latest.metadata_json.get("error") or send_status)
            self._event(
                session,
                "branch",
                attempt.branch_id,
                "session.web_reconciled",
                {"session_id": attempt.id, "send_status": send_status},
            )
            completed.append(attempt)
        if not completed:
            return []
        self._reconcile_work_activity(
            session,
            now=now,
            work_ids={attempt.branch.work_item_id for attempt in completed},
        )
        return list(dict.fromkeys(attempt.branch_id for attempt in completed))

    @staticmethod
    def _is_live_codex_window_process(pid: int) -> bool:
        """Validate cached window presence against the live Linux process table.

        ``CodexWindow.live`` can temporarily mean only that a managed Session
        lease was active during the last 10-second registry scan.  After a Web
        turn is interrupted that cached bit must not turn its leftover JSONL
        cursor into a fake local-CLI lock.
        """
        try:
            arguments = [
                item.decode(errors="replace")
                for item in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except (FileNotFoundError, OSError, PermissionError):
            return False
        if not arguments or Path(arguments[0]).name != "codex":
            return False
        return not any(
            item in {"app-server", "exec", "exec-server", "mcp-server", "remote-control"}
            for item in arguments[1:]
        )

    def _reconcile_stale_local_turns(
        self, session: Session, *, branch_id: str | None = None
    ) -> list[str]:
        query = (
            select(models.CodexSyncCursor, models.CodexThreadBinding)
            .join(
                models.CodexThreadBinding,
                models.CodexThreadBinding.id
                == models.CodexSyncCursor.thread_binding_id,
            )
            .where(
                models.CodexThreadBinding.status == "current",
            )
        )
        if branch_id is not None:
            query = query.where(models.CodexThreadBinding.branch_id == branch_id)
        cleared: list[str] = []
        for cursor, binding in session.execute(query):
            if not cursor.pending_json:
                continue
            # Web turns are represented by the same rollout cursor as local
            # terminals. Do not discard their pending JSONL state merely
            # because there is no local TUI process: it is the anchor used by
            # the live-activity tailer while the managed CLI is running.
            if self._pending_web_message(session, binding.branch_id) is not None:
                continue
            if self._thread_has_live_runtime(
                session, binding.branch_id, binding.thread_id
            ):
                continue
            stale_turn_ids = list(cursor.pending_json)
            cursor.pending_json = {}
            self._event(
                session,
                "branch",
                binding.branch_id,
                "codex.pending_reconciled",
                {"turn_ids": stale_turn_ids, "thread_id": binding.thread_id},
            )
            cleared.append(binding.branch_id)
        return list(dict.fromkeys(cleared))

    def _pending_web_message(
        self, session: Session, branch_id: str
    ) -> models.ConversationMessage | None:
        messages = list(
            session.scalars(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch_id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(20)
            )
        )
        return next(
            (
                message
                for message in messages
                if self._is_conversation_origin(message)
                and message.metadata_json.get("send_status") == "pending"
            ),
            None,
        )

    def _latest_web_message(
        self, session: Session, branch_id: str
    ) -> models.ConversationMessage | None:
        messages = list(
            session.scalars(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch_id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(20)
            )
        )
        return next(
            (
                message
                for message in messages
                if self._is_conversation_origin(message)
            ),
            None,
        )

    def _message_send_status(self, message_id: str) -> str:
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, message_id)
            return str(message.metadata_json.get("send_status")) if message else "interrupted"

    @staticmethod
    def _prompt_belongs_to_web(
        prompt: str, message: models.ConversationMessage
    ) -> bool:
        # Conversation messages are redacted before they are stored, while the
        # Codex JSONL keeps the original prompt.  Normalize both sides through
        # the same redaction function before comparing them; otherwise a prompt
        # containing an API key/token is misclassified as a local CLI turn.
        normalized_prompt = redact(prompt).strip()
        normalized_content = message.content.strip()
        return normalized_prompt == normalized_content or normalized_prompt.endswith(
            normalized_content
        )

    def _interrupt_pending_web(
        self, session: Session, branch_id: str, reason: str
    ) -> None:
        message = self._pending_web_message(session, branch_id)
        if message is None:
            return
        message.metadata_json = {
            **message.metadata_json,
            "send_status": "interrupted",
            "error": reason,
        }
        self._event(
            session,
            "branch",
            branch_id,
            "conversation.interrupted",
            {"message_id": message.id, "reason": reason},
        )

    def interrupt_web_turn(
        self, branch_id: str, message_id: str
    ) -> models.ConversationMessage:
        with self.session_factory() as session:
            message = session.get(models.ConversationMessage, message_id)
            if message is None or message.branch_id != branch_id:
                raise NotFoundError("Web message not found")
            if message.role != "user" or not self._is_conversation_origin(message):
                raise ConflictError(
                    "Only Web, scheduled Goal, or automatic continuation turns can be interrupted"
                )
            if message.metadata_json.get("send_status") == "pending":
                reason = "Interrupted from the Web workspace"
                session.execute(update(models.ClaudeWakeup).where(
                    models.ClaudeWakeup.branch_id == branch_id,
                    models.ClaudeWakeup.status.in_(("awaiting_turn", "scheduled", "claimed")),
                ).values(status="cancelled", reason=reason, completed_at=datetime.now(UTC)))
                message.metadata_json = {
                    **message.metadata_json,
                    "send_status": "interrupted",
                    "error": reason,
                }
                self._event(
                    session,
                    "branch",
                    branch_id,
                    "conversation.interrupted",
                    {"message_id": message.id, "reason": reason},
                )
                attempt = (
                    session.get(models.SessionAttempt, message.session_id)
                    if message.session_id
                    else None
                )
                if attempt is not None and attempt.status in {"starting", "active"}:
                    attempt.status = "completed"
                    attempt.completed_at = datetime.now(UTC)
                    attempt.error = reason
                    self._event(
                        session,
                        "branch",
                        branch_id,
                        "session.interrupted",
                        {"session_id": attempt.id, "reason": reason},
                    )
                session.commit()
            return message

    def _reconcile_orphaned_web_turns(
        self, session: Session, *, branch_id: str | None = None
    ) -> list[str]:
        """Release pending Web turns that no longer have a live request or Session."""
        now = datetime.now(UTC)
        orphaned: list[str] = []
        query = (
            select(models.ConversationMessage)
            .where(models.ConversationMessage.role == "user")
            .order_by(models.ConversationMessage.created_at.desc())
        )
        if branch_id is not None:
            query = query.where(models.ConversationMessage.branch_id == branch_id)
        pending = list(session.scalars(query))
        for message in pending:
            if (
                not self._is_conversation_origin(message)
                or message.metadata_json.get("send_status") != "pending"
            ):
                continue
            lock = self._web_turn_locks.get(message.branch_id)
            if self._web_turn_task_active(message.branch_id) or (
                lock is not None and lock.locked()
            ):
                continue
            attempt = (
                session.get(models.SessionAttempt, message.session_id)
                if message.session_id
                else None
            )
            if (
                attempt is not None
                and attempt.status in {"starting", "active"}
                and _as_utc(attempt.lease_expires_at) > now
                and attempt.lease_owner != "web"
            ):
                continue
            # A restarted Web worker has no in-memory task or lock. Preserve
            # its pending message when the durable JSONL cursor still contains
            # an unfinished turn; the live-activity tailer can then continue
            # from that cursor. Without a cursor there is no evidence that an
            # unexpired Web lease is doing work, so the orphan can be released.
            if (
                attempt is not None
                and attempt.lease_owner == "web"
                and attempt.status in {"starting", "active"}
                and _as_utc(attempt.lease_expires_at) > now
                and self._has_pending_runtime_cursor(session, attempt)
            ):
                continue
            error = "The Web runtime ended before this turn completed"
            message.metadata_json = {
                **message.metadata_json,
                "send_status": "failed",
                "error": error,
            }
            if attempt is not None and attempt.status in {"starting", "active"}:
                attempt.status = "completed"
                attempt.completed_at = now
                attempt.error = error
                self._event(
                    session,
                    "branch",
                    message.branch_id,
                    "session.web_reconciled",
                    {"session_id": attempt.id, "send_status": "failed"},
                )
            self._event(
                session,
                "branch",
                message.branch_id,
                "conversation.send_failed",
                {"message_id": message.id, "error": error},
            )
            self._enqueue_failure_notification(session, message)
            orphaned.append(message.branch_id)
        return list(dict.fromkeys(orphaned))

    @staticmethod
    def _has_pending_runtime_cursor(
        session: Session, attempt: models.SessionAttempt
    ) -> bool:
        """Whether a Session still has durable JSONL work to tail."""
        if not attempt.codex_thread_id:
            return False
        binding = session.scalar(
            select(models.CodexThreadBinding)
            .where(
                models.CodexThreadBinding.branch_id == attempt.branch_id,
                models.CodexThreadBinding.thread_id == attempt.codex_thread_id,
                models.CodexThreadBinding.status == "current",
            )
            .order_by(models.CodexThreadBinding.bound_at.desc())
            .limit(1)
        )
        if binding is None:
            return False
        cursor = session.scalar(
            select(models.CodexSyncCursor).where(
                models.CodexSyncCursor.thread_binding_id == binding.id
            )
        )
        return bool(cursor is not None and cursor.pending_json)

    def _enqueue_failure_notification(
        self, session: Session, message: models.ConversationMessage
    ) -> None:
        """Queue a single alert for a durable Web error without blocking the turn."""
        if (
            not self.settings.infoflow_notifications_enabled
            or not self.settings.infoflow_error_notifications_enabled
            or not self._is_conversation_origin(message)
            or message.metadata_json.get("scheduled_check_id")
        ):
            return
        try:
            with session.begin_nested():
                session.add(
                    models.ConversationFailureNotification(
                        message_id=message.id,
                        channel="infoflow",
                        recipient=self.settings.infoflow_recipient,
                    )
                )
                session.flush()
        except IntegrityError:
            # A repeated failure/reconciliation must not produce a second
            # alert for the same user query.
            pass

    def submit_handoff(
        self,
        branch_id: str,
        payload: schemas.HandoffCreate,
        *,
        session_id: str | None = None,
        lease_id: str | None = None,
    ) -> models.SessionAttempt:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            attempt = session.get(models.SessionAttempt, session_id) if session_id else None
            if attempt is None and session_id is None:
                attempt = session.scalar(
                    select(models.SessionAttempt)
                    .where(models.SessionAttempt.branch_id == branch.id)
                    .order_by(models.SessionAttempt.created_at.desc())
                    .limit(1)
                )
            if attempt is None:
                raise ConflictError("Branch has no Session to hand off")
            if attempt.branch_id != branch.id:
                raise ConflictError("Session does not belong to this branch")
            if lease_id is not None and attempt.lease_id != lease_id:
                raise ConflictError("Session lease does not match")
            if attempt.status in {"active", "starting"}:
                raise ConflictError("Exit the local Codex terminal before handing off")
            if attempt.handoff:
                raise ConflictError("This Session already has an immutable handoff")
            latest_user_message = session.scalar(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch.id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(1)
            )
            if latest_user_message and latest_user_message.metadata_json.get(
                "send_status"
            ) == "pending":
                raise ConflictError("Wait for the current Codex turn before handing off")
            attempt.handoff = payload.model_dump(mode="json")
            attempt.status = "completed"
            attempt.completed_at = attempt.completed_at or datetime.now(UTC)
            branch.head_sha = self.workspace.current_head(branch.cwd)
            work_item = branch.work_item
            if work_item.kind == PROGRAM_DESK_KIND:
                work_item.lifecycle = "active"
                work_item.flow = "actionable"
            elif payload.outcome == "waiting_external":
                work_item.lifecycle = "active"
                work_item.flow = "waiting_external"
            elif payload.outcome == "blocked":
                work_item.flow = "blocked"
            elif payload.outcome == "completed":
                work_item.lifecycle = "review"
                work_item.flow = "actionable"
            else:
                work_item.lifecycle = "active"
                work_item.flow = "actionable"
            self._event(
                session,
                "branch",
                branch.id,
                "session.handoff",
                {"session_id": attempt.id, "outcome": payload.outcome},
            )
            session.commit()
            return attempt

    def create_branch_result(
        self, branch_id: str, payload: schemas.BranchResultCreate
    ) -> tuple[models.BranchResult, models.MergeReview | None]:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            active = session.scalar(
                select(models.SessionAttempt.id).where(
                    models.SessionAttempt.branch_id == branch.id,
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > datetime.now(UTC),
                )
            )
            if active is not None:
                raise ConflictError(
                    "Exit the local Codex terminal before submitting a Branch result"
                )
            result = models.BranchResult(branch_id=branch.id, **payload.model_dump())
            session.add(result)
            session.flush()
            merge_review = None
            if payload.outcome == "merge" and branch.parent_branch_id:
                merge_review = models.MergeReview(
                    source_branch_id=branch.id,
                    target_branch_id=branch.parent_branch_id,
                    branch_result_id=result.id,
                )
                session.add(merge_review)
                branch.status = "review"
            elif payload.outcome == "park":
                branch.status = "parked"
            elif payload.outcome == "abandon":
                branch.status = "abandoned"
            elif payload.outcome == "expire":
                branch.status = "expired"
            self._event(
                session,
                "branch",
                branch.id,
                "branch.result_submitted",
                {"result_id": result.id, "outcome": payload.outcome},
            )
            session.commit()
            return result, merge_review

    def list_merge_queue(self) -> list[models.MergeReview]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(models.MergeReview)
                    .where(models.MergeReview.status == "pending")
                    .order_by(models.MergeReview.created_at)
                )
            )

    def resolve_merge(self, merge_id: str, payload: schemas.MergeResolve) -> models.MergeReview:
        with self.session_factory() as session:
            review = session.get(models.MergeReview, merge_id)
            if review is None:
                raise NotFoundError("Merge review not found")
            if review.status != "pending":
                raise ConflictError("Merge review is already resolved")
            source = self._branch(session, review.source_branch_id)
            review.status = "resolved"
            review.resolution = payload.resolution
            review.selected_changes = payload.selected_changes
            review.reviewer_note = payload.reviewer_note
            review.resolved_at = datetime.now(UTC)
            source.status = {
                "merge": "merged",
                "park": "parked",
                "abandon": "abandoned",
            }[payload.resolution]
            self._event(
                session,
                "branch",
                source.id,
                "branch.merge_resolved",
                {"resolution": payload.resolution, "target_branch_id": review.target_branch_id},
            )
            session.commit()
            return review

    def create_memory(self, payload: schemas.MemoryCreate) -> models.MemoryEntry:
        with self.session_factory() as session:
            program = self._program_by_code(session, payload.program_code)
            if payload.scope_type == "program":
                if payload.scope_id != program.id:
                    raise ConflictError("Program memory scope does not match program_code")
            elif payload.scope_type == "work_item":
                work_item = self._work(session, payload.scope_id)
                if work_item.program_id != program.id:
                    raise ConflictError("WorkItem memory scope belongs to another Program")
            else:
                branch = self._branch(session, payload.scope_id)
                if branch.work_item.program_id != program.id:
                    raise ConflictError("Branch memory scope belongs to another Program")
            if payload.source_branch_id:
                source = self._branch(session, payload.source_branch_id)
                if source.work_item.program_id != program.id:
                    raise ConflictError("Memory source branch belongs to another Program")
            memory = models.MemoryEntry(
                program_id=program.id,
                **payload.model_dump(exclude={"program_code", "title", "content"}),
                title=redact(payload.title),
                content=redact(payload.content),
            )
            session.add(memory)
            session.commit()
            return memory

    def list_memory(
        self, *, program_code: str | None = None, status: str | None = None
    ) -> list[models.MemoryEntry]:
        with self.session_factory() as session:
            query = select(models.MemoryEntry).order_by(models.MemoryEntry.updated_at.desc())
            if program_code:
                program = self._program_by_code(session, program_code)
                query = query.where(models.MemoryEntry.program_id == program.id)
            if status:
                query = query.where(models.MemoryEntry.status == status)
            return list(session.scalars(query.limit(500)))

    def review_memory(self, memory_id: str, payload: schemas.MemoryReview) -> models.MemoryEntry:
        with self.session_factory() as session:
            memory = session.get(models.MemoryEntry, memory_id)
            if memory is None:
                raise NotFoundError("Memory entry not found")
            if (
                payload.status == "accepted"
                and memory.source_branch_id is None
                and not memory.evidence_refs
            ):
                raise ConflictError(
                    "Accepted memory requires a source branch or evidence reference"
                )
            memory.status = payload.status
            memory.version += 1
            session.commit()
            return memory

    def create_external_job(self, payload: schemas.ExternalJobCreate) -> models.ExternalJob:
        with self.session_factory() as session:
            work_item = self._work(session, payload.work_item_id)
            duplicate = session.scalar(
                select(models.ExternalJob).where(
                    models.ExternalJob.provider == payload.provider,
                    models.ExternalJob.remote_job_id == payload.remote_job_id,
                )
            )
            if duplicate:
                if self._reconcile_work_activity(
                    session,
                    now=datetime.now(UTC),
                    work_ids={work_item.id},
                ):
                    session.commit()
                return duplicate
            job = models.ExternalJob(**payload.model_dump())
            if payload.status != "unknown":
                job.last_observed_at = datetime.now(UTC)
            session.add(job)
            session.flush()
            if external_job_is_active(job.status):
                work_item.lifecycle = "active"
            self._reconcile_work_activity(
                session,
                now=job.last_observed_at or datetime.now(UTC),
                work_ids={work_item.id},
            )
            session.commit()
            return job

    def update_external_job(
        self, job_id: str, payload: schemas.ExternalJobUpdate
    ) -> models.ExternalJob:
        with self.session_factory() as session:
            job = session.get(models.ExternalJob, job_id)
            if job is None:
                raise NotFoundError("External job not found")
            values = payload.model_dump(exclude_unset=True)
            for key, value in values.items():
                setattr(job, key, value)
            job.last_observed_at = datetime.now(UTC)
            session.flush()
            self._reconcile_work_activity(
                session,
                now=job.last_observed_at,
                work_ids={job.work_item_id},
            )
            session.commit()
            return job

    def today(self) -> dict[str, Any]:
        with self.session_factory() as session:
            if self._expire_stale_sessions(session):
                session.commit()
            continue_items = list(
                session.scalars(
                    select(models.WorkItem)
                    .where(
                        models.WorkItem.kind != PROGRAM_DESK_KIND,
                        models.WorkItem.hidden.is_(False),
                        models.WorkItem.lifecycle == "active",
                        models.WorkItem.flow.in_(["running", "actionable"]),
                    )
                    .order_by(models.WorkItem.priority.desc())
                    .limit(20)
                )
            )
            needs_decision = list(
                session.scalars(
                    select(models.WorkItem)
                    .where(
                        models.WorkItem.kind != PROGRAM_DESK_KIND,
                        models.WorkItem.hidden.is_(False),
                        models.WorkItem.flow == "needs_decision",
                    )
                    .order_by(models.WorkItem.priority.desc())
                    .limit(20)
                )
            )
            waiting_candidates = list(
                session.scalars(
                    select(models.ExternalJob)
                    .join(models.WorkItem, models.WorkItem.id == models.ExternalJob.work_item_id)
                    .where(models.WorkItem.hidden.is_(False))
                    .order_by(models.ExternalJob.created_at.desc())
                    .limit(500)
                )
            )
            waiting_external = [
                job for job in waiting_candidates if external_job_is_active(job.status)
            ][:50]
            suggested_next = list(
                session.scalars(
                    select(models.WorkItem)
                    .where(
                        models.WorkItem.kind != PROGRAM_DESK_KIND,
                        models.WorkItem.hidden.is_(False),
                        models.WorkItem.lifecycle == "ready",
                        models.WorkItem.flow == "actionable",
                    )
                    .order_by(models.WorkItem.priority.desc(), models.WorkItem.created_at)
                    .limit(10)
                )
            )
            active_sessions = (
                session.scalar(
                    select(func.count())
                    .select_from(models.SessionAttempt)
                    .where(
                        models.SessionAttempt.status.in_(["starting", "active"]),
                        models.SessionAttempt.lease_expires_at > datetime.now(UTC),
                    )
                )
                or 0
            )
            pending_merges = (
                session.scalar(
                    select(func.count())
                    .select_from(models.MergeReview)
                    .where(models.MergeReview.status == "pending")
                )
                or 0
            )
            return {
                "continue_items": continue_items,
                "needs_decision": needs_decision,
                "waiting_external": waiting_external,
                "suggested_next": suggested_next,
                "active_sessions": active_sessions,
                "pending_merges": pending_merges,
            }

    def terminal_command(self, branch_id: str) -> str:
        executable = PROJECT_ROOT / ".venv" / "bin" / "rops"
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) == "pi":
                raise ConflictError(
                    "Managed local Pi takeover is not available in this canary. "
                    "Use the Web composer, steer, follow-up, and abort controls instead."
                )
            if branch.work_item.kind == PROGRAM_DESK_KIND:
                return (
                    f"{shell_quote(str(executable))} desk "
                    f"{shell_quote(branch.work_item.program.code)}"
                )
        return f"{shell_quote(str(executable))} open {shell_quote(branch_id)}"

    def _prepare_fresh_codex_thread(
        self,
        session: Session,
        branch: models.ExecutionBranch,
        *,
        prior_thread_id: str,
        reason: str,
        now: datetime,
    ) -> str:
        """Retire a Codex thread and bind the Branch to a fresh deferred thread."""
        snapshot = dict(branch.context_snapshot or {})
        handoff = self._thread_recovery_handoff(
            session,
            branch,
            prior_thread_id=prior_thread_id,
            recovered_at=now,
            reason=reason,
        )
        snapshot["thread_recovery"] = handoff
        replacement = f"deferred:{models.new_id()}"
        branch.context_snapshot = snapshot
        branch.codex_thread_id = replacement
        for binding in session.scalars(
            select(models.CodexThreadBinding).where(
                models.CodexThreadBinding.branch_id == branch.id,
                models.CodexThreadBinding.status == "current",
            )
        ):
            binding.status = "historical"
            binding.retired_at = now
        self._event(
            session,
            "branch",
            branch.id,
            "codex.thread_recovery_prepared",
            {
                "prior_thread_id": prior_thread_id,
                "replacement": "deferred_fresh_thread",
                "reason": reason,
            },
        )
        return replacement

    def recover_codex_thread(self, branch_id: str) -> models.ExecutionBranch:
        """Prepare a fresh Codex execution thread without creating a new Branch."""
        now = datetime.now(UTC)
        with self.session_factory() as session:
            self._expire_stale_sessions(session, now=now, branch_id=branch_id)
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) != "codex":
                raise ConflictError("Only Codex Branches can recover a Codex thread")
            if branch.hidden:
                raise ConflictError("Restore this hidden Branch before recovering its Codex thread")
            if branch.mode == "capture":
                raise ConflictError("Capture-only Branches cannot recover a Codex thread")
            if branch.status != "active":
                raise ConflictError(f"Branch is {branch.status}; it cannot recover a Codex thread")
            if self._pending_web_message(session, branch.id) is not None:
                raise ConflictError("Wait for the current Web turn to finish")
            if self._active_attempt(session, branch.id) is not None:
                raise ConflictError("Finish the active session before recovering its Codex thread")

            prior_thread_id = str(branch.codex_thread_id or "").strip()
            if not prior_thread_id or prior_thread_id.startswith("deferred:"):
                raise ConflictError("This Branch does not have a persisted Codex thread to recover")

            self._prepare_fresh_codex_thread(
                session,
                branch,
                prior_thread_id=prior_thread_id,
                reason="manual_thread_recovery",
                now=now,
            )
            session.commit()
            return branch

    def _thread_recovery_handoff(
        self,
        session: Session,
        branch: models.ExecutionBranch,
        *,
        prior_thread_id: str,
        recovered_at: datetime,
        reason: str = "repeated_zero_output_gateway_failures",
    ) -> dict[str, Any]:
        """Build a bounded, evidence-preserving handoff for a fresh thread."""
        latest_turns = list(
            session.scalars(
                select(models.CodexTurn)
                .where(
                    models.CodexTurn.branch_id == branch.id,
                    models.CodexTurn.status == "completed",
                )
                .order_by(models.CodexTurn.completed_at.desc())
                .limit(3)
            )
        )
        completed_turns = [
            {
                "turn_id": turn.external_turn_id,
                "completed_at": turn.completed_at.isoformat() if turn.completed_at else None,
                "prompt": redact(str(turn.prompt or ""))[-1_500:],
                "conclusion": redact(str(turn.final_answer or ""))[-6_000:],
            }
            for turn in latest_turns
        ]
        messages = list(
            session.scalars(
                select(models.ConversationMessage)
                .where(
                    models.ConversationMessage.branch_id == branch.id,
                    models.ConversationMessage.role == "user",
                )
                .order_by(models.ConversationMessage.created_at.desc())
                .limit(20)
            )
        )
        recent_failed_queries = [
            {
                "at": message.created_at.isoformat(),
                "query": redact(message.content)[-1_000:],
            }
            for message in messages
            if self._is_conversation_origin(message)
            and message.metadata_json.get("send_status") == "failed"
        ][:4]
        jobs = list(
            session.scalars(
                select(models.ExternalJob)
                .where(models.ExternalJob.work_item_id == branch.work_item_id)
                .order_by(models.ExternalJob.updated_at.desc())
                .limit(12)
            )
        )
        return {
            "kind": "codex_thread_recovery",
            "prior_thread_id": prior_thread_id,
            "recovered_at": recovered_at.isoformat(),
            "reason": reason,
            "latest_completed_turns": completed_turns,
            "recent_failed_queries": recent_failed_queries,
            "external_jobs": [
                {
                    "provider": job.provider,
                    "remote_job_id": job.remote_job_id,
                    "status": job.status,
                    "url": job.url,
                }
                for job in jobs
            ],
        }

    def codex_terminal_command(
        self, branch_id: str, *, execution: dict[str, Any] | None = None
    ) -> str:
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) == "claude":
                if not branch.codex_thread_id:
                    raise ConflictError("Claude session has not been prepared")
                claude_adapter = self._adapter_for_runtime("claude")
                terminal_command = claude_adapter.terminal_command  # type: ignore[attr-defined]
                return terminal_command(
                    thread_id=branch.codex_thread_id,
                    cwd=branch.cwd,
                    execution=execution or self._execution_snapshot(branch),
                )
            command = [*shlex.split(self.settings.codex_command)]
            launch = self.identities.launch(
                execution or self.identities.snapshot(branch.execution_preferences)
            )
            command.extend(launch.args())
            if branch.codex_thread_id and not branch.codex_thread_id.startswith("deferred:"):
                command.extend(["resume", "-C", branch.cwd, branch.codex_thread_id])
            else:
                command.extend(["-C", branch.cwd])
            return shlex.join(command)

    def raw_terminal_command(self, branch_id: str) -> str:
        command = self.codex_terminal_command(branch_id)
        with self.session_factory() as session:
            branch = self._branch(session, branch_id)
            if self._runtime_name(branch) == "claude":
                return f"cd {shell_quote(branch.cwd)} && {command}"
        return command

    def _program_by_code(self, session: Session, code: str) -> models.Program:
        program = session.scalar(select(models.Program).where(models.Program.code == code.upper()))
        if program is None:
            raise NotFoundError(f"Program not found: {code}")
        return program

    def _work(self, session: Session, identity: str) -> models.WorkItem:
        work_item = session.get(models.WorkItem, identity)
        if work_item is None:
            work_item = session.scalar(
                select(models.WorkItem).where(models.WorkItem.human_id == identity.upper())
            )
        if work_item is None:
            raise NotFoundError(f"Work item not found: {identity}")
        return work_item

    def _branch(self, session: Session, branch_id: str) -> models.ExecutionBranch:
        branch = session.scalar(
            select(models.ExecutionBranch)
            .where(models.ExecutionBranch.id == branch_id)
            .options(selectinload(models.ExecutionBranch.work_item))
        )
        if branch is None:
            raise NotFoundError("Branch not found")
        return branch

    def _memory_context(
        self, session: Session, branch: models.ExecutionBranch
    ) -> list[dict[str, Any]]:
        inherited = (
            list(branch.context_snapshot.get("inherited_memory", []))
            if branch.parent_branch_id
            else []
        )
        if branch.parent_branch_id:
            scope_filter = (
                models.MemoryEntry.scope_type == "branch",
                models.MemoryEntry.scope_id == branch.id,
            )
        else:
            scope_filter = (
                or_(
                    (
                        (models.MemoryEntry.scope_type == "program")
                        & (models.MemoryEntry.scope_id == branch.work_item.program_id)
                    ),
                    (
                        (models.MemoryEntry.scope_type == "work_item")
                        & (models.MemoryEntry.scope_id == branch.work_item_id)
                    ),
                    (
                        (models.MemoryEntry.scope_type == "branch")
                        & (models.MemoryEntry.scope_id == branch.id)
                    ),
                ),
            )
        live = list(
            session.scalars(
                select(models.MemoryEntry)
                .where(models.MemoryEntry.status == "accepted", *scope_filter)
                .order_by(models.MemoryEntry.updated_at, models.MemoryEntry.id)
                .limit(100)
            )
        )
        indexed = {(item["id"], item["version"]): item for item in inherited}
        for item in live:
            serialized = self._serialize_memory(item)
            indexed[(item.id, item.version)] = serialized
        return list(indexed.values())

    def _build_branch_brief(
        self,
        session: Session,
        branch: models.ExecutionBranch,
        *,
        previous_handoff: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        work_item = branch.work_item
        active_goals = [
            {
                "id": goal.id,
                "title": goal.title,
                "objective": goal.objective,
                "status": goal.status,
                "executor": goal.executor,
                "completion": goal.completion_json,
            }
            for goal in session.scalars(
                select(models.GoalRun)
                .where(
                    models.GoalRun.branch_id == branch.id,
                    models.GoalRun.status == "active",
                )
                .order_by(models.GoalRun.created_at)
            )
        ]
        return build_brief(
            work_item.program,
            work_item,
            branch,
            previous_handoff=previous_handoff,
            memory_context=self._memory_context(session, branch),
            program_context=(
                self._program_context(session, work_item.program)
                if work_item.kind == PROGRAM_DESK_KIND
                else None
            ),
            active_goals=active_goals,
        )

    def _program_context(
        self, session: Session, program: models.Program
    ) -> dict[str, Any]:
        work_items = list(
            session.scalars(
                select(models.WorkItem)
                .where(
                    models.WorkItem.program_id == program.id,
                    models.WorkItem.kind != PROGRAM_DESK_KIND,
                    models.WorkItem.hidden.is_(False),
                )
                .order_by(models.WorkItem.updated_at.desc())
                .limit(50)
            )
        )
        context_items: list[dict[str, Any]] = []
        remaining_result_chars = 24_000
        for work_item in work_items:
            branches = list(
                session.scalars(
                    select(models.ExecutionBranch)
                    .where(models.ExecutionBranch.work_item_id == work_item.id)
                    .order_by(models.ExecutionBranch.created_at)
                    .limit(10)
                )
            )
            branch_context: list[dict[str, Any]] = []
            for branch in branches:
                latest_turn = next(
                    (
                        turn
                        for turn in session.scalars(
                            select(models.CodexTurn)
                            .where(models.CodexTurn.branch_id == branch.id)
                            .order_by(models.CodexTurn.completed_at.desc())
                            .limit(20)
                        )
                        if _turn_is_visible(turn)
                    ),
                    None,
                )
                latest_session = session.scalar(
                    select(models.SessionAttempt)
                    .where(models.SessionAttempt.branch_id == branch.id)
                    .order_by(models.SessionAttempt.created_at.desc())
                    .limit(1)
                )
                result_summary = None
                if latest_turn and remaining_result_chars > 0:
                    result_summary = latest_turn.final_answer[
                        : min(1200, remaining_result_chars)
                    ]
                    remaining_result_chars -= len(result_summary)
                handoff = latest_session.handoff if latest_session else {}
                branch_context.append(
                    {
                        "branch_id": branch.id,
                        "name": "Primary" if not branch.parent_branch_id else branch.name,
                        "status": branch.status,
                        "session_status": (
                            latest_session.status if latest_session else "not_started"
                        ),
                        "latest_result": result_summary,
                        "latest_result_at": (
                            latest_turn.completed_at.isoformat() if latest_turn else None
                        ),
                        "handoff": {
                            "outcome": handoff.get("outcome"),
                            "conclusion": str(handoff.get("conclusion", ""))[:800],
                            "next_action": str(handoff.get("next_action", ""))[:400],
                        }
                        if handoff
                        else {},
                    }
                )
            context_items.append(
                {
                    "work_id": work_item.human_id,
                    "title": work_item.title,
                    "objective": work_item.objective,
                    "lifecycle": work_item.lifecycle,
                    "flow": work_item.flow,
                    "updated_at": work_item.updated_at.isoformat(),
                    "branches": branch_context,
                }
            )
        return {
            "program": {
                "code": program.code,
                "name": program.name,
                "objective": program.objective,
            },
            "work_items": context_items,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def _expire_stale_sessions(
        self,
        session: Session,
        *,
        now: datetime | None = None,
        branch_id: str | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        local_terminal_prefix = f"terminal:{socket.gethostname()}:%"
        query = select(models.SessionAttempt).where(
            models.SessionAttempt.status.in_(["starting", "active"]),
            or_(
                models.SessionAttempt.lease_expires_at <= now,
                models.SessionAttempt.lease_owner.like(local_terminal_prefix),
            ),
        )
        if branch_id is not None:
            query = query.where(models.SessionAttempt.branch_id == branch_id)
        candidates = list(session.scalars(query))
        stale_attempts: list[models.SessionAttempt] = []
        renewed_attempts: list[models.SessionAttempt] = []
        dead_terminal_ids: set[str] = set()
        for attempt in candidates:
            terminal_process_state = self._terminal_owner_process_state(
                attempt.lease_owner, attempt.branch_id
            )
            if terminal_process_state is False:
                stale_attempts.append(attempt)
                dead_terminal_ids.add(attempt.id)
                continue
            if _as_utc(attempt.lease_expires_at) > now:
                continue
            lock = self._web_turn_locks.get(attempt.branch_id)
            if self._web_turn_task_active(attempt.branch_id) or (
                lock is not None and lock.locked()
            ):
                attempt.heartbeat_at = now
                attempt.lease_expires_at = now + timedelta(minutes=self.settings.lease_minutes)
                renewed_attempts.append(attempt)
            else:
                stale_attempts.append(attempt)
        changed = bool(stale_attempts or renewed_attempts)
        for attempt in renewed_attempts:
            branch = session.get(models.ExecutionBranch, attempt.branch_id)
            if branch and branch.work_item.flow == "actionable":
                branch.work_item.flow = "running"
        for attempt in stale_attempts:
            attempt.status = "expired"
            attempt.completed_at = now
            dead_terminal = attempt.id in dead_terminal_ids
            if dead_terminal:
                attempt.error = "Local terminal process ended; stale lease reclaimed"
            branch = session.get(models.ExecutionBranch, attempt.branch_id)
            if branch:
                self._event(
                    session,
                    "branch",
                    branch.id,
                    "session.expired",
                    {
                        "session_id": attempt.id,
                        "reason": (
                            "terminal_process_ended" if dead_terminal else "lease_expired"
                        ),
                    },
                )
        if not changed:
            # Global lifecycle callers still use this as their regular work
            # activity reconciliation pass. Branch detail/state reads do not.
            return (
                False
                if branch_id is not None
                else self._reconcile_work_activity(session, now=now)
            )
        session.flush()
        work_ids: set[str] | None = None
        if branch_id is not None:
            branch = session.get(models.ExecutionBranch, branch_id)
            work_ids = {branch.work_item_id} if branch is not None else set()
        return self._reconcile_work_activity(session, now=now, work_ids=work_ids) or changed

    @staticmethod
    def _terminal_owner_process_state(owner: str, branch_id: str) -> bool | None:
        """Return whether a well-formed local terminal owner still owns its PID.

        A terminal lease normally lasts longer than its heartbeat interval so a
        busy local process survives brief database stalls. If the process exits
        uncleanly, however, waiting for the full lease makes both ``rops open``
        and the Web composer report a terminal that no longer exists. Only
        same-host owners created by the CLI are inspected; legacy, registry,
        test, and remote-host owners keep the normal lease semantics.
        """
        parts = owner.split(":", 2)
        if (
            len(parts) != 3
            or parts[0] != "terminal"
            or parts[1] != socket.gethostname()
            or not parts[2].isdigit()
        ):
            return None
        process_dir = Path("/proc") / parts[2]
        try:
            process_state = (process_dir / "stat").read_text(encoding="utf-8").split()[2]
            arguments = [
                item.decode(errors="replace")
                for item in (process_dir / "cmdline").read_bytes().split(b"\0")
                if item
            ]
        except FileNotFoundError:
            return False
        except (IndexError, OSError, PermissionError):
            return None
        if process_state == "Z" or not arguments:
            return False
        is_rops = any(Path(argument).name == "rops" for argument in arguments[:2])
        opens_branch = any(
            argument == "open" and index + 1 < len(arguments) and arguments[index + 1] == branch_id
            for index, argument in enumerate(arguments)
        )
        return is_rops and opens_branch

    def _reconcile_work_activity(
        self,
        session: Session,
        *,
        now: datetime,
        work_ids: set[str] | None = None,
    ) -> bool:
        active_session_query = (
            select(models.ExecutionBranch.work_item_id)
            .join(
                models.SessionAttempt,
                models.SessionAttempt.branch_id == models.ExecutionBranch.id,
            )
            .where(
                models.SessionAttempt.status.in_(["starting", "active"]),
                models.SessionAttempt.lease_expires_at > now,
            )
            .distinct()
        )
        if work_ids is not None:
            active_session_query = active_session_query.where(
                models.ExecutionBranch.work_item_id.in_(work_ids)
            )
        busy_work_ids = set(session.scalars(active_session_query))

        external_query = select(
            models.ExternalJob.work_item_id,
            models.ExternalJob.status,
        )
        if work_ids is not None:
            external_query = external_query.where(
                models.ExternalJob.work_item_id.in_(work_ids)
            )
        busy_work_ids.update(
            work_item_id
            for work_item_id, status in session.execute(external_query)
            if external_job_is_active(status)
        )

        goal_query = select(models.GoalRun.work_item_id, models.GoalRun.status)
        if work_ids is not None:
            goal_query = goal_query.where(models.GoalRun.work_item_id.in_(work_ids))
        goal_active_work_ids: set[str] = set()
        goal_decision_work_ids: set[str] = set()
        for work_item_id, goal_status in session.execute(goal_query):
            if goal_status == "active":
                goal_active_work_ids.add(work_item_id)
            elif goal_status == "needs_decision":
                goal_decision_work_ids.add(work_item_id)
        busy_work_ids.update(goal_active_work_ids)
        goal_managed_work_ids = goal_active_work_ids | goal_decision_work_ids

        work_query = select(models.WorkItem).where(
            or_(
                models.WorkItem.flow.in_(["running", "actionable"]),
                models.WorkItem.id.in_(goal_managed_work_ids),
            )
        )
        if work_ids is not None:
            work_query = work_query.where(models.WorkItem.id.in_(work_ids))
        changed = False
        for work_item in session.scalars(work_query):
            if work_item.id in goal_decision_work_ids:
                desired = "needs_decision"
            else:
                desired = "running" if work_item.id in busy_work_ids else "actionable"
            if work_item.flow != desired:
                work_item.flow = desired
                changed = True
        return changed

    def reconcile_external_activity(self, work_ids: set[str]) -> None:
        """Recompute WorkItem flow after an external provider or Goal update."""

        if not work_ids:
            return
        with self.session_factory() as session:
            if self._reconcile_work_activity(session, now=datetime.now(UTC), work_ids=work_ids):
                session.commit()

    @staticmethod
    def _serialize_memory(memory: models.MemoryEntry) -> dict[str, Any]:
        return {
            "id": memory.id,
            "version": memory.version,
            "scope_type": memory.scope_type,
            "scope_id": memory.scope_id,
            "kind": memory.kind,
            "title": memory.title,
            "content": memory.content,
            "confidence": memory.confidence,
            "evidence_refs": memory.evidence_refs,
            "updated_at": memory.updated_at.isoformat(),
        }

    @staticmethod
    def _event(
        session: Session,
        scope_type: str,
        scope_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            models.ActivityEvent(
                scope_type=scope_type,
                scope_id=scope_id,
                event_type=event_type,
                actor=socket.gethostname(),
                payload=payload,
            )
        )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _turn_is_visible(turn: models.CodexTurn) -> bool:
    return not bool((turn.metadata_json or {}).get("hidden"))
