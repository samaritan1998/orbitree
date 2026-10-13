from pathlib import Path

from research_ops.services.instruction_cleanup import (
    GENERATED_AGENTS,
    cleanup_generated_instructions,
)
from research_ops.services.workspace import WorkspaceManager


def test_only_exact_generated_instructions_are_removed_with_backup(tmp_path: Path) -> None:
    workspaces = []
    originals = {}
    for index, text in enumerate(sorted(GENERATED_AGENTS)):
        directory = tmp_path / str(index)
        directory.mkdir()
        path = directory / "AGENTS.md"
        path.write_text(text)
        originals[str(path)] = text
        workspaces.append(directory)
    custom = tmp_path / "custom"
    custom.mkdir()
    custom_text = next(iter(GENERATED_AGENTS)) + "\nUser-owned additions.\n"
    (custom / "AGENTS.md").write_text(custom_text)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "AGENTS.md").symlink_to(workspaces[0] / "AGENTS.md")
    workspaces.extend([custom, linked])
    preview = cleanup_generated_instructions(workspaces)
    assert sum(item["status"] == "generated" for item in preview) == len(GENERATED_AGENTS)
    assert all(Path(path).exists() for path in originals)
    report = cleanup_generated_instructions(workspaces, backup_dir=tmp_path / "backup")
    for item in report:
        if item["status"] == "removed":
            assert not Path(item["path"]).exists()
            assert Path(item["backup"]).read_text() == originals[item["path"]]
    assert (custom / "AGENTS.md").read_text() == custom_text
    assert (linked / "AGENTS.md").is_symlink()
    assert not any(
        item["status"] == "removed"
        for item in cleanup_generated_instructions(workspaces, backup_dir=tmp_path / "backup")
    )


def test_workspace_preparation_preserves_user_authored_agents(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# User rules\n\nKeep this file exactly.\n")
    WorkspaceManager._prepare_scratch(tmp_path)
    assert path.read_text() == "# User rules\n\nKeep this file exactly.\n"
