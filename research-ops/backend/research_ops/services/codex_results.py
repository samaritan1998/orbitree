from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.codex.rollout import (
    PARSER_VERSION,
    CompletedTurn,
    RolloutBatch,
    find_rollout,
    read_rollout,
    user_visible_prompt,
)
from research_ops.db import is_sqlite_busy
from research_ops.services.codex_events import redact

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncedResult:
    branch_id: str
    turn: models.CodexTurn


class CodexResultSync:
    """Reconcile local Codex rollout files into durable, branch-owned turns."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sessions_root: Path,
        *,
        max_line_bytes: int = 8 * 1024 * 1024,
        notification_recipient: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.sessions_root = sessions_root
        self.max_line_bytes = max_line_bytes
        self.notification_recipient = str(notification_recipient or "").strip() or None
        self._files: dict[str, Path] = {}
        self._backfilled_turn_keys: set[tuple[str, str]] = set()
        self._backfill_state_lock = Lock()
        self._last_binding_refresh_at: float | None = None
        self._binding_refresh_interval_seconds = 30.0
        self._sync_lock = Lock()

    def sync_all(self) -> list[SyncedResult]:
        if not self._sync_lock.acquire(blocking=False):
            return []
        try:
            return self._sync_all_unlocked()
        finally:
            self._sync_lock.release()

    def _sync_all_unlocked(self) -> list[SyncedResult]:
        if not self.sessions_root.exists():
            return []
        now = monotonic()
        if (
            self._last_binding_refresh_at is None
            or now - self._last_binding_refresh_at >= self._binding_refresh_interval_seconds
        ):
            self.ensure_bindings()
            self._last_binding_refresh_at = now
        with self.session_factory() as session:
            binding_ids = list(
                session.scalars(
                    select(models.CodexThreadBinding.id).order_by(
                        models.CodexThreadBinding.bound_at
                    )
                )
            )
        synced: list[SyncedResult] = []
        for binding_id in binding_ids:
            try:
                synced.extend(self._sync_binding(binding_id))
            except Exception as exc:
                if is_sqlite_busy(exc):
                    # The next pass resumes from the unchanged durable cursor.
                    # Writing the same lock error back to SQLite compounds it.
                    logger.warning("Codex result sync delayed by SQLite for %s", binding_id)
                    break
                self._record_error(binding_id, str(exc))
        return synced

    def backfill_missing_prompts(
        self, *, branch_id: str | None = None, repair_mismatched: bool = False
    ) -> int:
        """Recover missing or control-plane polluted display queries.

        The API runs this infrequently in the background. It is idempotent and
        also repairs historical turns whose JSONL parser captured an internal
        Web prompt or AGENTS.md context instead of the user's saved query.
        """

        if repair_mismatched and not branch_id:
            raise ValueError("Pairing repair requires an explicit Branch")
        with self._backfill_state_lock:
            self._backfilled_turn_keys.clear()
        prompt_needs_recovery = or_(
            models.CodexTurn.prompt.is_(None),
            models.CodexTurn.prompt == "",
            models.CodexTurn.prompt.like("# AGENTS.md instructions for %"),
            models.CodexTurn.prompt.like("<research_ops_%"),
        )
        with self.session_factory() as session:
            query = (
                select(models.CodexThreadBinding.id)
                .join(models.CodexTurn)
            )
            if not repair_mismatched:
                query = query.where(prompt_needs_recovery)
            if branch_id:
                query = query.where(models.CodexThreadBinding.branch_id == branch_id)
            binding_ids = list(session.scalars(query.distinct()))

        updated = 0
        # Several Branch bindings can point at one long-lived Codex thread.
        # Parse each session file once per maintenance pass instead of doing a
        # full read for every Branch that happens to share it.
        batches: dict[str, RolloutBatch] = {}
        for binding_id in binding_ids:
            with self.session_factory() as session:
                binding = session.get(models.CodexThreadBinding, binding_id)
                if binding is None:
                    continue
                path = self._rollout_path(binding.thread_id)
                if path is None:
                    continue
                turn_query = select(models.CodexTurn).where(
                    models.CodexTurn.thread_binding_id == binding.id,
                )
                if not repair_mismatched:
                    turn_query = turn_query.where(prompt_needs_recovery)
                recoverable_ids = set(session.scalars(
                    turn_query.with_only_columns(models.CodexTurn.external_turn_id)
                ))
            if not recoverable_ids:
                continue

            cache_key = str(path)
            batch = batches.get(cache_key)
            if batch is None:
                batch = read_rollout(path, max_line_bytes=self.max_line_bytes)
                batches[cache_key] = batch
            recovered = {
                item.external_turn_id: item
                for item in batch.turns
                if item.external_turn_id in recoverable_ids
            }

            with self.session_factory() as session:
                binding = session.get(models.CodexThreadBinding, binding_id)
                if binding is None:
                    continue
                turns = list(session.scalars(turn_query))
                for turn in turns:
                    completed = recovered.get(turn.external_turn_id)
                    if completed is not None:
                        changed = self._upsert_turn(session, binding, completed, batch)
                        if changed is None:
                            continue
                    else:
                        # Without a raw turn, only an exact message id can repair
                        # a historical pairing; do not guess from an old Session.
                        prompt = self._conversation_prompt_for_id(
                            session, turn.branch_id, turn.external_turn_id
                        )
                        if not prompt or redact(prompt) == turn.prompt:
                            continue
                        turn.prompt = redact(prompt)
                        turn.metadata_json = {
                            **(turn.metadata_json or {}),
                            "prompt_source": "conversation_message_turn",
                            "prompt_kind": "user",
                        }
                    with self._backfill_state_lock:
                        self._backfilled_turn_keys.add((turn.branch_id, turn.external_turn_id))
                    updated += 1
                    if updated % 100 == 0:
                        session.commit()
                session.commit()
        return updated

    def take_backfilled_turn_keys(self) -> set[tuple[str, str]]:
        """Return and clear turns changed by the last backfill pass."""
        with self._backfill_state_lock:
            turn_keys = set(self._backfilled_turn_keys)
            self._backfilled_turn_keys.clear()
        return turn_keys

    @staticmethod
    def _conversation_prompt(session: Session, turn: models.CodexTurn) -> str | None:
        """Use the durable Web query when a rollout lacks its user record."""
        prompt, _source = CodexResultSync._resolved_prompt(
            session,
            branch_id=turn.branch_id,
            external_turn_id=turn.external_turn_id,
            session_id=turn.session_id,
            rollout_prompt=None,
        )
        return prompt

    @staticmethod
    def _conversation_prompt_for_session(
        session: Session, branch_id: str, session_id: str | None,
        completed_at: datetime | None = None,
    ) -> str | None:
        """Resolve the original Web query even when CLI and JSONL turn ids differ."""
        if not session_id:
            return None
        query = (
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == branch_id,
                models.ConversationMessage.session_id == session_id,
                models.ConversationMessage.role == "user",
                models.ConversationMessage.metadata_json["origin"]
                .as_string()
                .in_(("web", "continuation")),
            )
        )
        if completed_at is not None:
            query = query.where(models.ConversationMessage.created_at <= completed_at)
        messages = list(session.scalars(query.limit(2)))
        return user_visible_prompt(messages[0].content) if len(messages) == 1 else None

    @staticmethod
    def _conversation_prompt_for_id(
        session: Session, branch_id: str, external_turn_id: str
    ) -> str | None:
        """Resolve a visible Web query without rescanning the rollout file."""
        message = session.scalar(
            select(models.ConversationMessage)
            .where(
                models.ConversationMessage.branch_id == branch_id,
                models.ConversationMessage.turn_id == external_turn_id,
                models.ConversationMessage.role == "user",
            )
            .order_by(models.ConversationMessage.created_at.asc())
            .limit(1)
        )
        return user_visible_prompt(message.content) if message is not None else None

    @classmethod
    def _resolved_prompt(
        cls,
        session: Session,
        *,
        branch_id: str,
        external_turn_id: str,
        session_id: str | None,
        rollout_prompt: str | None,
        prompt_kind: str | None = None,
        completed_at: datetime | None = None,
    ) -> tuple[str | None, str | None]:
        """Prefer turn-scoped evidence; a Session can contain unrelated turns."""
        if prompt_kind == "goal_continuation":
            return user_visible_prompt(rollout_prompt), "codex_goal_continuation"
        prompt = cls._conversation_prompt_for_id(session, branch_id, external_turn_id)
        if prompt:
            return prompt, "conversation_message_turn"
        prompt = user_visible_prompt(rollout_prompt)
        if prompt:
            return prompt, "codex_session_jsonl"
        prompt = cls._conversation_prompt_for_session(
            session, branch_id, session_id, completed_at
        )
        if prompt:
            return prompt, "conversation_message_session"
        return None, None

    def ensure_bindings(self) -> None:
        with self.session_factory() as session:
            branches = list(
                session.scalars(
                    select(models.ExecutionBranch)
                    .where(models.ExecutionBranch.codex_thread_id.is_not(None))
                    .order_by(models.ExecutionBranch.created_at)
                )
            )
            for branch in branches:
                thread_id = branch.codex_thread_id
                if not thread_id or thread_id.startswith("deferred:"):
                    continue
                existing = session.scalar(
                    select(models.CodexThreadBinding).where(
                        models.CodexThreadBinding.thread_id == thread_id
                    )
                )
                current_bindings = list(
                    session.scalars(
                        select(models.CodexThreadBinding).where(
                            models.CodexThreadBinding.branch_id == branch.id,
                            models.CodexThreadBinding.status == "current",
                        )
                    )
                )
                for current in current_bindings:
                    if current.thread_id != thread_id:
                        current.status = "historical"
                        current.retired_at = _utc_now()
                if existing is not None:
                    if existing.branch_id == branch.id:
                        existing.status = "current"
                        existing.retired_at = None
                    continue
                attempt = session.scalar(
                    select(models.SessionAttempt)
                    .where(
                        models.SessionAttempt.branch_id == branch.id,
                        models.SessionAttempt.codex_thread_id == thread_id,
                    )
                    .order_by(models.SessionAttempt.started_at, models.SessionAttempt.created_at)
                    .limit(1)
                )
                session.add(
                    models.CodexThreadBinding(
                        branch_id=branch.id,
                        initial_session_id=attempt.id if attempt else None,
                        thread_id=thread_id,
                        source="reconciled",
                        bound_at=(
                            attempt.started_at
                            if attempt and attempt.started_at
                            else branch.created_at
                        ),
                    )
                )
            session.commit()

    def bind_thread(
        self,
        *,
        branch_id: str,
        session_id: str,
        thread_id: str,
        bound_at: datetime,
        source: str = "terminal",
    ) -> models.CodexThreadBinding:
        with self.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            attempt = session.get(models.SessionAttempt, session_id)
            if branch is None or attempt is None or attempt.branch_id != branch_id:
                raise RuntimeError("Cannot bind Codex thread to an unknown branch session")
            conflict = session.scalar(
                select(models.CodexThreadBinding).where(
                    models.CodexThreadBinding.thread_id == thread_id
                )
            )
            if conflict is not None and conflict.branch_id != branch_id:
                raise RuntimeError("Codex thread is already bound to another branch")
            current = session.scalar(
                select(models.CodexThreadBinding).where(
                    models.CodexThreadBinding.branch_id == branch_id,
                    models.CodexThreadBinding.status == "current",
                )
            )
            if current is not None and current.thread_id != thread_id:
                current.status = "historical"
                current.retired_at = _utc_now()
            binding = conflict or models.CodexThreadBinding(
                branch_id=branch_id,
                initial_session_id=session_id,
                thread_id=thread_id,
                source=source,
                bound_at=_as_utc(bound_at),
            )
            if conflict is None:
                session.add(binding)
            else:
                binding.status = "current"
            branch.codex_thread_id = thread_id
            attempt.codex_thread_id = thread_id
            session.commit()
            return binding

    def known_thread_ids(self) -> set[str]:
        with self.session_factory() as session:
            return set(session.scalars(select(models.CodexThreadBinding.thread_id)))

    def _sync_binding(self, binding_id: str) -> list[SyncedResult]:
        with self.session_factory() as session:
            binding = session.get(models.CodexThreadBinding, binding_id)
            if binding is None:
                return []
            thread_id = binding.thread_id
            path = self._rollout_path(thread_id)
            if path is None:
                return []
            cursor = session.scalar(
                select(models.CodexSyncCursor).where(
                    models.CodexSyncCursor.thread_binding_id == binding.id
                )
            )
            stat = path.stat()
            offset = 0
            pending_state: dict = {}
            parser_reset = bool(cursor and cursor.parser_version != PARSER_VERSION)
            file_identity_reset = bool(
                cursor and (cursor.st_dev != stat.st_dev or cursor.st_ino != stat.st_ino)
            )
            reset_cursor = parser_reset or file_identity_reset
            if (
                cursor
                and not reset_cursor
                and cursor.st_dev == stat.st_dev
                and cursor.st_ino == stat.st_ino
            ):
                offset = cursor.byte_offset
                pending_state = cursor.pending_json
                # Avoid parsing and committing an unchanged append-only JSONL
                # on every background tick. Active files still advance as soon
                # as their size changes.
                if offset == stat.st_size:
                    return []

        batch = read_rollout(
            path,
            offset=offset,
            pending_state=pending_state,
            max_line_bytes=self.max_line_bytes,
        )
        if batch.metadata.thread_id != thread_id:
            raise RuntimeError(f"Rollout thread mismatch for {path}")

        synced: list[SyncedResult] = []
        with self.session_factory() as session:
            binding = session.get(models.CodexThreadBinding, binding_id)
            assert binding is not None
            cursor = session.scalar(
                select(models.CodexSyncCursor).where(
                    models.CodexSyncCursor.thread_binding_id == binding.id
                )
            )
            if cursor is None:
                cursor = models.CodexSyncCursor(
                    thread_binding_id=binding.id,
                    path=str(path),
                    st_dev=batch.st_dev,
                    st_ino=batch.st_ino,
                    parser_version=PARSER_VERSION,
                )
                session.add(cursor)
            elif (
                cursor.byte_offset != offset and cursor.st_ino == batch.st_ino and not reset_cursor
            ):
                return []

            for completed in batch.turns:
                if not self._belongs_to_binding(completed, binding):
                    continue
                stored = self._upsert_turn(session, binding, completed, batch)
                if stored is not None:
                    synced.append(SyncedResult(binding.branch_id, stored))
            cursor.path = str(path)
            cursor.st_dev = batch.st_dev
            cursor.st_ino = batch.st_ino
            cursor.byte_offset = batch.next_offset
            cursor.parser_version = PARSER_VERSION
            cursor.last_event_id = (
                batch.turns[-1].external_turn_id if batch.turns else cursor.last_event_id
            )
            cursor.pending_json = _pending_after(batch.pending_state, binding.bound_at)
            parse_warning = (
                f"Skipped {batch.skipped_oversized_lines} oversized Codex JSONL "
                f"event(s) above {self.max_line_bytes} bytes"
                if batch.skipped_oversized_lines
                else None
            )
            cursor.last_error = parse_warning
            binding.last_error = parse_warning
            session.commit()
        return synced

    def _upsert_turn(
        self,
        session: Session,
        binding: models.CodexThreadBinding,
        completed: CompletedTurn,
        batch: RolloutBatch,
    ) -> models.CodexTurn | None:
        attempt = self._attempt_for_turn(session, binding, completed)
        session_id = attempt.id if attempt else None
        prompt, prompt_source = self._resolved_prompt(
            session,
            branch_id=binding.branch_id,
            external_turn_id=completed.external_turn_id,
            session_id=session_id,
            rollout_prompt=completed.prompt,
            prompt_kind=completed.prompt_kind,
            completed_at=completed.completed_at,
        )
        pairing = {
            "prompt_source": prompt_source,
            "prompt_kind": completed.prompt_kind or ("user" if prompt else None),
        }
        existing = session.scalar(
            select(models.CodexTurn).where(
                models.CodexTurn.external_turn_id == completed.external_turn_id
            )
        )
        if existing is not None:
            if existing.branch_id != binding.branch_id:
                binding.last_error = (
                    f"Turn {completed.external_turn_id} is already owned by branch "
                    f"{existing.branch_id}"
                )
                return None
            metadata = {**(existing.metadata_json or {}), **pairing}
            repaired_prompt = redact(prompt) if prompt else existing.prompt
            if (
                repaired_prompt != existing.prompt
                or existing.session_id != session_id
                or (completed.started_at is not None and (
                    existing.started_at is None
                    or _as_utc(existing.started_at) != completed.started_at
                ))
                or metadata != existing.metadata_json
            ):
                existing.prompt = repaired_prompt
                existing.session_id = session_id
                if completed.started_at is not None:
                    existing.started_at = completed.started_at
                existing.metadata_json = metadata
                return existing
            return None
        metadata = batch.metadata
        query = session.scalar(
            select(models.ConversationMessage).where(
                models.ConversationMessage.branch_id == binding.branch_id,
                models.ConversationMessage.turn_id == completed.external_turn_id,
                models.ConversationMessage.role == "user",
            ).order_by(models.ConversationMessage.created_at.desc()).limit(1)
        )
        if query is None and attempt is not None:
            # CLI tracking ids and persisted Codex turn ids may differ. Match
            # the durable query within this attempt before using its snapshot.
            query = session.scalar(
                select(models.ConversationMessage).where(
                    models.ConversationMessage.branch_id == binding.branch_id,
                    models.ConversationMessage.session_id == attempt.id,
                    models.ConversationMessage.role == "user",
                    models.ConversationMessage.content == redact(prompt or ""),
                    models.ConversationMessage.created_at <= completed.completed_at,
                ).order_by(models.ConversationMessage.created_at.desc()).limit(1)
            )
        execution = (
            (query.metadata_json.get("execution") if query else None)
            or (attempt.execution_config if attempt else {})
        )
        execution = dict(execution or {})
        if completed.model:
            execution["model"] = completed.model
        if completed.reasoning_effort:
            execution["reasoning_effort"] = completed.reasoning_effort
        turn = models.CodexTurn(
            branch_id=binding.branch_id,
            session_id=session_id,
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id=completed.external_turn_id,
            status="completed",
            prompt=redact(prompt) if prompt else None,
            final_answer=redact(completed.final_answer),
            started_at=completed.started_at,
            completed_at=completed.completed_at,
            duration_ms=completed.duration_ms,
            time_to_first_token_ms=completed.time_to_first_token_ms,
            metadata_json={
                "source": "codex_session_jsonl",
                "parser_version": PARSER_VERSION,
                "cli_version": metadata.cli_version,
                "forked_from_id": metadata.forked_from_id,
                **pairing,
                "execution": execution,
                **({key: query.metadata_json[key] for key in (
                    "scheduled_check_id", "scheduled_check_run_id",
                ) if key in query.metadata_json} if query else {}),
            },
        )
        try:
            with session.begin_nested():
                session.add(turn)
                session.flush()
        except IntegrityError:
            return None
        if attempt is not None:
            attempt.last_turn_id = completed.external_turn_id
        if self.notification_recipient and not (query and query.metadata_json.get(
            "scheduled_check_id"
        )):
            session.add(
                models.TurnNotification(
                    turn_id=turn.id,
                    channel="infoflow",
                    recipient=self.notification_recipient,
                )
            )
        return turn

    def _attempt_for_turn(
        self,
        session: Session,
        binding: models.CodexThreadBinding,
        completed: CompletedTurn,
    ) -> models.SessionAttempt | None:
        if completed.prompt_kind == "goal_continuation":
            return None
        attempts = list(
            session.scalars(
                select(models.SessionAttempt)
                .where(
                    models.SessionAttempt.branch_id == binding.branch_id,
                    models.SessionAttempt.codex_thread_id == binding.thread_id,
                )
                .order_by(models.SessionAttempt.started_at, models.SessionAttempt.created_at)
            )
        )
        turn_time = completed.started_at or completed.completed_at
        eligible = [
            attempt
            for attempt in attempts
            if _at_or_before(_as_utc(attempt.started_at or attempt.created_at), turn_time)
            and (attempt.completed_at is None or (
                _at_or_before(turn_time, _as_utc(attempt.completed_at))
            ))
        ]
        return eligible[-1] if eligible else None

    @staticmethod
    def _belongs_to_binding(completed: CompletedTurn, binding: models.CodexThreadBinding) -> bool:
        turn_time = completed.started_at or completed.completed_at
        boundary = _as_utc(binding.bound_at)
        # Codex rollout event timestamps are second-precision, while SQLite
        # preserves microseconds for the Orbitree binding time. A turn
        # started in the same second as the binding is the child turn, not a
        # historical parent turn; comparing raw datetimes would drop it.
        return turn_time.replace(microsecond=0) >= boundary.replace(microsecond=0)

    def _rollout_path(self, thread_id: str) -> Path | None:
        cached = self._files.get(thread_id)
        if cached and cached.exists():
            return cached
        path = find_rollout(self.sessions_root, thread_id)
        if path is not None:
            self._files[thread_id] = path
        return path

    def _record_error(self, binding_id: str, error: str) -> None:
        with self.session_factory() as session:
            binding = session.get(models.CodexThreadBinding, binding_id)
            if binding is not None:
                binding.last_error = error[:4000]
                cursor = session.scalar(
                    select(models.CodexSyncCursor).where(
                        models.CodexSyncCursor.thread_binding_id == binding_id
                    )
                )
                if cursor is not None:
                    cursor.last_error = error[:4000]
                session.commit()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _at_or_before(left: datetime, right: datetime) -> bool:
    if left.microsecond == 0 or right.microsecond == 0:
        return left.replace(microsecond=0) <= right.replace(microsecond=0)
    return left <= right


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _pending_after(pending_state: dict[str, dict], bound_at: datetime) -> dict[str, dict]:
    boundary = _as_utc(bound_at)
    filtered: dict[str, dict] = {}
    for turn_id, item in pending_state.items():
        raw_started_at = item.get("started_at")
        if not isinstance(raw_started_at, str):
            filtered[turn_id] = item
            continue
        try:
            started_at = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
        except ValueError:
            filtered[turn_id] = item
            continue
        if _as_utc(started_at) >= boundary:
            filtered[turn_id] = item
    return filtered
