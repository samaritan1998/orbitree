"""Reversible removal of legacy platform envelopes from idle native transcripts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from research_ops.services.instruction_cleanup import GENERATED_AGENTS

_DEVELOPER_PREFIXES = (
    "You are executing one Research Ops branch. ",
    "You are the persistent Research Ops Program Desk coordinator. ",
)
_ENVELOPE = re.compile(r"\A<(research_ops_[a-z_]+)>\n?(.*?)</\1>", re.DOTALL)
_PREFIXES = {
    "research_ops_policy": ("Fork is optional and user-controlled.",),
    "research_ops_handoff_policy": ("Research Ops Handoff is user-controlled.",),
    "research_ops_web_turn_limits": ("This is a managed Research Ops Web turn.",),
    "research_ops_brief": ("Work ID: ",),
    "research_ops_thread_recovery": ("The prior Codex thread is historical",),
    "research_ops_instructions": _DEVELOPER_PREFIXES,
}
_CONTINUATION = (
    "Continue this child Branch now. Work only on the fork objective below. "
    "Inspect the current child workspace and frozen parent context, then execute the "
    "objective instead of returning only a plan. Preserve evidence, report external job "
    "IDs and validation results, and leave a concise next action when the work is "
    "waiting.\n\nFork objective:\n"
)
_GOAL_PREFIX = "This is a scheduled checkpoint from the persistent Research Ops Goal worker. "
_BOOTSTRAP = (
    "\u8bf7\u8bfb\u53d6 Brief \u548c\u5f53\u524d\u5de5\u4f5c\u533a\uff0c"
    "\u7ed9\u51fa\u7b80\u77ed\u8ba1\u5212\u540e\u5f00\u59cb\u6267\u884c\u3002"
)
_MARKERS = ("<research_ops_", *_DEVELOPER_PREFIXES, _GOAL_PREFIX, "# Research Ops Branch")
_SCAN = (b"research_ops_", b"Research Ops", b"Continue this child Branch", b"# Program Desk")


def clean_prompt(text: str, literal_queries: frozenset[str] = frozenset()) -> str:
    """Only peel recognized, leading platform envelopes, never replace quoted text."""
    original = text
    if text in literal_queries:
        return text
    count = 0
    bootstrap = False
    while match := _ENVELOPE.match(text):
        name, body = match.group(1), match.group(2)
        recognized = body.startswith(_PREFIXES.get(name, ()))
        if name == "research_ops_brief" and bootstrap:
            try:
                brief = json.loads(body)
                recognized = isinstance(brief, dict) and "work_id" in brief
            except ValueError:
                pass
        if not recognized:
            break
        bootstrap |= name == "research_ops_instructions"
        rest = text[match.end() :]
        # Historical generators inserted at most two newlines, not arbitrary whitespace.
        if rest.startswith("\n\n"):
            rest = rest[2:]
        elif rest.startswith("\n"):
            rest = rest[1:]
        text = rest
        count += 1
        if text in literal_queries:
            return text
    if bootstrap and text == _BOOTSTRAP:
        return ""
    if text.startswith(_CONTINUATION):
        return text[len(_CONTINUATION) :]
    if text.startswith(_GOAL_PREFIX):
        _, start, rest = text.partition("\nObjective: ")
        objective, end, _ = rest.partition("\nCompletion contract: ")
        if start and end and "\nGoal ID: " in text:
            return objective
    return text if count else original


def _clean_agents(text: str) -> str:
    if text in GENERATED_AGENTS:
        return ""
    match = re.match(
        r"(# AGENTS\.md instructions for [^\n]+\s*<INSTRUCTIONS>\n)(.*?)(\n?</INSTRUCTIONS>)(.*)\Z",
        text,
        re.DOTALL,
    )
    if match and (match.group(2) in GENERATED_AGENTS or match.group(2) + "\n" in GENERATED_AGENTS):
        return match.group(4).removeprefix("\n")
    return text


def _clean_message(item: dict[str, Any], changes: Counter, literal_queries: frozenset[str]) -> None:
    role = item.get("role")
    if role not in ("user", "developer"):
        return
    parts = item.get("content")
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        before = part["text"]
        if role == "developer" and before.startswith(_DEVELOPER_PREFIXES):
            after = ""
            kind = "developer_instructions"
        elif role == "user":
            after = (
                before
                if before in literal_queries
                else _clean_agents(clean_prompt(before, literal_queries))
            )
            kind = "user_envelope"
        else:
            continue
        if before != after:
            part["text"] = after
            changes[kind] += 1


def clean_record(record: dict[str, Any], literal_queries: frozenset[str] = frozenset()) -> Counter:
    changes: Counter = Counter()
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return changes
    kind = record.get("type")
    if kind == "response_item":
        _clean_message(payload, changes, literal_queries)
    elif kind == "event_msg":
        if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
            before = payload["message"]
            payload["message"] = clean_prompt(before, literal_queries)
            if before != payload["message"]:
                changes["user_event"] += 1
        if isinstance(payload.get("item"), dict):
            item = payload["item"]
            if item.get("type") in ("userMessage", "user_message"):
                _clean_message({**item, "role": "user"}, changes, literal_queries)
    elif kind == "compacted":
        for item in payload.get("replacement_history") or []:
            if isinstance(item, dict):
                _clean_message(item, changes, literal_queries)
    elif kind == "world_state":
        state = payload.get("state") or {}
        agents = state.get("agents_md") or {}
        if isinstance(agents, dict) and isinstance(agents.get("text"), str):
            before = agents["text"]
            agents["text"] = _clean_agents(before)
            if before != agents["text"]:
                changes["generated_agents"] += 1
    for key in ("developer_instructions", "developerInstructions"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(_DEVELOPER_PREFIXES):
            payload[key] = ""
            changes["developer_instructions"] += 1
    return changes


def rewrite_line(
    line: bytes, literal_queries: frozenset[str] = frozenset()
) -> tuple[bytes, Counter, bool]:
    if not line.endswith(b"\n"):
        raise ValueError("Incomplete native transcript record")
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("Invalid native transcript record")
    if not any(marker in line for marker in _SCAN):
        return line, Counter(), False
    changes = clean_record(record, literal_queries)
    if changes:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > len(line) - 1:
            raise ValueError("Sanitized record would change native byte offsets")
        # Native forks and paginated history refer to both ordinals AND byte offsets.
        # JSON permits trailing whitespace, so preserve every original line boundary.
        result = encoded + b" " * (len(line) - len(encoded) - 1) + b"\n"
    else:
        result = line
    encoded_record = json.dumps(record, ensure_ascii=False) if changes else line.decode()
    remaining = any(marker in encoded_record for marker in _MARKERS)
    return result, changes, remaining


@contextmanager
def writer_guards(sqlite_home: Path, thread_ids: list[str]):
    """Use the same advisory writer locks as the native CLI; never unlink lock files."""
    with ExitStack() as stack:
        directory = sqlite_home / "thread-writer-locks"
        directory.mkdir(parents=True, exist_ok=True)
        for thread_id in sorted(set(thread_ids)):
            if not re.fullmatch(r"[a-zA-Z0-9-]+", thread_id):
                raise ValueError("Invalid native thread ID")
            handle = stack.enter_context((directory / f"{thread_id}.lock").open("a+b"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BlockingIOError(f"Active native writer: {thread_id}") from exc
        yield


def clean_rollout(
    path: Path,
    *,
    thread_id: str,
    sqlite_home: Path,
    backup_dir: Path | None = None,
    dependent_threads: list[str] | None = None,
    literal_queries: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Audit by default; applying requires backups and exclusive native writer locks."""
    entry: dict[str, Any] = {"thread_id": thread_id, "path": str(path)}
    if path.is_symlink() or not path.is_file():
        return {**entry, "status": "unavailable"}
    try:
        with writer_guards(sqlite_home, [thread_id, *(dependent_threads or [])]):
            stat = path.stat()
            changed = Counter()
            remaining = 0
            opaque_compactions = 0
            source_hash = hashlib.sha256()
            result_hash = hashlib.sha256()
            with tempfile.TemporaryFile() as output, path.open("rb") as source:
                first = json.loads(source.readline())
                if first.get("type") != "session_meta" or first["payload"].get("id") != thread_id:
                    raise ValueError("Native transcript identity mismatch")
                source.seek(0)
                for line in source:
                    source_hash.update(line)
                    if b'"compacted"' in line and b'"encrypted_content"' in line:
                        opaque_compactions += 1
                    replacement, changes, residual = rewrite_line(line, literal_queries)
                    changed.update(changes)
                    remaining += residual
                    result_hash.update(replacement)
                    if backup_dir is not None:
                        output.write(replacement)
                entry.update(
                    changes=dict(changed),
                    remaining_marker_records=remaining,
                    sha256=source_hash.hexdigest(),
                    cleaned_sha256=result_hash.hexdigest(),
                    opaque_compaction_records=opaque_compactions,
                )
                if not changed or backup_dir is None:
                    return {**entry, "status": "needs_cleanup" if changed else "unchanged"}
                backup = backup_dir / thread_id / path.name
                backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if backup.exists():
                    raise ValueError("Backup exists; use a new backup directory")
                source.seek(0)
                with backup.open("xb") as saved:
                    os.fchmod(saved.fileno(), 0o600)
                    shutil.copyfileobj(source, saved)
                    saved.flush()
                    os.fsync(saved.fileno())
                fd, name = tempfile.mkstemp(prefix=".rops-clean-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as target:
                        os.fchmod(target.fileno(), stat.st_mode & 0o777)
                        output.seek(0)
                        shutil.copyfileobj(output, target)
                        target.flush()
                        os.fsync(target.fileno())
                    current = path.stat()
                    if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                        stat.st_ino,
                        stat.st_size,
                        stat.st_mtime_ns,
                    ):
                        raise ValueError("Transcript changed during cleanup")
                    receipt = {**entry, "status": "cleaned", "backup": str(backup)}
                    (backup.parent / "receipt.json").write_text(
                        json.dumps(receipt, indent=2) + "\n"
                    )
                    os.replace(name, path)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                finally:
                    Path(name).unlink(missing_ok=True)
                return receipt
    except BlockingIOError as exc:
        return {**entry, "status": "busy", "reason": str(exc)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**entry, "status": "error", "reason": str(exc)}


def restore_rollout(receipt_path: Path, *, sqlite_home: Path) -> None:
    """Restore a backup only if no new turn or other edit has occurred since cleanup."""
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("status") != "cleaned":
        raise ValueError("Not a completed cleanup receipt")
    path, backup = Path(receipt["path"]), Path(receipt["backup"])
    if path.is_symlink() or backup.is_symlink():
        raise ValueError("Refusing to restore through a symlink")
    with writer_guards(sqlite_home, [receipt["thread_id"]]):
        with path.open("rb") as current:
            digest = hashlib.file_digest(current, "sha256").hexdigest()
        if digest != receipt["cleaned_sha256"]:
            raise ValueError("Transcript changed after cleanup; refusing to discard new work")
        with backup.open("rb") as saved:
            digest = hashlib.file_digest(saved, "sha256").hexdigest()
        if digest != receipt["sha256"]:
            raise ValueError("Backup checksum mismatch")
        stat = path.stat()
        fd, name = tempfile.mkstemp(prefix=".rops-restore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as target, backup.open("rb") as saved:
                os.fchmod(target.fileno(), stat.st_mode & 0o777)
                shutil.copyfileobj(saved, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        finally:
            Path(name).unlink(missing_ok=True)
