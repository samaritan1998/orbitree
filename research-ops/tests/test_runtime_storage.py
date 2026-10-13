from __future__ import annotations

import sqlite3

from research_ops.runtime_storage import prepare_runtime_database, seed_runtime_database
from research_ops.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "catalog",
        runtime_dir=tmp_path / "runtime",
        workspace_root=tmp_path / "workspaces",
    )


def _write_value(path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("create table if not exists records (value text not null)")
        connection.execute("delete from records")
        connection.execute("insert into records (value) values (?)", (value,))


def _read_value(path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("select value from records").fetchone()[0])


def test_prepare_runtime_database_seeds_empty_local_storage(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    _write_value(settings.data_dir / "research-ops.db", "from-catalog")

    assert prepare_runtime_database(settings) is True
    assert _read_value(settings.runtime_data_dir / "research-ops.db") == "from-catalog"


def test_prepare_runtime_database_preserves_existing_runtime_state(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    _write_value(settings.data_dir / "research-ops.db", "from-catalog")
    _write_value(settings.runtime_data_dir / "research-ops.db", "newer-runtime")

    assert prepare_runtime_database(settings) is False
    assert _read_value(settings.runtime_data_dir / "research-ops.db") == "newer-runtime"


def test_seed_runtime_database_can_replace_a_stale_runtime_snapshot(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    _write_value(settings.data_dir / "research-ops.db", "from-catalog")
    _write_value(settings.runtime_data_dir / "research-ops.db", "stale-runtime")

    assert seed_runtime_database(settings, replace=True) is True
    assert _read_value(settings.runtime_data_dir / "research-ops.db") == "from-catalog"
