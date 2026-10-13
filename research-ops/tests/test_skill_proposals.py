from __future__ import annotations

from pathlib import Path

from research_ops import models, schemas
from test_api_workflow import create_work


def _completed_round(client, tmp_path: Path):
    _work, branch = create_work(client)
    state = client.app.state.rops
    state.settings.skill_extraction_enabled = True
    state.settings.skill_source_root = tmp_path / "skill-source"
    state.settings.skill_install_root = tmp_path / "skill-install"
    attempt = state.service.claim_terminal_session(branch["id"], "terminal:skill-test")
    state.service.finish_terminal_session(branch["id"], attempt.id, attempt.lease_id, 0)
    state.service.submit_handoff(
        branch["id"],
        schemas.HandoffCreate(
            outcome="continue",
            conclusion="A verifier workflow was implemented and checked against fixtures.",
            next_action="Reuse the verified procedure.",
            source_turn_ids=["turn-evidence-1"],
        ),
        session_id=attempt.id,
        lease_id=attempt.lease_id,
    )
    return state, branch, attempt


def test_skill_proposal_api_is_idempotent_and_reviewable(client, tmp_path, monkeypatch) -> None:
    state, branch, attempt = _completed_round(client, tmp_path)
    monkeypatch.setattr(
        state.skill_proposals,
        "extract",
        lambda proposal_id: state.skill_proposals.get(proposal_id),
    )

    created = client.post(
        f"/api/branches/{branch['id']}/sessions/{attempt.id}/skill-proposals",
        json={"requested": True},
    )
    repeated = client.post(
        f"/api/branches/{branch['id']}/sessions/{attempt.id}/skill-proposals",
        json={"requested": True},
    )

    assert created.status_code == 202, created.text
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["id"] == created.json()["id"]
    proposals = client.get("/api/skill-proposals").json()
    assert len(proposals) == 1
    assert proposals[0]["source_context"]["work_human_id"].startswith("HARN-")


def test_installed_skill_catalog_includes_personal_system_and_invalid(client, tmp_path) -> None:
    state = client.app.state.rops
    source = tmp_path / "skill-source" / "source-directory"
    personal = tmp_path / "skill-install" / "installed-directory"
    system = tmp_path / "skill-install" / ".system" / "system-skill"
    invalid = tmp_path / "skill-install" / "broken-skill"
    for directory in (source, personal, system, invalid):
        directory.mkdir(parents=True)
    valid_content = """---
name: example-skill
description: Use when validating the installed skill catalog.
---

# Example
"""
    (source / "SKILL.md").write_text(valid_content, encoding="utf-8")
    (personal / "SKILL.md").write_text(valid_content, encoding="utf-8")
    (personal / "scripts").mkdir()
    (personal / "scripts" / "check.py").write_text("print('ok')\n", encoding="utf-8")
    (system / "SKILL.md").write_text(
        "---\nname: system-skill\ndescription: Built-in workflow.\n---\n",
        encoding="utf-8",
    )
    (invalid / "SKILL.md").write_text("# Missing frontmatter\n", encoding="utf-8")
    state.settings.skill_source_root = tmp_path / "skill-source"
    state.settings.skill_install_root = tmp_path / "skill-install"

    response = client.get("/api/skills")

    assert response.status_code == 200, response.text
    skills = response.json()
    assert [skill["name"] for skill in skills] == [
        "broken-skill",
        "example-skill",
        "system-skill",
    ]
    example = next(skill for skill in skills if skill["name"] == "example-skill")
    assert example["scope"] == "personal"
    assert example["source_available"] is True
    assert example["source_path"] == str(source)
    assert example["supporting_files"] == 1
    broken = next(skill for skill in skills if skill["name"] == "broken-skill")
    assert broken["valid"] is False
    assert "frontmatter" in broken["error"]


def test_new_skill_is_only_written_after_explicit_accept(client, tmp_path, monkeypatch) -> None:
    state, branch, attempt = _completed_round(client, tmp_path)
    service = state.skill_proposals
    monkeypatch.setattr(
        service,
        "_run_analysis",
        lambda _context, _catalog: {
            "action": "new",
            "candidate_name": "fixture-verifier-workflow",
            "target_skill": None,
            "title": "Fixture verifier workflow",
            "description": "Use when building deterministic fixture-based verifiers.",
            "rationale": "The round established a repeatable implementation and validation order.",
            "reusable_steps": ["Inspect fixtures.", "Implement the verifier.", "Run checks."],
            "risks": ["Do not generalize beyond observed fixture formats."],
            "evidence_refs": ["turn-evidence-1"],
        },
    )
    skill_md = """---
name: fixture-verifier-workflow
description: Use when building and validating deterministic fixture-based verifiers.
---

# Fixture Verifier Workflow

1. Inspect the fixture inputs and expected outputs.
2. Implement deterministic checks for the observed contract.
3. Run all fixtures and record failures before expanding coverage.
"""
    monkeypatch.setattr(service, "_run_draft", lambda **_kwargs: skill_md)

    proposal = service.create(branch["id"], attempt.id)
    extracted = service.extract(proposal.id)

    assert extracted.status == "ready"
    assert not (state.settings.skill_source_root / "fixture-verifier-workflow").exists()
    assert not (state.settings.skill_install_root / "fixture-verifier-workflow").exists()

    accepted = service.accept(proposal.id, "Reviewed against the evidence")

    assert accepted.status == "accepted"
    source = state.settings.skill_source_root / "fixture-verifier-workflow"
    installed = state.settings.skill_install_root / "fixture-verifier-workflow"
    assert (source / "SKILL.md").read_text(encoding="utf-8") == skill_md
    assert (source / "agents/openai.yaml").is_file()
    assert (installed / "SKILL.md").read_text(encoding="utf-8") == skill_md


def test_failed_merge_validation_restores_existing_skill(client, tmp_path, monkeypatch) -> None:
    state, branch, attempt = _completed_round(client, tmp_path)
    service = state.skill_proposals
    source = state.settings.skill_source_root / "existing-workflow"
    source.mkdir(parents=True)
    original = """---
name: existing-workflow
description: Use when applying the existing verified workflow.
---

# Existing Workflow

1. Preserve this instruction.
"""
    (source / "SKILL.md").write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        service,
        "_run_analysis",
        lambda _context, _catalog: {
            "action": "merge",
            "candidate_name": None,
            "target_skill": "existing-workflow",
            "title": "Extend existing workflow",
            "description": "Use when applying the existing verified workflow.",
            "rationale": "The new step belongs to the existing owner.",
            "reusable_steps": ["Add a verified final check."],
            "risks": [],
            "evidence_refs": ["turn-evidence-1"],
        },
    )
    merged = original + "\n2. Add the verified final check.\n"
    monkeypatch.setattr(service, "_run_draft", lambda **_kwargs: merged)
    proposal = service.create(branch["id"], attempt.id)
    extracted = service.extract(proposal.id)
    assert extracted.status == "ready"
    assert (source / "SKILL.md").read_text(encoding="utf-8") == original

    real_validator = service._validate_directory

    def fail_after_source_write(skill_path: Path):
        if skill_path.resolve() == source.resolve():
            raise ValueError("simulated source validation failure")
        return real_validator(skill_path)

    monkeypatch.setattr(service, "_validate_directory", fail_after_source_write)
    failed = service.accept(proposal.id, None)

    assert failed.status == "failed"
    assert "simulated source validation failure" in (failed.error or "")
    assert (source / "SKILL.md").read_text(encoding="utf-8") == original
    with state.database.session_factory() as database_session:
        events = list(
            database_session.query(models.ActivityEvent).filter_by(
                event_type="skill.proposal_install_failed"
            )
        )
    assert len(events) == 1
