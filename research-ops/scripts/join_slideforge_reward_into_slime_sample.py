#!/usr/bin/env python3
"""Join a verified asynchronous SlideForge score into one Slime debug sample."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_torch(path: Path) -> dict[str, Any]:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - executed in the LTP image
        raise RuntimeError("torch is required to join a Slime sample") from exc
    try:
        payload = torch.load(path, map_location="cpu", weights_only=False)
    except TypeError:  # older torch releases
        payload = torch.load(path, map_location="cpu")
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise ValueError("sample is not a slime.debug_rollout.v1 payload")
    return payload


def _write_torch(path: Path, payload: dict[str, Any]) -> None:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        torch.save(payload, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--rollout-evidence", type=Path, required=True)
    parser.add_argument("--reward-evidence", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    sample_path = args.sample.resolve()
    artifact_path = args.artifact.resolve()
    rollout = _load_json(args.rollout_evidence.resolve())
    reward_evidence = _load_json(args.reward_evidence.resolve())
    if rollout.get("status") != "completed" or rollout.get("finished") is not True:
        raise ValueError("rollout evidence is not a completed episode")
    expected_sample_sha = str(rollout.get("sample_artifact_sha256") or "").strip().lower()
    actual_sample_sha = _sha256(sample_path)
    if not expected_sample_sha or expected_sample_sha != actual_sample_sha:
        raise ValueError("rollout evidence sample hash does not match the input sample")

    artifact_sha = _sha256(artifact_path)
    if str(reward_evidence.get("artifact_sha256") or "").strip().lower() != artifact_sha:
        raise ValueError("SlideForge reward evidence artifact hash does not match artifact")
    result = reward_evidence.get("result")
    if not isinstance(result, dict) or result.get("usable") is not True:
        raise ValueError("SlideForge reward evidence is not training usable")
    reward = result.get("reward")
    if (
        not isinstance(reward, (int, float))
        or isinstance(reward, bool)
        or not math.isfinite(float(reward))
    ):
        raise ValueError("SlideForge reward evidence has no finite numeric reward")
    task_id = str(reward_evidence.get("task_id") or "").strip()
    rollout_task_id = str((rollout.get("metadata") or {}).get("openreward_task_id") or "").strip()
    if not task_id or task_id != rollout_task_id:
        raise ValueError("SlideForge and rollout task IDs do not match")

    payload = _load_torch(sample_path)
    samples = payload["samples"]
    if not samples:
        raise ValueError("sample payload has no samples")
    joined_at = datetime.now(UTC).isoformat()
    old_rewards: list[Any] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("sample payload contains a non-object sample")
        old_rewards.append(sample.get("reward"))
        response_length = int(sample.get("response_length") or 0)
        teacher = sample.get("teacher_log_probs")
        if response_length < 1 or not isinstance(teacher, list) or len(teacher) != response_length:
            raise ValueError("sample teacher logprobs are not complete")
        if any(not math.isfinite(float(value)) for value in teacher):
            raise ValueError("sample teacher logprobs contain a non-finite value")
        metadata = sample.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            sample["metadata"] = metadata
        metadata["openreward_reward_source"] = "slideforge-http-reward-join"
        metadata["openreward_reward_join"] = {
            "schema_version": "openreward.reward_join.v1",
            "joined_at": joined_at,
            "source": "slideforge-http",
            "reward_evidence": str(args.reward_evidence.resolve()),
            "artifact_sha256": artifact_sha,
            "reward": float(reward),
            "original_reward": sample.get("reward"),
        }
        sample["reward"] = float(reward)

    output = args.output.resolve()
    _write_torch(output, payload)
    receipt = {
        "schema_version": "openreward.reward_join_receipt.v1",
        "joined_at": joined_at,
        "status": "completed",
        "task_id": task_id,
        "input_sample": str(sample_path),
        "input_sample_sha256": actual_sample_sha,
        "output_sample": str(output),
        "output_sample_sha256": _sha256(output),
        "artifact_sha256": artifact_sha,
        "reward_evidence": str(args.reward_evidence.resolve()),
        "reward": float(reward),
        "original_rewards": old_rewards,
        "sample_count": len(samples),
    }
    args.receipt.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.receipt.resolve().write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        key: receipt[key]
        for key in ("status", "task_id", "reward", "sample_count", "output_sample_sha256")
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
