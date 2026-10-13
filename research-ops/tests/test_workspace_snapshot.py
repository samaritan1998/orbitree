from pathlib import Path

import pytest
from research_ops import models
from research_ops.services.workspace import WorkspaceError
from research_ops.services.workspace_snapshot import (
    MANIFEST,
    apply_workspace_snapshot,
    contains_credentials,
    plan_workspace_snapshot,
)
from test_api_workflow import create_work
from test_claude_execution import claude_branch


def test_credential_filter_does_not_confuse_token_counters_with_secrets():
    assert not contains_credentials("token = count\nmax_token = 8192\n")
    assert not contains_credentials("api_key = os.environ['API_KEY']")
    assert contains_credentials('{"api_key": "abcdefgh0123456789"}')  # gitleaks:allow -- synthetic test fixture
    assert contains_credentials("authorization=bce-auth-v1/" + "a" * 60)


def test_snapshot_keeps_uncommitted_sources_but_not_outputs_or_secrets(tmp_path):
    source, destination = tmp_path / "primary", tmp_path / "child"
    source.mkdir()
    destination.mkdir()
    files = {
        "research/run.py": "print('ready')",
        "README.md": "Experiment notes",
        "profile/trial/a01/config.yaml": "steps: 5",
        "profile/trial/a01/measurements.json": '{"loss": 1}',
        "profile/trial/a01/raw/source.py": "raw output",
        "node_modules/pkg/index.js": "dependency",
        ".env": "private",
        "weights/model.py": "model output",
        "artifacts/report.md": "generated artifact",
        ".git/config": "git metadata",
        "key.py": 'key = "sk-' + "x" * 32 + '"',
        "huge.py": "x" * (2 * 1024 * 1024 + 1),
    }
    for name, content in files.items():
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    (source / "outside.md").symlink_to(tmp_path / "elsewhere")
    plan = plan_workspace_snapshot(source, destination)
    result = apply_workspace_snapshot(plan)
    assert result["file_count"] == 3
    assert (destination / "research/run.py").read_text() == files["research/run.py"]
    assert (destination / MANIFEST).stat().st_mode & 0o777 == 0o600
    assert not (destination / "key.py").exists()
    (destination / "research/run.py").write_text("changed")
    assert (source / "research/run.py").read_text() == files["research/run.py"]
    with pytest.raises(WorkspaceError, match="already has"):
        apply_workspace_snapshot(plan)


@pytest.mark.parametrize("failure", ["overwrite", "source_changed", "symlink"])
def test_snapshot_preflight_never_overwrites_child_or_changed_source(tmp_path, failure):
    source, destination = tmp_path / "primary", tmp_path / "child"
    source.mkdir()
    destination.mkdir()
    (source / "first.md").write_text("first")
    (source / "last.md").write_text("source")
    plan = plan_workspace_snapshot(source, destination)
    if failure == "overwrite":
        (destination / "last.md").write_text("child work")
    elif failure == "source_changed":
        (source / "last.md").write_text("changed")
    else:
        (destination / "last.md").symlink_to(source / "last.md")
    with pytest.raises(WorkspaceError):
        apply_workspace_snapshot(plan)
    assert not (destination / "first.md").exists()
    assert not (destination / MANIFEST).exists()


def test_workspace_handoff_preserves_thread_and_transfers_only_explicit_parent(client):
    _, parent = create_work(client)
    child = claude_branch(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        p = session.get(models.ExecutionBranch, parent["id"])
        c = session.get(models.ExecutionBranch, child["id"])
        c.work_item_id = p.work_item_id
        c.slug = "claude-child"
        c.parent_branch_id = p.id
        c.codex_thread_id = "existing-claude-thread"
        c.context_snapshot = {"runtime_handoff": {"immutable": True}}
        session.add(models.ScheduledCheck(branch_id=p.id, prompt="Old check", status="active"))
        session.commit()
        source, destination = Path(p.cwd), Path(c.cwd)
    (source / "code.py").write_text("print('source')")
    path = f"/api/branches/{child['id']}/claude-workspace-handoff"
    assert client.post(path, json={"take_over": True}).status_code == 409
    response = client.post(path, json={"manual_approval": True, "take_over": True})
    assert response.status_code == 200, response.text
    assert response.json()["file_count"] == 1
    assert (destination / "code.py").read_text() == "print('source')"
    with state.database.session_factory() as session:
        p = session.get(models.ExecutionBranch, parent["id"])
        c = session.get(models.ExecutionBranch, child["id"])
        assert p.status == "continued" and p.continued_by_branch_id == c.id
        assert c.status == "active" and c.codex_thread_id == "existing-claude-thread"
        assert c.context_snapshot["runtime_handoff"] == {"immutable": True}
        assert c.cwd == str(destination) and p.cwd == str(source)
    check = client.get(f"/api/branches/{parent['id']}/scheduled-check").json()["check"]
    assert check["status"] == "paused"
    assert check["next_run_at"] is None


def test_workspace_handoff_rolls_back_status_on_file_conflict(client):
    _, parent = create_work(client)
    child = claude_branch(client)
    state = client.app.state.rops
    with state.database.session_factory() as session:
        p = session.get(models.ExecutionBranch, parent["id"])
        c = session.get(models.ExecutionBranch, child["id"])
        c.parent_branch_id = p.id
        c.codex_thread_id = "existing-claude-thread"
        session.commit()
        Path(p.cwd, "README.md").write_text("parent")
        Path(c.cwd, "README.md").write_text("child")
    with pytest.raises(WorkspaceError, match="overwritten"):
        state.service.prepare_claude_workspace_handoff(
            child["id"], manual_approval=True, take_over=True
        )
    assert state.service.branch_detail(parent["id"])["branch"].status == "active"
    assert state.service.branch_detail(child["id"])["branch"].status == "active"
