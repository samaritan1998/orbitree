from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models
from research_ops.services.codex_events import redact


@dataclass(frozen=True)
class _ClaudeTurn:
    external_turn_id: str
    prompt: str
    final_answer: str
    model: str | None
    started_at: datetime
    completed_at: datetime


class ClaudeResultSync:
    """Import completed turns from one explicitly managed Claude session."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        projects_root: Path,
        *,
        max_line_bytes: int,
        notification_recipient: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.projects_root = projects_root
        self.max_line_bytes = max_line_bytes
        self.notification_recipient = str(notification_recipient or "").strip() or None

    def session_path(self, thread_id: str) -> Path | None:
        matches = list(self.projects_root.glob(f"*/{thread_id}.jsonl"))
        return max(matches, key=lambda path: path.stat().st_mtime, default=None)

    def sync_session(
        self,
        *,
        branch_id: str,
        session_id: str,
        thread_id: str,
        started_at: datetime,
    ) -> list[models.CodexTurn]:
        path = self.session_path(thread_id)
        if path is None:
            return []
        turns = self._read_turns(path, started_at=started_at)
        if not turns:
            return []
        inserted: list[models.CodexTurn] = []
        with self.session_factory() as session:
            binding = session.scalar(
                select(models.CodexThreadBinding).where(
                    models.CodexThreadBinding.thread_id == thread_id
                )
            )
            if binding is None:
                binding = models.CodexThreadBinding(
                    branch_id=branch_id,
                    initial_session_id=session_id,
                    thread_id=thread_id,
                    source="claude-terminal",
                    bound_at=started_at,
                )
                session.add(binding)
                session.flush()
            elif binding.branch_id != branch_id:
                raise RuntimeError("Claude session is already bound to another Branch")
            attempt = session.get(models.SessionAttempt, session_id)
            for parsed in turns:
                existing = session.scalar(
                    select(models.CodexTurn).where(
                        models.CodexTurn.external_turn_id == parsed.external_turn_id
                    )
                )
                if existing is not None:
                    continue
                turn = models.CodexTurn(
                    branch_id=branch_id,
                    session_id=session_id,
                    thread_binding_id=binding.id,
                    thread_id=thread_id,
                    external_turn_id=parsed.external_turn_id,
                    status="completed",
                    prompt=redact(parsed.prompt),
                    final_answer=redact(parsed.final_answer),
                    started_at=parsed.started_at,
                    completed_at=parsed.completed_at,
                    duration_ms=max(
                        0,
                        int((parsed.completed_at - parsed.started_at).total_seconds() * 1000),
                    ),
                    metadata_json={
                        "runtime": "claude",
                        "source": "claude_session_jsonl",
                        "rollout_path": str(path),
                        "execution": {
                            "provider": "claude-cli",
                            "model": parsed.model,
                        },
                    },
                )
                try:
                    with session.begin_nested():
                        session.add(turn)
                        session.flush()
                except IntegrityError:
                    continue
                inserted.append(turn)
                if attempt is not None:
                    attempt.last_turn_id = parsed.external_turn_id
                if self.notification_recipient:
                    session.add(
                        models.TurnNotification(
                            turn_id=turn.id,
                            channel="infoflow",
                            recipient=self.notification_recipient,
                        )
                    )
            session.commit()
        return inserted

    def _read_turns(self, path: Path, *, started_at: datetime) -> list[_ClaudeTurn]:
        threshold = _as_utc(started_at) - timedelta(seconds=3)
        turns: list[_ClaudeTurn] = []
        current: dict[str, Any] | None = None
        with path.open("rb") as handle:
            for raw_line in handle:
                if len(raw_line) > self.max_line_bytes:
                    continue
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if event.get("isSidechain"):
                    continue
                event_type = event.get("type")
                message = event.get("message") or {}
                timestamp = _timestamp(event.get("timestamp"))
                if event_type == "user" and isinstance(message.get("content"), str):
                    if current is not None:
                        completed = self._complete(current)
                        if completed is not None:
                            turns.append(completed)
                    current = {
                        "prompt": message["content"],
                        "started_at": timestamp,
                        "answer": "",
                        "answer_id": None,
                        "model": None,
                        "completed_at": timestamp,
                    }
                    continue
                if event_type != "assistant" or current is None:
                    continue
                texts = [
                    str(block.get("text") or "")
                    for block in message.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(texts).strip()
                if text:
                    current["answer"] = text
                    current["answer_id"] = str(message.get("id") or event.get("uuid") or "")
                    current["model"] = str(message.get("model") or "").strip() or None
                    current["completed_at"] = timestamp
            if current is not None:
                completed = self._complete(current)
                if completed is not None:
                    turns.append(completed)
        return [turn for turn in turns if turn.started_at >= threshold]

    @staticmethod
    def _complete(raw: dict[str, Any]) -> _ClaudeTurn | None:
        answer = str(raw.get("answer") or "").strip()
        turn_id = str(raw.get("answer_id") or "").strip()
        if not answer or not turn_id:
            return None
        return _ClaudeTurn(
            external_turn_id=turn_id,
            prompt=str(raw.get("prompt") or ""),
            final_answer=answer,
            model=raw.get("model"),
            started_at=_as_utc(raw["started_at"]),
            completed_at=_as_utc(raw["completed_at"]),
        )


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
