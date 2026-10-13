#!/usr/bin/env python3
"""Validate a Slime debug-rollout artifact before model initialization."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch
from slime.utils.types import Sample


def _status_name(sample: Sample) -> str:
    status = getattr(sample.status, "value", sample.status)
    return str(status).casefold()


def _validate_sample(
    sample: Sample,
    *,
    max_tokens: int,
    require_pure_opd: bool,
) -> dict[str, Any]:
    tokens = [int(item) for item in sample.tokens]
    mask = [int(item) for item in (sample.loss_mask or [])]
    logprobs = [float(item) for item in (sample.rollout_log_probs or [])]
    if not 0 < len(tokens) <= max_tokens:
        raise ValueError(f"token length {len(tokens)} is outside (0, {max_tokens}]")
    if sample.response_length != len(mask) or sample.response_length != len(logprobs):
        raise ValueError(
            "response_length, loss_mask and rollout_log_probs lengths do not match"
        )
    if not any(mask):
        raise ValueError("loss mask contains no policy tokens")
    if any(not math.isfinite(value) for value in logprobs):
        raise ValueError("rollout log-probs contain a non-finite value")
    teacher_log_probs = sample.teacher_log_probs
    if teacher_log_probs is not None:
        teacher = [float(item) for item in teacher_log_probs]
        if len(teacher) != sample.response_length:
            raise ValueError("teacher_log_probs length does not match response_length")
        if any(not math.isfinite(value) for value in teacher):
            raise ValueError("teacher_log_probs contain a non-finite value")
    if _status_name(sample) != "completed":
        raise ValueError(f"sample status is {_status_name(sample)!r}, expected 'completed'")
    if sample.remove_sample:
        raise ValueError("sample is marked remove_sample")
    if sample.reward is None:
        raise ValueError("sample has no reward")
    if isinstance(sample.reward, (int, float)) and not math.isfinite(float(sample.reward)):
        raise ValueError("sample reward is non-finite")
    metadata = sample.metadata if isinstance(sample.metadata, dict) else {}
    if require_pure_opd:
        if not isinstance(sample.reward, (int, float)) or float(sample.reward) != 0.0:
            raise ValueError("pure OPD requires neutral sample reward 0.0")
        if metadata.get("openreward_training_objective") != "pure_opd":
            raise ValueError("sample is not marked with the pure OPD objective")
        if metadata.get("openreward_task_reward_used") is not False:
            raise ValueError("pure OPD sample still uses a task reward")
        if sample.teacher_log_probs is None:
            raise ValueError("pure OPD sample has no teacher_log_probs")
    group_id = getattr(sample, "group_id", None)
    return {
        "index": sample.index,
        "group_id": group_id,
        "token_count": len(tokens),
        "response_length": int(sample.response_length),
        "loss_mask_count": len(mask),
        "loss_mask_ones": sum(mask),
        "logprob_count": len(logprobs),
        "teacher_logprob_count": (
            len(sample.teacher_log_probs) if sample.teacher_log_probs is not None else 0
        ),
        "reward": sample.reward,
        "training_objective": metadata.get("openreward_training_objective"),
        "task_reward_used": metadata.get("openreward_task_reward_used"),
        "status": _status_name(sample),
        "weight_versions": list(sample.weight_versions),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--require-pure-opd", action="store_true")
    args = parser.parse_args()
    if args.max_tokens < 1:
        raise SystemExit("--max-tokens must be positive")

    payload = torch.load(args.sample, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise ValueError("artifact must contain a samples list")
    if payload.get("format") not in {None, "slime.debug_rollout.v1"}:
        raise ValueError(f"unsupported artifact format: {payload.get('format')!r}")
    samples = [Sample.from_dict(item) for item in payload["samples"]]
    if not samples:
        raise ValueError("artifact contains no samples")
    rows = [
        _validate_sample(
            sample,
            max_tokens=args.max_tokens,
            require_pure_opd=args.require_pure_opd,
        )
        for sample in samples
    ]
    summary = {
        "version": 1,
        "validated_at": datetime.now(UTC).isoformat(),
        "sample_path": str(args.sample),
        "format": payload.get("format", "slime.debug_rollout.v1"),
        "rollout_id": payload.get("rollout_id"),
        "sample_count": len(rows),
        "pure_opd_required": bool(args.require_pure_opd),
        "rows": rows,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
