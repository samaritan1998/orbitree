from __future__ import annotations

import hashlib

from research_ops.openreward.swe_manifest import (
    ManifestProvenance,
    build_openreward_swe_task,
    candidate_instance_ids,
    join_swe_trajectories,
)


def provenance(row: int = 1) -> ManifestProvenance:
    return ManifestProvenance(
        dataset_id="example/tasks",
        revision="abc123",
        split="train",
        file="train.parquet",
        row=row,
    )


def test_direct_instance_id_join_builds_policy_safe_task() -> None:
    manifest = {
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": "deadbeef",
        "problem_statement": "Fix the bug",
        "patch": "gold secret solution patch",
        "test_patch": "test-only patch",
        "FAIL_TO_PASS": '["tests/test_bug.py::test_bug"]',
        "PASS_TO_PASS": ["tests/test_ok.py::test_ok"],
        "docker_image": "registry/swe:owner__repo-1",
    }
    result = join_swe_trajectories(
        family="rebench",
        warehouse_batch="batch-a",
        warehouse_records=[
            {"instance_id": "owner__repo-1", "trajectory_file": "trace.jsonl"}
        ],
        manifest_records=[(manifest, provenance())],
    )

    assert result.summary["coverage"] == 1.0
    joined = result.joined[0]
    task = joined["task_spec"]
    assert task["repo_ref"] == "owner/repo"
    assert task["verifier"]["fail_to_pass"] == ["tests/test_bug.py::test_bug"]
    assert task["gold_patch_sha256"]
    assert "gold secret solution patch" not in str(joined)
    assert joined["provenance"]["join_method"] == "exact_instance_id"


def test_r2e_join_uses_deterministic_repo_and_commit_encoding() -> None:
    manifest = {
        "repo_name": "sympy",
        "commit_hash": "012345",
        "docker_image": "registry/r2e/sympy_final:012345",
        "problem_statement": "Implement the requested behavior",
        "expected_output_json": '{"ok": true}',
    }
    assert candidate_instance_ids("r2e", manifest) == ("sympy_final_012345",)

    result = join_swe_trajectories(
        family="r2e",
        warehouse_batch="batch-r2e",
        warehouse_records=[{"instance_id": "sympy_final_012345"}],
        manifest_records=[(manifest, provenance())],
    )
    assert result.summary["joined_rows"] == 1
    joined = result.joined[0]
    assert joined["provenance"]["join_method"] == "derived_r2e_instance_id"
    assert joined["task_spec"]["sandbox"]["image_ref"].endswith("sympy_final:012345")


def test_missing_and_ambiguous_ids_are_not_claimed_as_matches() -> None:
    record = {
        "instance_id": "owner__repo-1",
        "problem_statement": "Fix it",
    }
    result = join_swe_trajectories(
        family="swebench",
        warehouse_batch="batch-b",
        warehouse_records=[
            {"instance_id": "owner__repo-1"},
            {"instance_id": "missing__repo-2"},
        ],
        manifest_records=[(record, provenance(1)), (record, provenance(2))],
    )

    assert not result.joined
    assert [item["reason"] for item in result.unmatched] == [
        "ambiguous_manifest_id",
        "not_found",
    ]


def test_task_builder_reports_dataset_specific_missing_fields_without_fabricating() -> None:
    task = build_openreward_swe_task(
        family="swegym",
        instance_id="owner__repo-3",
        manifest={"instance_id": "owner__repo-3", "problem_statement": "Fix it"},
    )
    assert task == {
        "task_id": "owner__repo-3",
        "query": "Fix it",
        "benchmark_family": "swegym",
    }


def test_task_builder_preserves_executable_patch_whitespace() -> None:
    test_patch = "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    gold_patch = "diff --git a/code.py b/code.py\n@@ -1 +1 @@\n-bad\n+good\n"
    task = build_openreward_swe_task(
        family="swebench",
        instance_id="owner__repo-1",
        manifest={
            "problem_statement": "fix",
            "repo": "owner/repo",
            "base_commit": "abc",
            "version": "1",
            "test_patch": test_patch,
            "patch": gold_patch,
            "FAIL_TO_PASS": [],
            "PASS_TO_PASS": [],
        },
    )

    assert task["verifier"]["test_patch"] == test_patch
    assert task["gold_patch_sha256"] == hashlib.sha256(gold_patch.encode()).hexdigest()
