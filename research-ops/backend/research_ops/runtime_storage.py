"""Bootstrap local runtime storage without changing the durable catalog location."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from research_ops.settings import Settings


def prepare_runtime_database(settings: Settings) -> bool:
    """Seed an empty local SQLite runtime database from the prior catalog.

    A SQLite online backup reads a consistent view even if the old service left
    WAL files behind. Existing local runtime state always wins, so routine
    service restarts never replace newer local state with a stale CFS copy.
    """

    return seed_runtime_database(settings, replace=False)


def seed_runtime_database(settings: Settings, *, replace: bool) -> bool:
    """Copy the durable SQLite catalog to local runtime storage.

    ``replace`` is reserved for the explicit, idle-only migration command. A
    normal service start never overwrites a runtime database that may contain
    newer state than the previous CFS snapshot.
    """

    if not settings.uses_separate_runtime_storage:
        return False
    source = settings.data_dir / "research-ops.db"
    target = settings.runtime_data_dir / "research-ops.db"
    if not source.is_file() or (target.exists() and not replace):
        return False
    _backup_sqlite(source, target)
    return True


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True) as reader,
            sqlite3.connect(temporary) as writer,
        ):
            reader.backup(writer)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
