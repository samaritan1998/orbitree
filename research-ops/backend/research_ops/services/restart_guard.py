from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_ops import models


@dataclass(frozen=True, order=True)
class RestartBlocker:
    label: str
    reason: str
    branch_id: str = ""
    thread_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@contextmanager
def idle_restart_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("An idle-restart monitor is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class RestartGuard:
    """Read-only evidence for a deferred restart; never reconcile or cancel work."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        proc_root: Path = Path("/proc"),
        max_tail_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.session_factory = session_factory
        self.proc_root = proc_root
        self.max_tail_bytes = max_tail_bytes
        self._tails: dict[Path, tuple[tuple[int, ...], str]] = {}

    def blockers(self) -> tuple[RestartBlocker, ...]:
        blockers: set[RestartBlocker] = set()
        writers = tuple(self._codex_writers())
        owned_threads = {
            thread_id
            for _, paths, _, locked_threads in writers
            for thread_id in {*locked_threads, *(path.stem[-36:] for path in paths)}
        }
        with self.session_factory() as session:
            names = {
                branch_id: (f"{human_id} / {title} / {name}", thread_id)
                for branch_id, name, thread_id, human_id, title in session.execute(
                    select(
                        models.ExecutionBranch.id,
                        models.ExecutionBranch.name,
                        models.ExecutionBranch.codex_thread_id,
                        models.WorkItem.human_id,
                        models.WorkItem.title,
                    ).join(models.WorkItem)
                )
            }
            threads = {
                thread_id: branch_id
                for thread_id, branch_id in session.execute(
                    select(models.CodexThreadBinding.thread_id, models.CodexThreadBinding.branch_id)
                )
            }
            threads.update({thread: bid for bid, (_, thread) in names.items() if thread})
            current_threads = {thread for _, thread in names.values() if thread}

            def add_branch(branch_id: str, reason: str, thread_id: str = "") -> None:
                label, current_thread = names.get(branch_id, (branch_id, ""))
                blockers.add(RestartBlocker(
                    label, reason, branch_id, thread_id or current_thread or ""
                ))

            for branch_id in session.scalars(
                select(models.ConversationMessage.branch_id)
                .where(
                    models.ConversationMessage.role == "user",
                    models.ConversationMessage.metadata_json["send_status"].as_string()
                    == "pending",
                )
                .distinct()
            ):
                add_branch(branch_id, "web_request_pending")
            for branch_id in session.scalars(
                select(models.SessionAttempt.branch_id)
                .where(
                    models.SessionAttempt.lease_owner == "web",
                    models.SessionAttempt.status.in_(["starting", "active"]),
                    models.SessionAttempt.lease_expires_at > datetime.now(UTC),
                )
                .distinct()
            ):
                add_branch(branch_id, "web_turn_lease_active")
            for goal in session.scalars(select(models.NativeGoalMonitor)):
                snapshot = goal.goal_snapshot or {}
                if snapshot.get("status") == "active" or goal.next_retry_at is not None:
                    current_thread = names.get(goal.branch_id, ("", ""))[1]
                    # A replaced thread is no longer a supervisor target. Its
                    # historical snapshot is not live work without an owner.
                    if (
                        current_thread != goal.thread_id
                        and goal.thread_id not in current_threads
                        and goal.thread_id not in owned_threads
                    ):
                        continue
                    reason = (
                        "native_goal_thread_mismatch"
                        if current_thread != goal.thread_id
                        else "native_goal_active_or_retrying"
                    )
                    add_branch(goal.branch_id, reason, goal.thread_id)
            for branch_id in session.scalars(
                select(models.GoalRun.branch_id).where(
                    models.GoalRun.executor == "agent", models.GoalRun.status == "active"
                )
            ):
                add_branch(branch_id, "background_goal_active")

        # Open writer files also cover local CLI and App Server turns that have
        # no Web lease. A terminal merely being open is not a running turn.
        for pid, paths, needs_rollout, locked_threads in writers:
            if needs_rollout and not paths:
                blockers.add(RestartBlocker(f"Codex PID {pid}", "writer_starting_or_unverified"))
            for thread_id in locked_threads - {path.stem[-36:] for path in paths}:
                branch_id = threads.get(thread_id, "")
                label = names.get(branch_id, (f"Codex session {thread_id}", ""))[0]
                blockers.add(RestartBlocker(
                    label, "codex_writer_unverified", branch_id, thread_id
                ))
            for path in paths:
                state = self._rollout_state(path)
                if state == "idle":
                    continue
                thread_id = path.stem[-36:]
                branch_id = threads.get(thread_id, "")
                label = names.get(branch_id, (f"Codex session {thread_id}", ""))[0]
                blockers.add(RestartBlocker(label, f"codex_turn_{state}", branch_id, thread_id))
        return tuple(sorted(blockers))

    def _codex_writers(self) -> Iterator[tuple[int, list[Path], bool, set[str]]]:
        for directory in self.proc_root.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                args = (directory / "cmdline").read_bytes().split(b"\0")
                if not args or Path(os.fsdecode(args[0])).name != "codex":
                    continue
                paths = []
                locked_threads: set[str] = set()
                for fd in (directory / "fd").iterdir():
                    try:
                        target = Path(os.readlink(fd))
                    except FileNotFoundError:
                        continue
                    if target.name.startswith("rollout-") and target.suffix == ".jsonl":
                        paths.append(target)
                    elif target.parent.name == "thread-writer-locks" and target.suffix == ".lock":
                        locked_threads.add(target.stem)
                # A metadata-only App Server may have no loaded thread. CLI
                # startup, in contrast, cannot be certified idle without a log.
                yield int(directory.name), paths, b"app-server" not in args, locked_threads
            except FileNotFoundError:
                continue

    def _rollout_state(self, path: Path) -> str:
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = self._tails.get(path)
            if cached and cached[0] == signature:
                return cached[1]
            with path.open("rb") as handle:
                start = max(0, stat.st_size - self.max_tail_bytes)
                handle.seek(start)
                if start:
                    handle.readline(self.max_tail_bytes)
                chunk = handle.read(self.max_tail_bytes)
                after = os.fstat(handle.fileno())
            if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                return "unverified"
            if not chunk or not chunk.endswith(b"\n"):
                return "unverified"
            state = "unverified" if start else "idle"
            turn_id: str | None = None
            for line in chunk.splitlines():
                record = json.loads(line)
                if not isinstance(record, dict):
                    return "unverified"
                if record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    return "unverified"
                event = payload.get("type")
                if event == "task_started":
                    turn_id = payload.get("turn_id")
                    state = "running"
                elif event in {"task_complete", "turn_aborted"}:
                    if turn_id is None or payload.get("turn_id") == turn_id:
                        turn_id = None
                        state = "idle"
            self._tails[path] = (signature, state)
            return state
        except (OSError, ValueError):
            return "unverified"
