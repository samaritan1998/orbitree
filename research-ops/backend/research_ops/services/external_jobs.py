from __future__ import annotations

from typing import Literal

ExternalJobPhase = Literal["active", "completed", "failed", "cancelled", "unknown"]

_ACTIVE_STATUSES = {
    "created",
    "pending",
    "queued",
    "submitted",
    "starting",
    "running",
    "in_progress",
}


def external_job_phase(status: str) -> ExternalJobPhase:
    """Classify provider-specific status text without discarding its details."""
    normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _ACTIVE_STATUSES or normalized.startswith(
        ("created_", "pending_", "queued_", "submitted_", "starting_", "running_")
    ):
        return "active"
    if normalized in {"success", "succeeded", "complete", "completed", "done"} or (
        normalized.startswith(("success_", "succeeded_", "complete_", "completed_", "done_"))
    ):
        return "completed"
    if normalized in {"failed", "error", "inconclusive"} or normalized.startswith(
        ("failed_", "error_", "inconclusive_")
    ):
        return "failed"
    if normalized in {"cancelled", "canceled", "superseded"} or normalized.startswith(
        ("cancelled_", "canceled_", "superseded_")
    ):
        return "cancelled"
    return "unknown"


def external_job_is_active(status: str) -> bool:
    return external_job_phase(status) == "active"
