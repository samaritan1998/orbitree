from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Generator

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import Settings

logger = logging.getLogger(__name__)


def is_sqlite_busy(error: BaseException) -> bool:
    if not isinstance(error, OperationalError) or not isinstance(error.orig, sqlite3.Error):
        return False
    code = getattr(error.orig, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(error.orig).lower() in {"database is locked", "database table is locked"}


async def run_db[**P, T](operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Retry only a database transaction, never the surrounding agent/tool operation."""
    for attempt in range(4):
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except OperationalError as exc:
            if not is_sqlite_busy(exc) or attempt == 3:
                raise
            logger.warning("SQLite busy; retrying %s (%s/3)", operation.__name__, attempt + 1)
            await asyncio.sleep(0.1 * 2**attempt)
    raise AssertionError("unreachable")


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings) -> None:
        is_sqlite = settings.resolved_database_url.startswith("sqlite")
        connect_args = {"check_same_thread": False, "timeout": 30.0} if is_sqlite else {}
        self.engine = create_engine(
            settings.resolved_database_url, connect_args=connect_args, pool_pre_ping=True
        )
        if is_sqlite:
            self._configure_sqlite(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    def init(self) -> None:
        from . import models  # noqa: F401

        Base.metadata.create_all(self.engine)
        self._migrate_message_idempotency_index()
        self._migrate_codex_cursor_pending()
        self._migrate_work_item_hidden()
        self._migrate_codex_window_lineage()
        self._migrate_branch_workspace_path()
        self._migrate_branch_continuation()
        self._migrate_execution_branch_hidden()
        self._migrate_codex_window_identity()
        self._migrate_agent_runtime()
        self._migrate_execution_identity()
        self._migrate_native_goal_tool_state()

    def _migrate_native_goal_tool_state(self) -> None:
        columns = {
            item["name"] for item in inspect(self.engine).get_columns("native_goal_monitors")
        }
        if "turn_tool_state" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE native_goal_monitors ADD COLUMN turn_tool_state JSON")
                )

    def _migrate_execution_identity(self) -> None:
        for table, additions in {
            "execution_branches": ("execution_preferences", "native_goal_execution"),
            "session_attempts": ("execution_config",),
        }.items():
            columns = {item["name"] for item in inspect(self.engine).get_columns(table)}
            with self.engine.begin() as connection:
                for column in additions:
                    if column not in columns:
                        connection.execute(
                            text(
                                f"ALTER TABLE {table} ADD COLUMN {column} "
                                "JSON NOT NULL DEFAULT '{}'"
                            )
                        )

    def _migrate_message_idempotency_index(self) -> None:
        """Scope CLI item ids to a turn and upgrade databases created before that fix."""
        table = Base.metadata.tables["conversation_messages"]
        turn_scoped_index = next(
            index for index in table.indexes if index.name == "uq_message_external_turn"
        )
        index_names = {
            item["name"] for item in inspect(self.engine).get_indexes("conversation_messages")
        }
        if "uq_message_external" in index_names:
            turn_scoped_index.name = "uq_message_external"
            try:
                turn_scoped_index.drop(self.engine, checkfirst=True)
            finally:
                turn_scoped_index.name = "uq_message_external_turn"
        turn_scoped_index.create(self.engine, checkfirst=True)

    def _migrate_codex_cursor_pending(self) -> None:
        if "codex_sync_cursors" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("codex_sync_cursors")}
        if "pending_json" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE codex_sync_cursors "
                    "ADD COLUMN pending_json JSON NOT NULL DEFAULT '{}'"
                )
            )

    def _migrate_work_item_hidden(self) -> None:
        if "work_items" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("work_items")}
        if "hidden" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE work_items ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_work_items_hidden ON work_items (hidden)")
            )

    def _migrate_codex_window_lineage(self) -> None:
        if "codex_windows" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("codex_windows")}
        if "parent_thread_id" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE codex_windows ADD COLUMN parent_thread_id VARCHAR(80)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_codex_windows_parent_thread_id "
                    "ON codex_windows (parent_thread_id)"
                )
            )

    def _migrate_branch_workspace_path(self) -> None:
        if "execution_branches" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("execution_branches")}
        if "workspace_path" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE execution_branches ADD COLUMN workspace_path TEXT")
            )

    def _migrate_branch_continuation(self) -> None:
        if "execution_branches" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("execution_branches")}
        if "continued_by_branch_id" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE execution_branches ADD COLUMN continued_by_branch_id VARCHAR(36)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_execution_branches_continued_by_branch_id "
                    "ON execution_branches (continued_by_branch_id)"
                )
            )

    def _migrate_execution_branch_hidden(self) -> None:
        if "execution_branches" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("execution_branches")}
        if "hidden" in columns:
            return
        with self.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE execution_branches ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT 0")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_execution_branches_hidden "
                    "ON execution_branches (hidden)"
                )
            )

    def _migrate_codex_window_identity(self) -> None:
        if "codex_windows" not in inspect(self.engine).get_table_names():
            return
        columns = {item["name"] for item in inspect(self.engine).get_columns("codex_windows")}
        with self.engine.begin() as connection:
            if "kind" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE codex_windows ADD COLUMN kind VARCHAR(24) "
                        "NOT NULL DEFAULT 'unknown'"
                    )
                )
                connection.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_codex_windows_kind ON codex_windows (kind)")
                )
            if "display_name" not in columns:
                connection.execute(
                    text("ALTER TABLE codex_windows ADD COLUMN display_name VARCHAR(120)")
                )
            if "title_source" not in columns:
                connection.execute(
                    text("ALTER TABLE codex_windows ADD COLUMN title_source VARCHAR(24)")
                )

    def _migrate_agent_runtime(self) -> None:
        """Add explicit runtime ownership without rewriting historical sessions."""
        tables = set(inspect(self.engine).get_table_names())
        if "execution_branches" in tables:
            branch_columns = {
                item["name"] for item in inspect(self.engine).get_columns("execution_branches")
            }
            if "runtime" not in branch_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE execution_branches "
                            "ADD COLUMN runtime VARCHAR(16) NOT NULL DEFAULT 'codex'"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_execution_branches_runtime "
                            "ON execution_branches (runtime)"
                        )
                    )
        if "session_attempts" in tables:
            session_columns = {
                item["name"] for item in inspect(self.engine).get_columns("session_attempts")
            }
            if "runtime" not in session_columns:
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE session_attempts "
                            "ADD COLUMN runtime VARCHAR(16) NOT NULL DEFAULT 'codex'"
                        )
                    )
                    connection.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS ix_session_attempts_runtime "
                            "ON session_attempts (runtime)"
                        )
                    )

    def session(self) -> Generator[Session, None, None]:
        with self.session_factory() as session:
            yield session
