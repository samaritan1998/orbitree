#!/usr/bin/env python3
"""Merge Slime OpenReward debug artifacts without changing sample tensors."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import torch

_PUBLIC_TASK_FIELDS = {
    "task_id",
    "id",
    "episode_id",
    "case_id",
    "query",
    "benchmark_family",
    "task_name",
}
_PUBLIC_SOURCE_FIELDS = {
    "dataset",
    "split",
    "revision",
    "repository",
    "source_revision",
    "source_row",
}


def _sanitize_task_metadata(metadata: dict[str, Any]) -> None:
    """Defensively strip verifier/sandbox fields from old debug artifacts."""

    task = metadata.get("openreward_task")
    if not isinstance(task, Mapping):
        return
    public = {
        str(key): value
        for key, value in task.items()
        if str(key) in _PUBLIC_TASK_FIELDS
        and isinstance(value, (str, int, float, bool))
    }
    if isinstance(task.get("source"), Mapping):
        source = {
            str(key): value
            for key, value in task["source"].items()
            if str(key) in _PUBLIC_SOURCE_FIELDS
            and isinstance(value, (str, int, float, bool))
        }
        if source:
            public["source"] = source
    metadata["openreward_task"] = public
    metadata["openreward_task_metadata_sanitized"] = True
    metadata.pop("openreward_trace", None)


def _load(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = torch.load(path, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: artifact must be a dictionary")
    if payload.get("format") not in {None, "slime.debug_rollout.v1"}:
        raise ValueError(f"{path}: unsupported format {payload.get('format')!r}")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError(f"{path}: artifact contains no samples")
    if not all(isinstance(sample, dict) for sample in samples):
        raise ValueError(f"{path}: samples must be dictionaries")
    return payload, samples


def _source_id(path: Path, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return str(payload.get("episode_id") or payload.get("rollout_id") or digest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    if len(args.input) < 2:
        raise SystemExit("at least two --input artifacts are required")
    if any(path.resolve() == args.output.resolve() for path in args.input):
        raise SystemExit("--output must differ from every --input")

    merged: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in args.input:
        if not path.is_file():
            raise SystemExit(f"input artifact is missing: {path}")
        payload, samples = _load(path)
        source_id = _source_id(path, payload)
        sources.append({"path": str(path), "source_id": source_id, "count": len(samples)})
        for sample in samples:
            item = copy.deepcopy(sample)
            index = len(merged)
            item["index"] = index
            item["group_id"] = index
            item.pop("rollout_id", None)
            metadata = dict(item.get("metadata") or {})
            _sanitize_task_metadata(metadata)
            metadata["openreward_merged_source_id"] = source_id
            metadata["openreward_merged_source_path"] = str(path)
            metadata["openreward_merged_index"] = index
            item["metadata"] = metadata
            merged.append(item)

    output: dict[str, Any] = {
        "format": "slime.debug_rollout.v1",
        "samples": merged,
        "openreward_merge": {
            "source_count": len(sources),
            "sample_count": len(merged),
            "sources": sources,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(output, args.output)
    summary = {
        "output": str(args.output),
        "source_count": len(sources),
        "sample_count": len(merged),
        "sources": sources,
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
