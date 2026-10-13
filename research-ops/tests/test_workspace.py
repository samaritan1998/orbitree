from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from research_ops import models
from research_ops.services.workspace import WorkspaceError, WorkspaceManager, branch_slug
from research_ops.settings import Settings


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def test_branch_slug_keeps_mixed_unicode_names_distinct() -> None:
    assert branch_slug("MOPD") == "mopd"
    assert branch_slug("在线MOPD").startswith("mopd-")
    assert branch_slug("在线MOPD") != branch_slug("MOPD")
    assert branch_slug("在线MOPD") == branch_slug("在线mopd")
    assert branch_slug("在线研究").startswith("branch-")


def test_git_worktrees_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run("git", "init", cwd=source)
    run("git", "config", "user.email", "research-ops@example.invalid", cwd=source)
    run("git", "config", "user.name", "Research Ops Test", cwd=source)
    (source / "README.md").write_text("source\n", encoding="utf-8")
    run("git", "add", "README.md", cwd=source)
    run("git", "commit", "-m", "initial", cwd=source)

    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    settings.ensure_directories()
    manager = WorkspaceManager(settings)
    repository = models.Repository(
        name="source",
        canonical_path=str(source),
        default_ref="HEAD",
        program_codes=["HARN"],
    )
    main = manager.prepare(
        program_code="HARN",
        work_human_id="HARN-0001",
        branch_slug="main",
        repository=repository,
        mode="write",
    )
    child = manager.prepare(
        program_code="HARN",
        work_human_id="HARN-0001",
        branch_slug="child",
        repository=repository,
        mode="write",
    )
    assert main.cwd != child.cwd
    assert main.git_branch != child.git_branch
    assert Path(main.cwd).parent.name == "primary"
    assert Path(child.cwd).parent.name == "child"
    assert "forks" in Path(child.cwd).parts
    assert Path(main.cwd, "README.md").exists()
    assert Path(child.cwd, "README.md").exists()


def test_program_desk_has_a_human_readable_home(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "Q3",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    settings.ensure_directories()
    desk = WorkspaceManager(settings).prepare_program_desk(
        program_code="HARN",
        program_name="Harness / Office",
        objective="Build generalized Office harness tasks.",
    )

    root = tmp_path / "Q3" / "HARN" / "_desk"
    assert Path(desk.cwd) == root
    assert (root / "CHARTER.md").exists()
    assert not (root / "AGENTS.md").exists()
    assert (root / ".git").exists()


def test_scratch_workspace_does_not_generate_conversation_instructions(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "Q3",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    settings.ensure_directories()

    workspace = WorkspaceManager(settings).prepare(
        program_code="HARN",
        work_human_id="HARN-0001",
        branch_slug="main",
        repository=None,
        mode="write",
    )

    assert not (Path(workspace.cwd) / "AGENTS.md").exists()


def test_existing_workspace_gets_an_idempotent_q3_entry(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy" / "main"
    legacy.mkdir(parents=True)
    (legacy / "result.md").write_text("kept in place\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "Q3",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    settings.ensure_directories()
    manager = WorkspaceManager(settings)

    entry = manager.ensure_workspace_entry(
        program_code="HARN",
        work_human_id="HARN-0001",
        branch_slug="main",
        repository_name=None,
        existing_cwd=str(legacy),
    )
    expected = tmp_path / "Q3" / "HARN" / "tasks" / "HARN-0001" / "primary"
    assert Path(entry) == expected
    assert expected.is_symlink()
    assert expected.resolve() == legacy.resolve()
    assert (expected / "result.md").read_text(encoding="utf-8") == "kept in place\n"
    assert manager.ensure_workspace_entry(
        program_code="HARN",
        work_human_id="HARN-0001",
        branch_slug="main",
        repository_name=None,
        existing_cwd=str(legacy),
    ) == str(expected)


def test_workspace_entry_never_overwrites_an_existing_path(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "Q3",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
    )
    settings.ensure_directories()
    occupied = tmp_path / "Q3" / "COST" / "tasks" / "COST-0001" / "primary"
    occupied.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="already contains other data"):
        WorkspaceManager(settings).ensure_workspace_entry(
            program_code="COST",
            work_human_id="COST-0001",
            branch_slug="main",
            repository_name=None,
            existing_cwd=str(legacy),
        )
