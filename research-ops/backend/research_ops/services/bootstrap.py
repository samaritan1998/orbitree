from __future__ import annotations

import logging

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from research_ops import models
from research_ops.constants import PROGRAM_DESK_KIND
from research_ops.settings import Settings

from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


def bootstrap_catalog(session_factory: sessionmaker[Session], settings: Settings) -> None:
    payload = yaml.safe_load(settings.programs_file.read_text(encoding="utf-8"))
    program_specs = payload.get("programs", [])
    workspace = WorkspaceManager(settings)
    with session_factory() as session:
        # Seed once. A restart must not overwrite personal names or unarchive directions.
        if session.scalar(select(models.Program.id).limit(1)) is None:
            session.add_all(models.Program(**spec) for spec in program_specs)
            session.flush()
        active_programs = select(models.Program).where(models.Program.active.is_(True))
        for program in session.scalars(active_programs):
            desk_workspace = workspace.prepare_program_desk(
                program_code=program.code,
                program_name=program.name,
                objective=program.objective,
            )
            _ensure_program_desk(session, program, desk_workspace.cwd)
        _ensure_workspace_entries(session, workspace)
        session.commit()


def _ensure_program_desk(session: Session, program: models.Program, cwd: str) -> None:
    desk = session.scalar(
        select(models.WorkItem).where(
            models.WorkItem.program_id == program.id,
            models.WorkItem.kind == PROGRAM_DESK_KIND,
        )
    )
    if desk is None:
        desk = models.WorkItem(
            human_id=f"{program.code}-DESK",
            program_id=program.id,
            title=f"{program.name} Desk",
            objective=program.objective,
            kind=PROGRAM_DESK_KIND,
            lifecycle="ready",
            flow="actionable",
            priority=0,
            acceptance_criteria=[],
            labels=["internal", "program_desk"],
        )
        session.add(desk)
        session.flush()
    else:
        desk.title = f"{program.name} Desk"
        desk.objective = program.objective
    branch = session.scalar(
        select(models.ExecutionBranch).where(models.ExecutionBranch.work_item_id == desk.id)
    )
    if branch is None:
        session.add(
            models.ExecutionBranch(
                work_item_id=desk.id,
                name="Program Desk",
                slug="desk",
                status="active",
                mode="write",
                cwd=cwd,
                workspace_path=cwd,
                context_snapshot={"program_desk": True, "memory_snapshot_version": 1},
            )
        )
    else:
        branch.workspace_path = cwd


def _ensure_workspace_entries(session: Session, workspace: WorkspaceManager) -> None:
    branches = session.scalars(
        select(models.ExecutionBranch).options(
            selectinload(models.ExecutionBranch.work_item).selectinload(models.WorkItem.program),
            selectinload(models.ExecutionBranch.work_item).selectinload(
                models.WorkItem.repository
            ),
        )
    )
    for branch in branches:
        work = branch.work_item
        if work.kind == PROGRAM_DESK_KIND or work.hidden:
            continue
        repository_name = work.repository.name if work.repository else None
        try:
            branch.workspace_path = workspace.ensure_workspace_entry(
                program_code=work.program.code,
                work_human_id=work.human_id,
                branch_slug=branch.slug,
                repository_name=repository_name,
                existing_cwd=branch.cwd,
            )
        except Exception as exc:
            branch.workspace_path = branch.cwd
            logger.warning(
                "Could not create Q3 entry for %s/%s: %s",
                work.human_id,
                branch.slug,
                exc,
            )
