#!/usr/bin/env python3
"""Validate two live OpenReward MOPD rounds and their updated checkpoint."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"receipt is not an object: {path.name}")
    return value


def _final_version(receipt: dict) -> str:
    versions: set[str] = set()
    for sample in receipt.get("samples") or []:
        response_length = int(sample.get("response_length") or 0)
        if sample.get("status") != "completed" or sample.get("reward") is None:
            raise RuntimeError("rollout contains an unusable sample")
        for key in (
            "behavior_logprob_count",
            "finite_behavior_logprob_count",
            "teacher_logprob_count",
            "finite_teacher_logprob_count",
        ):
            if int(sample.get(key) or 0) != response_length:
                raise RuntimeError(f"{key} does not match response_length")
        if int(sample.get("loss_mask_ones") or 0) < 1:
            raise RuntimeError("rollout sample has no trainable policy tokens")
        sample_versions = [str(value) for value in sample.get("weight_versions") or []]
        if not sample_versions or any(value == "default" for value in sample_versions):
            raise RuntimeError("rollout sample has no concrete weight version")
        versions.add(sample_versions[-1])
    if len(versions) != 1:
        raise RuntimeError(f"rollout has inconsistent final weight versions: {sorted(versions)}")
    return versions.pop()


def _weight_versions_changed(versions: list[str]) -> bool:
    """Return whether every adjacent rollout pair has a new policy version."""
    return all(a != b for a, b in zip(versions, versions[1:], strict=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--rollout-ids", type=int, nargs="+", required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--expected-iteration", type=int, required=True)
    parser.add_argument("--train-log", type=Path, required=True)
    parser.add_argument("--transition", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if len(args.rollout_ids) < 2:
        raise RuntimeError("at least two rollout ids are required to verify weight refresh")
    receipts = [
        _load(args.evidence_dir / f"rollout_{rollout_id}.json")
        for rollout_id in args.rollout_ids
    ]
    versions = [_final_version(receipt) for receipt in receipts]
    if not _weight_versions_changed(versions):
        raise RuntimeError(f"policy weight version did not change across rollouts: {versions}")

    tracker = args.checkpoint / "latest_checkpointed_iteration.txt"
    if not tracker.is_file():
        raise RuntimeError("updated checkpoint tracker is missing")
    iteration = int(tracker.read_text(encoding="utf-8").strip())
    if iteration != args.expected_iteration:
        raise RuntimeError(
            f"updated checkpoint iteration is {iteration}, expected {args.expected_iteration}"
        )
    iteration_dir = args.checkpoint / f"iter_{iteration:07d}"
    if not iteration_dir.is_dir() or not any(iteration_dir.glob("*.distcp")):
        raise RuntimeError("updated checkpoint shards are missing")

    train_log = args.train_log.read_text(encoding="utf-8", errors="replace")
    if "opd_reverse_kl" not in train_log:
        raise RuntimeError("training log has no OPD reverse-KL metric")
    transition = None
    if args.transition is not None:
        transition = _load(args.transition)
        if transition.get("mode") != "sync_validate":
            raise RuntimeError("online transition receipt has the wrong driver mode")
        required_markers = (
            "[online-transition] first_rollout_trained_and_saved",
            "[online-transition] policy_weights_refreshed_before_validation_rollout",
            "[online-transition] validation_rollout_generated_after_refresh",
        )
        if any(marker not in train_log for marker in required_markers):
            raise RuntimeError("training log is missing the online transition markers")
    if not all(math.isfinite(float(receipt.get("rollout_seconds") or 0.0)) for receipt in receipts):
        raise RuntimeError("rollout duration is non-finite")

    result = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "completed",
        "mode": "online_mopd",
        "optimizer_input": "live_memory_rollout",
        "rollout_ids": args.rollout_ids,
        "policy_weight_versions": versions,
        "weight_version_changed": True,
        "teacher_logprobs_verified": True,
        "opd_reverse_kl_logged": True,
        "checkpoint": str(args.checkpoint),
        "checkpoint_iteration": iteration,
    }
    if transition is not None:
        result["transition"] = transition
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
