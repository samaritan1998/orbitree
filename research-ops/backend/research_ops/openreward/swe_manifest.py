"""Deterministic joins between SWE trajectory rows and executable task manifests.

Warehouse SWE rows intentionally keep trajectories small.  The executable task
definition lives in the benchmark manifest and is recovered by an exact task ID
join.  This module contains the format-independent part of that join so the
provenance rules can be tested without downloading benchmark datasets.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ManifestProvenance:
    dataset_id: str
    revision: str
    split: str
    file: str
    row: int


@dataclass(frozen=True)
class SweJoinResult:
    joined: tuple[dict[str, Any], ...]
    unmatched: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _raw_nonempty_string(value: Any) -> str | None:
    """Keep executable patches byte-for-byte while rejecting blank values."""

    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _first_string(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _nonempty_string(record.get(key))
        if value is not None:
            return value
    return None


def _sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        return _sequence(parsed)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value]
    return [str(value)]


def candidate_instance_ids(family: str, record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exact IDs encoded by one official manifest row.

    R2E-Gym does not expose an ``instance_id`` column.  Its rollout code names
    instances as ``<repo_name>_final_<commit_hash>`` and uses the same value in
    the image basename/tag pair.  Both deterministic encodings are accepted;
    no fuzzy title or repository matching is performed.
    """

    candidates: list[str] = []
    direct = _first_string(record, "instance_id", "task_id")
    if direct is not None:
        candidates.append(direct)

    if family == "r2e":
        repo_name = _first_string(record, "repo_name")
        commit_hash = _first_string(record, "commit_hash")
        if repo_name is not None and commit_hash is not None:
            candidates.append(f"{repo_name}_final_{commit_hash}")

        image = _first_string(record, "docker_image", "image")
        if image is not None and ":" in image:
            image_name, tag = image.rsplit(":", 1)
            basename = image_name.rsplit("/", 1)[-1]
            if basename and tag:
                candidates.append(f"{basename}_{tag}")

    return tuple(dict.fromkeys(candidates))


def _sha256_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact_mapping(record: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            output[key] = value
    return output


def build_openreward_swe_task(
    *,
    family: str,
    instance_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Map a manifest record to a policy-safe OpenReward SWE task.

    The gold solution patch is deliberately excluded.  Its digest is retained
    for provenance, while evaluator-only tests remain under ``verifier``.
    """

    query = _first_string(manifest, "problem_statement", "prompt") or ""
    repo_ref = _first_string(manifest, "repo", "repo_name")
    base_commit = _first_string(manifest, "base_commit", "commit_hash")
    image_ref = _first_string(manifest, "docker_image", "image_name", "image")
    gold_patch = _raw_nonempty_string(manifest.get("patch"))

    verifier = {
        "test_patch": _raw_nonempty_string(manifest.get("test_patch")),
        "fail_to_pass": _sequence(manifest.get("FAIL_TO_PASS")),
        "pass_to_pass": _sequence(manifest.get("PASS_TO_PASS")),
        "expected_output_json": manifest.get("expected_output_json"),
    }
    verifier = {key: value for key, value in verifier.items() if value not in (None, [], "")}

    task: dict[str, Any] = {
        "task_id": instance_id,
        "query": query,
        "benchmark_family": family,
    }
    if repo_ref is not None:
        task["repo_ref"] = repo_ref
    if base_commit is not None:
        task["base_commit"] = base_commit
    if image_ref is not None:
        task["sandbox"] = {"image_ref": image_ref}
    if verifier:
        task["verifier"] = verifier

    setup = _compact_mapping(
        manifest,
        (
            "environment_setup_commit",
            "version",
            "install_config",
            "requirements",
            "environment",
        ),
    )
    if setup:
        task["setup"] = setup
    if gold_patch is not None:
        task["gold_patch_sha256"] = _sha256_text(gold_patch)
    return task


def join_swe_trajectories(
    *,
    family: str,
    warehouse_batch: str,
    warehouse_records: Iterable[Mapping[str, Any]],
    manifest_records: Iterable[tuple[Mapping[str, Any], ManifestProvenance]],
) -> SweJoinResult:
    """Join warehouse trajectories to official rows by exact instance ID."""

    manifest_index: dict[str, list[tuple[Mapping[str, Any], ManifestProvenance]]] = (
        defaultdict(list)
    )
    manifest_rows = 0
    for record, provenance in manifest_records:
        manifest_rows += 1
        for candidate in candidate_instance_ids(family, record):
            manifest_index[candidate].append((record, provenance))

    joined: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    warehouse_ids: set[str] = set()
    duplicate_warehouse_ids: set[str] = set()
    seen_warehouse_ids: set[str] = set()
    ambiguous_ids: set[str] = set()
    missing_task_fields: dict[str, int] = defaultdict(int)

    for warehouse_row, warehouse_record in enumerate(warehouse_records, start=1):
        instance_id = _first_string(warehouse_record, "instance_id", "task_id")
        if instance_id is None:
            unmatched.append(
                {
                    "warehouse_batch": warehouse_batch,
                    "warehouse_row": warehouse_row,
                    "reason": "missing_instance_id",
                }
            )
            continue
        warehouse_ids.add(instance_id)
        if instance_id in seen_warehouse_ids:
            duplicate_warehouse_ids.add(instance_id)
        seen_warehouse_ids.add(instance_id)

        matches = manifest_index.get(instance_id, [])
        if len(matches) != 1:
            reason = "not_found" if not matches else "ambiguous_manifest_id"
            if matches:
                ambiguous_ids.add(instance_id)
            unmatched.append(
                {
                    "instance_id": instance_id,
                    "warehouse_batch": warehouse_batch,
                    "warehouse_row": warehouse_row,
                    "trajectory_file": warehouse_record.get("trajectory_file"),
                    "reason": reason,
                    "manifest_match_count": len(matches),
                }
            )
            continue

        manifest, provenance = matches[0]
        task = build_openreward_swe_task(
            family=family,
            instance_id=instance_id,
            manifest=manifest,
        )
        for field in ("query", "repo_ref", "base_commit", "sandbox", "verifier"):
            if field not in task or task[field] in (None, "", [], {}):
                missing_task_fields[field] += 1

        joined.append(
            {
                "task_spec": task,
                "provenance": {
                    "join_method": (
                        "exact_instance_id"
                        if _first_string(manifest, "instance_id", "task_id") == instance_id
                        else "derived_r2e_instance_id"
                    ),
                    # An exact ID proves the join key, but does not by itself
                    # prove the historical dataset revision used for rollout.
                    "confidence": "high_confidence",
                    "warehouse": {
                        "batch_id": warehouse_batch,
                        "row": warehouse_row,
                        "trajectory_file": warehouse_record.get("trajectory_file"),
                        "is_solved": warehouse_record.get("is_solved"),
                    },
                    "manifest": {
                        "dataset_id": provenance.dataset_id,
                        "revision": provenance.revision,
                        "split": provenance.split,
                        "file": provenance.file,
                        "row": provenance.row,
                    },
                },
            }
        )

    total = len(joined) + len(unmatched)
    summary = {
        "family": family,
        "warehouse_batch": warehouse_batch,
        "warehouse_rows": total,
        "warehouse_unique_instance_ids": len(warehouse_ids),
        "manifest_rows": manifest_rows,
        "manifest_index_ids": len(manifest_index),
        "joined_rows": len(joined),
        "unmatched_rows": len(unmatched),
        "coverage": len(joined) / total if total else 0.0,
        "duplicate_warehouse_id_count": len(duplicate_warehouse_ids),
        "duplicate_warehouse_ids_sample": sorted(duplicate_warehouse_ids)[:20],
        "ambiguous_manifest_id_count": len(ambiguous_ids),
        "ambiguous_manifest_ids_sample": sorted(ambiguous_ids)[:20],
        "missing_task_fields": dict(sorted(missing_task_fields.items())),
    }
    return SweJoinResult(tuple(joined), tuple(unmatched), summary)
