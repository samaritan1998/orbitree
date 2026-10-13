from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models, schemas
from research_ops.services.core import NotFoundError


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def record_goal_history(
    session: Session,
    monitor: models.NativeGoalMonitor,
    now: datetime,
    *,
    cleared: bool = False,
    source: str = "live",
) -> None:
    snapshot = monitor.goal_snapshot
    if not snapshot or not snapshot.get("objective"):
        return
    row = session.get(models.NativeGoalHistory, (monitor.branch_id, monitor.goal_key))
    if row is None:
        row = models.NativeGoalHistory(
            branch_id=monitor.branch_id,
            goal_key=monitor.goal_key,
            thread_id=monitor.thread_id,
            first_recorded_at=now,
            last_recorded_at=now,
            source=source,
        )
        session.add(row)
    elif (row.cleared_at and not cleared) or _utc(now) < _utc(row.last_recorded_at):
        return
    values = {
        "goal_snapshot": dict(snapshot),
        "last_summary": monitor.last_summary,
        "last_error": monitor.last_error,
        "turn_id": monitor.turn_id,
        "source": source,
    }
    changed = any(getattr(row, key) != value for key, value in values.items())
    for key, value in values.items():
        setattr(row, key, value)
    if cleared and row.cleared_at is None:
        row.cleared_at = now
        changed = True
    if changed:
        row.last_recorded_at = now


class NativeGoalHistoryService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def backfill(self) -> int:
        """Recover missing records from local evidence; never query or resume Codex."""
        with self.session_factory() as session:
            existing = set(
                session.execute(
                    select(models.NativeGoalHistory.branch_id, models.NativeGoalHistory.goal_key)
                ).all()
            )
            candidates: dict[tuple[str, str], dict[str, Any]] = {}
            notices = session.scalars(
                select(models.NativeGoalNotification)
                .order_by(
                    models.NativeGoalNotification.created_at,
                    models.NativeGoalNotification.id,
                )
                .execution_options(yield_per=500)
            )
            for notice in notices:
                parts = notice.event_key.split(":")
                if len(parts) != 3 or parts[0] != notice.branch_id or len(parts[1]) != 64:
                    continue
                key = (notice.branch_id, parts[1])
                payload = notice.payload
                if key in existing or not payload.get("objective") or not payload.get("thread_id"):
                    continue
                try:
                    now = _utc(datetime.fromisoformat(str(payload["observed_at"])))
                except (KeyError, ValueError):
                    now = _utc(notice.created_at)
                snapshot = {
                    "threadId": payload["thread_id"],
                    "objective": payload["objective"],
                    "status": payload.get("goal_status") or "unknown",
                    **{
                        target: payload[source]
                        for source, target in (
                            ("tokens_used", "tokensUsed"),
                            ("time_used_seconds", "timeUsedSeconds"),
                        )
                        if source in payload
                    },
                }
                previous = candidates.get(key)
                first = min(previous["first_recorded_at"], now) if previous else now
                if previous and previous["last_recorded_at"] > now:
                    previous["first_recorded_at"] = first
                    continue
                candidates[key] = {
                    "branch_id": notice.branch_id,
                    "goal_key": parts[1],
                    "thread_id": payload["thread_id"],
                    "goal_snapshot": snapshot,
                    "source": "notification",
                    "first_recorded_at": first,
                    "last_recorded_at": now,
                    "cleared_at": now if notice.kind == "cleared" else None,
                    "last_summary": payload.get("summary"),
                    "last_error": payload.get("error"),
                }
            for monitor in session.scalars(select(models.NativeGoalMonitor)):
                key = (monitor.branch_id, monitor.goal_key)
                if key in existing or not monitor.goal_snapshot.get("objective"):
                    continue
                now = _utc(monitor.updated_at)
                previous = candidates.get(key)
                # Monitor snapshots retain exact objectives and native timestamps
                # that the older notification payload did not contain.
                if previous and previous["last_recorded_at"] > now:
                    previous["goal_snapshot"] = {
                        **monitor.goal_snapshot,
                        **previous["goal_snapshot"],
                        "objective": monitor.goal_snapshot["objective"],
                    }
                    previous["source"] = "monitor"
                    continue
                candidates[key] = {
                    "branch_id": monitor.branch_id,
                    "goal_key": monitor.goal_key,
                    "thread_id": monitor.thread_id,
                    "goal_snapshot": dict(monitor.goal_snapshot),
                    "source": "monitor",
                    "first_recorded_at": previous["first_recorded_at"] if previous else now,
                    "last_recorded_at": now,
                    "cleared_at": previous.get("cleared_at") if previous else None,
                    "last_summary": monitor.last_summary,
                    "last_error": monitor.last_error,
                    "turn_id": monitor.turn_id,
                }
            session.add_all(models.NativeGoalHistory(**value) for value in candidates.values())
            session.commit()
            return len(candidates)

    def page(
        self, branch_id: str, *, limit: int = 20, offset: int = 0
    ) -> schemas.NativeGoalHistoryPage:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            filters = [models.NativeGoalHistory.branch_id == branch_id]
            monitor = session.get(models.NativeGoalMonitor, branch_id)
            if (
                monitor
                and monitor.thread_id == branch.codex_thread_id
                and monitor.goal_snapshot
                and monitor.goal_snapshot.get("status") != "complete"
            ):
                filters.append(models.NativeGoalHistory.goal_key != monitor.goal_key)
            total = (
                session.scalar(
                    select(func.count()).select_from(models.NativeGoalHistory).where(*filters)
                )
                or 0
            )
            rows = session.scalars(
                select(models.NativeGoalHistory)
                .where(*filters)
                .order_by(
                    models.NativeGoalHistory.first_recorded_at.desc(),
                    models.NativeGoalHistory.goal_key.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
            items = [self._read(row) for row in rows]
            next_offset = offset + len(items)
            return schemas.NativeGoalHistoryPage(
                items=items,
                total=total,
                next_offset=next_offset if next_offset < total else None,
            )

    @staticmethod
    def _read(row: models.NativeGoalHistory) -> schemas.NativeGoalHistoryRead:
        goal = row.goal_snapshot
        return schemas.NativeGoalHistoryRead(
            goal_key=row.goal_key,
            thread_id=row.thread_id,
            objective=goal["objective"],
            status="cleared"
            if row.cleared_at and goal.get("status") != "complete"
            else goal.get("status", "unknown"),
            token_budget=goal.get("tokenBudget"),
            tokens_used=goal.get("tokensUsed"),
            time_used_seconds=goal.get("timeUsedSeconds"),
            goal_created_at=goal.get("createdAt"),
            goal_updated_at=goal.get("updatedAt"),
            first_recorded_at=_utc(row.first_recorded_at),
            last_recorded_at=_utc(row.last_recorded_at),
            cleared_at=_utc(row.cleared_at) if row.cleared_at else None,
            last_summary=row.last_summary,
            last_error=row.last_error,
            turn_id=row.turn_id,
            source=row.source,
        )
