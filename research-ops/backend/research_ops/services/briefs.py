from __future__ import annotations

import hashlib
import json
from typing import Any

from research_ops import models
from research_ops.constants import PROGRAM_DESK_KIND

NEEDS_DEFINITION_LABEL = "needs_definition"


def build_brief(
    program: models.Program,
    work_item: models.WorkItem,
    branch: models.ExecutionBranch,
    *,
    previous_handoff: dict[str, Any] | None = None,
    memory_context: list[dict[str, Any]] | None = None,
    program_context: dict[str, Any] | None = None,
    active_goals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fork_objective = branch.context_snapshot.get("fork_objective")
    objective = (
        str(fork_objective).strip()
        if isinstance(fork_objective, str) and fork_objective.strip()
        else work_item.objective
    )
    fork_context = (
        {
            "parent_branch_id": branch.parent_branch_id,
            "parent_thread_id": branch.context_snapshot.get("parent_thread_id"),
            "forked_at": branch.context_snapshot.get("forked_at"),
            "objective": objective,
            "continuation": branch.context_snapshot.get("continuation", "parallel"),
            "parent_snapshot": branch.context_snapshot.get("parent_snapshot", {}),
        }
        if branch.parent_branch_id
        else {}
    )
    brief = {
        "schema_version": "1.0",
        "program": {
            "code": program.code,
            "name": program.name,
            "objective": program.objective,
        },
        "work_id": work_item.human_id,
        "work_item_revision": work_item.revision,
        "branch_id": branch.id,
        "branch_name": branch.name,
        "branch_mode": branch.mode,
        "objective": objective,
        "fork_context": fork_context,
        "program_desk": work_item.kind == PROGRAM_DESK_KIND,
        "program_context": program_context or {},
        "definition_pending": NEEDS_DEFINITION_LABEL in work_item.labels,
        "acceptance_criteria": work_item.acceptance_criteria,
        "repository": {
            "cwd": branch.cwd,
            "git_branch": branch.git_branch,
            "base_sha": branch.base_sha,
        },
        "previous_handoff": previous_handoff or {},
        "next_action": (previous_handoff or {}).get("next_action"),
        "memory_context": memory_context or [],
        "active_goals": active_goals or [],
        "runtime_handoff": branch.context_snapshot.get("runtime_handoff") or {},
        "thread_recovery": branch.context_snapshot.get("thread_recovery") or {},
    }
    brief["brief_hash"] = hashlib.sha256(
        json.dumps(brief, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return brief


def developer_instructions(brief: dict[str, Any]) -> str:
    """Briefs are display metadata, never instructions for the conversation."""
    return ""
