from __future__ import annotations

from pathlib import Path

import pytest

from scripts.patch_toolathlon_task_databases import (
    TaskDatabasePatchError,
    patch_source,
    patch_task_databases,
)


def test_patch_task_databases_rewrites_dict_and_keyword_connections(tmp_path: Path) -> None:
    task = tmp_path / "task-1"
    preprocess = task / "preprocess" / "main.py"
    evaluation = task / "evaluation" / "main.py"
    preprocess.parent.mkdir(parents=True)
    evaluation.parent.mkdir(parents=True)
    preprocess.write_text(
        'DB = {"dbname": "toolathlon_gym"}\n',
        encoding="utf-8",
    )
    evaluation.write_text(
        "connect(database='toolathlon_gym')\n",
        encoding="utf-8",
    )

    checked = patch_task_databases(tmp_path, write=False)
    assert checked["changed_files"] == 2
    assert checked["replacements"] == 2
    assert "PGDATABASE" not in preprocess.read_text(encoding="utf-8")

    written = patch_task_databases(tmp_path, write=True)
    assert written["replacements"] == 2
    assert "PGDATABASE" in preprocess.read_text(encoding="utf-8")
    assert "PGDATABASE" in evaluation.read_text(encoding="utf-8")


def test_patch_source_accepts_existing_env_default_and_rejects_unknown_literal() -> None:
    existing = 'DB = {"dbname": os.environ.get("PGDATABASE", "toolathlon_gym")}\n'
    assert patch_source(existing, path=Path("existing.py")) == (existing, 0)

    with pytest.raises(TaskDatabasePatchError, match="unsupported hard-coded"):
        patch_source('DB_NAME = "toolathlon_gym"\n', path=Path("unsupported.py"))
