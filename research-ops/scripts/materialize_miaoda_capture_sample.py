#!/usr/bin/env python3
"""Materialize a completed text-free Miaoda v2 capture as a Slime sample."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_miaoda_online_mopd import _direct_capture_tokens, _teacher


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _captures(path: Path, episode_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("capture row is not an object")
        if value.get("schema_version") != "mopd-policy-capture.v2":
            raise RuntimeError("capture row is not v2")
        if str(value.get("episode_id") or "") != episode_id:
            raise RuntimeError("capture episode id mismatch")
        rows.append(value)
    if not rows:
        raise RuntimeError("capture file is empty")
    return rows


async def _main_async(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    evidence: dict[str, Any] = {
        "version": 1,
        "created_at": started_at.isoformat(),
        "environment": "miaoda",
        "episode_id": args.episode_id,
        "finished": False,
        "miaoda_batch_task_id": args.batch_task_id,
        "training_objective": "pure_opd",
        "reward": 0.0,
        "task_reward_used": False,
        "goblin_job_submitted": False,
        "capture_file": str(args.capture_file),
    }
    try:
        import torch
        from slime.rollout.openreward_rollout import build_debug_rollout_dump
        from slime.utils.types import Sample

        rows = _captures(args.capture_file, args.episode_id)
        samples: list[Any] = []
        for index, row in enumerate(rows):
            version = str(row.get("weight_version") or "")
            if args.expected_weight_version and version != args.expected_weight_version:
                raise RuntimeError("policy weight version mismatch")
            prompt_ids, response_ids, behavior = _direct_capture_tokens(row)
            if not prompt_ids or not response_ids or len(response_ids) != len(behavior):
                raise RuntimeError("capture token/logprob alignment failed")
            tokens = prompt_ids + response_ids
            teacher = await _teacher(args.teacher_url, tokens, len(response_ids))
            if len(teacher) != len(response_ids):
                raise RuntimeError("teacher token/logprob alignment failed")
            sample = Sample(
                index=index,
                prompt="",
                tokens=tokens,
                response_length=len(response_ids),
                loss_mask=[1] * len(response_ids),
                rollout_log_probs=behavior,
                teacher_log_probs=teacher,
                reward=0.0,
                weight_versions=[version],
                status=Sample.Status.COMPLETED,
                metadata={
                    "openreward_env": "miaoda",
                    "openreward_finished": True,
                    "openreward_training_objective": "pure_opd",
                    "openreward_task_reward_used": False,
                    "openreward_reward_source": "pure_opd_neutral",
                    "openreward_policy_weight_version": version,
                    "openreward_episode_id": args.episode_id,
                    "openreward_miaoda_batch_task_id": args.batch_task_id,
                },
            )
            # Each AgentOS policy call is an independent OPD training group.
            # Assign this after construction for compatibility with Slime
            # versions where group_id is a dynamic Sample attribute.
            sample.group_id = index
            samples.append(sample)
            print(
                json.dumps(
                    {
                        "capture_index": index,
                        "response_length": len(response_ids),
                        "teacher_logprob_count": len(teacher),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )

        payload = build_debug_rollout_dump(samples, rollout_id=0, include_text=False)
        args.sample_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.sample_output.with_suffix(args.sample_output.suffix + ".part")
        torch.save(payload, temporary)
        os.replace(temporary, args.sample_output)

        behavior_values = [value for sample in samples for value in sample.rollout_log_probs]
        teacher_values = [value for sample in samples for value in sample.teacher_log_probs]
        evidence.update(
            {
                "status": "completed",
                "finished": True,
                "capture_count": len(rows),
                "sample_count": len(samples),
                "token_count": sum(len(sample.tokens) for sample in samples),
                "response_length": sum(sample.response_length for sample in samples),
                "logprob_count": len(behavior_values),
                "finite_logprob_count": sum(
                    math.isfinite(float(value)) for value in behavior_values
                ),
                "teacher_logprob_count": len(teacher_values),
                "finite_teacher_logprob_count": sum(
                    math.isfinite(float(value)) for value in teacher_values
                ),
                "loss_mask_ones": sum(sum(sample.loss_mask) for sample in samples),
                "group_ids": sorted(int(sample.group_id) for sample in samples),
                "unique_group_count": len({int(sample.group_id) for sample in samples}),
                "weight_versions": sorted(
                    {version for sample in samples for version in sample.weight_versions if version}
                ),
                "sample_artifact": str(args.sample_output),
                "sample_artifact_sha256": hashlib.sha256(
                    args.sample_output.read_bytes()
                ).hexdigest(),
            }
        )
    except Exception as exc:
        evidence.update({"status": "failed", "error_type": type(exc).__name__})
    evidence["duration_seconds"] = (datetime.now(UTC) - started_at).total_seconds()
    _write(args.output, evidence)
    return 0 if evidence.get("status") == "completed" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-file", type=Path, required=True)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--batch-task-id", required=True)
    parser.add_argument("--teacher-url", required=True)
    parser.add_argument("--expected-weight-version", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-output", type=Path, required=True)
    args = parser.parse_args()
    if not args.teacher_url.endswith("/generate"):
        raise SystemExit("teacher URL must end with /generate")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
