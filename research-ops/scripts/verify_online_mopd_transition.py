#!/usr/bin/env python3
"""Verify the one-step online-MOPD weight/version transition.

This is intentionally JSON-only so it can run on the Research Ops host after
the two LTP jobs finish.  It checks that the post-update episode carries a
different concrete policy version, finite behavior and teacher logprobs, and
that its reward/terminal contract remains valid.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _versions(evidence: dict[str, Any]) -> set[str]:
    values = evidence.get("weight_versions") or []
    return {str(value).strip() for value in values if str(value).strip()}


def _episode_rows(document: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    rows = document.get("samples")
    if isinstance(rows, list) and rows:
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"{label} samples must be objects")
        return rows
    if document.get("status") is not None:
        return [document]
    raise ValueError(f"{label} evidence has no episode samples")


def _check_episode(evidence: dict[str, Any], *, require_teacher: bool) -> dict[str, Any]:
    identity = evidence.get("identity") if isinstance(evidence.get("identity"), dict) else {}
    finished = evidence.get("finished", identity.get("openreward_finished"))
    if evidence.get("status") != "completed" or finished is not True:
        raise ValueError("episode is not completed/finished")
    reward = evidence.get("reward")
    if reward is None or (isinstance(reward, (int, float)) and not math.isfinite(float(reward))):
        raise ValueError("episode reward is missing or non-finite")
    response_length = int(evidence.get("response_length") or 0)
    if response_length < 1:
        raise ValueError("episode response_length is empty")
    behavior_count = int(
        evidence.get("logprob_count") or evidence.get("behavior_logprob_count") or 0
    )
    finite_behavior_count = int(
        evidence.get("finite_logprob_count")
        or evidence.get("finite_behavior_logprob_count")
        or 0
    )
    behavior_aligned = evidence.get(
        "token_logprob_lengths_match", behavior_count == response_length
    )
    if behavior_aligned is not True:
        raise ValueError("behavior token/logprob alignment failed")
    if finite_behavior_count != behavior_count:
        raise ValueError("behavior logprobs contain non-finite values")
    metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
    teacher_count = int(
        evidence.get("teacher_logprob_count")
        or metadata.get("openreward_teacher_logprob_count")
        or 0
    )
    finite_teacher_count = int(
        evidence.get("finite_teacher_logprob_count")
        or metadata.get("openreward_teacher_logprob_count")
        or 0
    )
    teacher_ok = (
        teacher_count == response_length
        and finite_teacher_count == teacher_count
        and evidence.get("teacher_logprob_lengths_match", teacher_count == response_length) is True
    )
    if require_teacher and not teacher_ok:
        raise ValueError("teacher logprobs do not cover the complete response")
    versions = _versions(evidence)
    if not versions or "default" in versions:
        raise ValueError("episode has no concrete policy weight version")
    return {
        "reward": reward,
        "response_length": response_length,
        "token_count": int(evidence.get("token_count") or 0),
        "behavior_logprob_count": behavior_count,
        "finite_behavior_logprob_count": finite_behavior_count,
        "teacher_logprob_count": teacher_count,
        "finite_teacher_logprob_count": finite_teacher_count,
        "weight_versions": sorted(versions),
        "teacher_logprobs_valid": teacher_ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--gateway-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    before = _load(args.before)
    after = _load(args.after)
    gateway = _load(args.gateway_receipt)
    before_rows = _episode_rows(before, label="before")
    after_rows = _episode_rows(after, label="after")
    before_versions: set[str] = set()
    after_versions: set[str] = set()
    before_summary: list[dict[str, Any]] = []
    after_summary: list[dict[str, Any]] = []
    for row in before_rows:
        before_summary.append(_check_episode(row, require_teacher=True))
        before_versions.update(_versions(row))
    for row in after_rows:
        after_summary.append(_check_episode(row, require_teacher=True))
        after_versions.update(_versions(row))
    gateway_version = str(gateway.get("weight_version") or "").strip()
    if not gateway_version or gateway_version == "default":
        raise ValueError("gateway receipt has no concrete weight_version")
    if gateway_version not in after_versions:
        raise ValueError("post-update episode version does not match gateway receipt")
    if before_versions == after_versions:
        raise ValueError("policy weight version did not change")
    result = {
        "version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "verified",
        "gateway_version": gateway_version,
        "before_versions": sorted(before_versions),
        "after_versions": sorted(after_versions),
        "version_changed": True,
        "before": before_summary,
        "after": after_summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
