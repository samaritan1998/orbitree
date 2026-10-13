from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.codex.rollout import (
    LiveRolloutEvent,
    RolloutFormatError,
    find_turn_start_offset,
    read_live_rollout,
)
from research_ops.services.codex_events import redact


@dataclass(frozen=True)
class LocalLiveEnvelope:
    branch_id: str
    event: dict[str, Any]


@dataclass
class _TailState:
    branch_id: str
    thread_id: str
    path: Path
    st_dev: int
    st_ino: int
    offset: int
    turn_id: str
    current_turn_id: str | None
    completed: bool = False
    pending_calls: dict[str, dict[str, str]] = field(default_factory=dict)


class LocalCodexLiveSync:
    """Passively mirror visible events from active local Codex JSONL sessions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_line_bytes: int,
        replay_bytes: int,
        max_events: int,
        max_content_chars: int,
    ) -> None:
        self.session_factory = session_factory
        self.max_line_bytes = max_line_bytes
        self.replay_bytes = replay_bytes
        self.max_events = max_events
        self.max_content_chars = max_content_chars
        self._tails: dict[str, _TailState] = {}
        self._buffers: dict[str, OrderedDict[str, dict[str, Any]]] = {}
        self._active_branches: set[str] = set()
        self._lock = RLock()

    def poll(self) -> list[LocalLiveEnvelope]:
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(models.CodexThreadBinding, models.CodexSyncCursor)
                    .join(
                        models.CodexSyncCursor,
                        models.CodexSyncCursor.thread_binding_id
                        == models.CodexThreadBinding.id,
                    )
                    .where(models.CodexThreadBinding.status == "current")
                )
            )

        emitted: list[LocalLiveEnvelope] = []
        binding_ids = {binding.id for binding, _cursor in rows}
        with self._lock:
            self._prune(binding_ids)
            for binding, cursor in rows:
                try:
                    emitted.extend(self._poll_binding(binding, cursor))
                except (OSError, RolloutFormatError):
                    continue
        return emitted

    def snapshot(self, branch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            if branch_id not in self._active_branches:
                return []
            return [dict(event) for event in self._buffers.get(branch_id, {}).values()]

    def _poll_binding(
        self,
        binding: models.CodexThreadBinding,
        cursor: models.CodexSyncCursor,
    ) -> list[LocalLiveEnvelope]:
        pending = _latest_pending(cursor.pending_json)
        tail = self._tails.get(binding.id)
        if tail is None and pending is None:
            return []

        path = Path(cursor.path)
        stat = path.stat()
        if tail is None or tail.path != path or (tail.st_dev, tail.st_ino) != (
            stat.st_dev,
            stat.st_ino,
        ):
            if pending is None:
                return []
            turn_id, item = pending
            offset = self._initial_offset(path, cursor, turn_id, item)
            replaying_start = offset < cursor.byte_offset
            tail = _TailState(
                branch_id=binding.branch_id,
                thread_id=binding.thread_id,
                path=path,
                st_dev=stat.st_dev,
                st_ino=stat.st_ino,
                offset=offset,
                turn_id=turn_id,
                current_turn_id=None if replaying_start else turn_id,
            )
            self._tails[binding.id] = tail
            self._begin_turn(tail.branch_id)
            emitted = [] if replaying_start else [self._turn_event(tail, "started")]
        else:
            emitted = []
            if pending is not None and pending[0] != tail.turn_id:
                turn_id, item = pending
                offset = self._initial_offset(path, cursor, turn_id, item)
                replaying_start = offset < cursor.byte_offset
                tail.offset = offset
                tail.turn_id = turn_id
                tail.current_turn_id = None if replaying_start else turn_id
                tail.completed = False
                tail.pending_calls.clear()
                self._begin_turn(tail.branch_id)
                if not replaying_start:
                    emitted.append(self._turn_event(tail, "started"))
            elif tail.completed:
                if pending is None:
                    self._tails.pop(binding.id, None)
                return emitted

        if tail.offset == stat.st_size:
            return emitted

        batch = read_live_rollout(
            tail.path,
            offset=tail.offset,
            current_turn_id=tail.current_turn_id,
            pending_calls=tail.pending_calls,
            max_line_bytes=self.max_line_bytes,
        )
        tail.offset = batch.next_offset
        tail.st_dev = batch.st_dev
        tail.st_ino = batch.st_ino
        tail.current_turn_id = batch.current_turn_id
        tail.pending_calls = batch.pending_calls

        for event in batch.events:
            envelope = self._normalize(tail, event)
            if envelope is None:
                continue
            emitted.append(envelope)
            if event.event_type == "turn.started":
                tail.turn_id = event.turn_id or tail.turn_id
                tail.completed = False
                self._begin_turn(tail.branch_id)
            elif event.event_type == "turn.completed":
                tail.completed = True
                self._active_branches.discard(tail.branch_id)

        if pending is None and tail.current_turn_id is not None and tail.offset >= stat.st_size:
            tail.completed = True
            self._active_branches.discard(tail.branch_id)
        return emitted

    def _initial_offset(
        self,
        path: Path,
        cursor: models.CodexSyncCursor,
        turn_id: str,
        pending: dict[str, Any],
    ) -> int:
        value = pending.get("start_offset")
        if isinstance(value, int) and 0 <= value <= path.stat().st_size:
            return value
        recovered = find_turn_start_offset(
            path,
            turn_id,
            max_scan_bytes=self.replay_bytes,
            max_line_bytes=self.max_line_bytes,
        )
        return recovered if recovered is not None else cursor.byte_offset

    def _normalize(
        self, tail: _TailState, event: LiveRolloutEvent
    ) -> LocalLiveEnvelope | None:
        if event.event_type == "turn.started":
            return LocalLiveEnvelope(
                branch_id=tail.branch_id,
                event={
                    "type": "local.turn.started",
                    "turn_id": event.turn_id,
                    "source": "local_cli",
                },
            )
        if event.event_type == "turn.completed":
            return LocalLiveEnvelope(
                branch_id=tail.branch_id,
                event={
                    "type": "local.turn.completed",
                    "turn_id": event.turn_id,
                    "status": event.status,
                    "source": "local_cli",
                },
            )
        if event.event_type != "activity" or event.role is None or event.kind is None:
            return None
        content = redact(event.content)
        if len(content) > self.max_content_chars:
            content = f"{content[: self.max_content_chars]}\n[output truncated]"
        normalized = {
            "id": f"local:{tail.thread_id}:{event.event_id}",
            "turn_id": event.turn_id,
            "role": event.role,
            "kind": event.kind,
            "content": content,
            "created_at": event.created_at.isoformat(),
            "status": event.status,
            "source": "local_cli",
        }
        self._store(tail.branch_id, normalized)
        return LocalLiveEnvelope(
            branch_id=tail.branch_id,
            event={"type": "local.live", "event": normalized},
        )

    def _store(self, branch_id: str, event: dict[str, Any]) -> None:
        buffer = self._buffers.setdefault(branch_id, OrderedDict())
        event_id = str(event["id"])
        if event_id in buffer:
            previous = buffer.pop(event_id)
            event["created_at"] = previous.get("created_at", event["created_at"])
        buffer[event_id] = event
        while len(buffer) > self.max_events:
            buffer.popitem(last=False)

    def _begin_turn(self, branch_id: str) -> None:
        self._buffers[branch_id] = OrderedDict()
        self._active_branches.add(branch_id)

    @staticmethod
    def _turn_event(tail: _TailState, action: str) -> LocalLiveEnvelope:
        return LocalLiveEnvelope(
            branch_id=tail.branch_id,
            event={
                "type": f"local.turn.{action}",
                "turn_id": tail.turn_id,
                "source": "local_cli",
            },
        )

    def _prune(self, binding_ids: set[str]) -> None:
        stale = [binding_id for binding_id in self._tails if binding_id not in binding_ids]
        for binding_id in stale:
            tail = self._tails.pop(binding_id)
            self._active_branches.discard(tail.branch_id)
            self._buffers.pop(tail.branch_id, None)


def _latest_pending(value: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    candidates = [
        (turn_id, item)
        for turn_id, item in value.items()
        if isinstance(turn_id, str) and isinstance(item, dict)
    ]
    return candidates[-1] if candidates else None
