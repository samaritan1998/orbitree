from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest
from research_ops.db import Database, run_db
from research_ops.settings import Settings
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError


def test_init_upgrades_legacy_message_idempotency_index(tmp_path) -> None:
    database = Database(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
        )
    )
    database.init()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_message_external_turn")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_message_external "
            "ON conversation_messages (branch_id, external_item_id)"
        )

    database.init()

    indexes = {item["name"]: item for item in inspect(database.engine).get_indexes(
        "conversation_messages"
    )}
    assert "uq_message_external" not in indexes
    assert indexes["uq_message_external_turn"]["column_names"] == [
        "branch_id",
        "turn_id",
        "external_item_id",
    ]
    work_columns = {
        item["name"] for item in inspect(database.engine).get_columns("work_items")
    }
    assert "hidden" in work_columns
    codex_window_columns = {
        item["name"] for item in inspect(database.engine).get_columns("codex_windows")
    }
    assert "parent_thread_id" in codex_window_columns
    assert {"kind", "display_name", "title_source"} <= codex_window_columns
    branch_columns = {
        item["name"] for item in inspect(database.engine).get_columns("execution_branches")
    }
    assert "workspace_path" in branch_columns
    assert "hidden" in branch_columns
    assert "turn_notifications" in inspect(database.engine).get_table_names()
    assert "conversation_failure_notifications" in inspect(database.engine).get_table_names()


def test_sqlite_runtime_preserves_parallel_reads_and_busy_timeout(tmp_path) -> None:
    database = Database(
        Settings(
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        )
    )

    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
        with database.engine.connect() as another:
            assert another.exec_driver_sql("SELECT 1").scalar() == 1


async def test_db_retry_runs_off_event_loop_and_retries_only_busy_transactions() -> None:
    main_thread = threading.get_ident()
    calls = 0

    def operation() -> str:
        nonlocal calls
        assert threading.get_ident() != main_thread
        calls += 1
        if calls < 3:
            raise OperationalError(
                "UPDATE test", {}, sqlite3.OperationalError("database is locked")
            )
        return "saved"

    assert await run_db(operation) == "saved"
    assert calls == 3


async def test_db_wait_does_not_stall_event_loop(tmp_path) -> None:
    database = Database(Settings(database_url=f"sqlite:///{tmp_path / 'busy.db'}"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE test (value INTEGER)")
    blocker = sqlite3.connect(tmp_path / "busy.db")
    blocker.execute("BEGIN IMMEDIATE")
    started = threading.Event()

    def write() -> None:
        started.set()
        with database.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO test VALUES (1)")

    task = asyncio.create_task(run_db(write))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        blocker.rollback()
        blocker.close()
    await asyncio.wait_for(task, timeout=2)


async def test_non_lock_db_errors_are_not_retried() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise OperationalError("SELECT missing", {}, sqlite3.OperationalError("no such table: x"))

    with pytest.raises(OperationalError, match="no such table"):
        await run_db(operation)
    assert calls == 1
