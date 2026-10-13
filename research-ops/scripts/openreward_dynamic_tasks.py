#!/usr/bin/env python3
"""Resolve one policy-safe OpenReward task reference at rollout time."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MAX_TASK_BYTES = 32 * 1024 * 1024


def _task_store_root() -> Path:
    raw = str(os.environ.get("SLIME_OPENREWARD_TASK_STORE_ROOT", "")).strip()
    if not raw:
        raise RuntimeError("missing runtime reference: SLIME_OPENREWARD_TASK_STORE_ROOT")
    root = Path(raw)
    if not root.is_absolute():
        raise RuntimeError("OpenReward task store root must be absolute")
    return root.resolve()


def _read_reference(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise RuntimeError("OpenReward task reference must be an object")
    path = Path(str(raw.get("path") or ""))
    if not path.is_absolute():
        raise RuntimeError("OpenReward task reference path must be absolute")
    root = _task_store_root()
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise RuntimeError("OpenReward task reference is outside its task store")
    try:
        offset = int(raw.get("offset"))
        length = int(raw.get("length"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OpenReward task reference has an invalid byte range") from exc
    if offset < 0 or not 1 <= length <= _MAX_TASK_BYTES:
        raise RuntimeError("OpenReward task reference has an invalid byte range")
    with path.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read(length)
    if len(payload) != length:
        raise RuntimeError("OpenReward task reference is truncated")
    expected = str(raw.get("sha256") or "").strip().casefold()
    if len(expected) != 64 or hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("OpenReward task reference hash mismatch")
    try:
        record = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OpenReward task reference is not valid JSON") from exc
    if not isinstance(record, dict) or not isinstance(record.get("task"), dict):
        raise RuntimeError("OpenReward task store record has an invalid schema")
    return record, dict(raw)


def materialize_task_reference(sample: Any) -> bool:
    """Attach a full task spec for one adapter call, returning whether it was loaded."""

    metadata = getattr(sample, "metadata", None)
    if not isinstance(metadata, dict):
        raise RuntimeError("OpenReward sample metadata must be an object")
    raw_ref = metadata.get("openreward_task_ref")
    if raw_ref is None:
        return False
    if metadata.get("openreward_task") is not None:
        raise RuntimeError("OpenReward sample cannot contain both a task and task reference")
    record, _ = _read_reference(raw_ref)
    domain = str(metadata.get("openreward_env") or "").strip().casefold()
    if str(record.get("domain") or "").strip().casefold() != domain:
        raise RuntimeError("OpenReward task reference domain mismatch")
    task = dict(record["task"])
    source_task_id = str(
        metadata.get("openreward_source_task_id")
        or task.get("task_id")
        or task.get("episode_id")
        or task.get("id")
        or ""
    ).strip()
    if not source_task_id:
        raise RuntimeError("OpenReward task reference has no stable source ID")
    sample_index = getattr(sample, "index", None)
    if isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index < 0:
        raise RuntimeError("OpenReward dynamic sample has no stable occurrence index")
    occurrence_id = f"{source_task_id}:online:{sample_index}"
    metadata["openreward_source_task_id"] = source_task_id
    metadata["openreward_task_id"] = occurrence_id
    if metadata.get("openreward_pure_opd") is True:
        task["training_objective"] = "pure_opd"
        task["task_reward_required"] = False
    if domain == "miaoda":
        batch_fields = {
            "optimizer_batch_id": metadata.get("openreward_optimizer_batch_id"),
            "optimizer_batch_expected_count": metadata.get(
                "openreward_optimizer_batch_expected_count"
            ),
            "optimizer_batch_member_index": metadata.get(
                "openreward_optimizer_batch_member_index"
            ),
        }
        if any(value is not None for value in batch_fields.values()):
            if any(value is None for value in batch_fields.values()):
                raise RuntimeError("Miaoda optimizer batch metadata is incomplete")
            task.update(batch_fields)
        task["source_episode_id"] = str(
            task.get("source_episode_id") or task.get("episode_id") or source_task_id
        )
        task["episode_id"] = occurrence_id
    metadata["openreward_task"] = task
    return True


def clear_materialized_task(sample: Any) -> None:
    """Drop large verifier/task fields after ORS has created the episode."""

    metadata = getattr(sample, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop("openreward_task", None)
