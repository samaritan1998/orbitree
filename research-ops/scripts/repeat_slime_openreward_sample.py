#!/usr/bin/env python3
"""Repeat a validated Slime debug-rollout sample into distinct groups."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path

import torch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--copies", type=int, required=True)
    args = parser.parse_args()
    if args.copies < 1:
        raise SystemExit("--copies must be positive")
    if args.input.resolve() == args.output.resolve():
        raise SystemExit("--output must differ from --input")

    payload = torch.load(args.input, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or not isinstance(payload.get("samples"), list):
        raise ValueError("artifact must contain a samples list")
    if len(payload["samples"]) != 1:
        raise ValueError("repeat helper expects exactly one source sample")

    source = payload["samples"][0]
    if not isinstance(source, dict):
        raise ValueError("source sample must be a dictionary")
    samples = []
    for index in range(args.copies):
        sample = copy.deepcopy(source)
        sample["index"] = index
        sample["group_id"] = index
        sample.pop("rollout_id", None)
        metadata = dict(sample.get("metadata") or {})
        metadata["openreward_debug_repeat_index"] = index
        metadata["openreward_debug_repeat_count"] = args.copies
        sample["metadata"] = metadata
        samples.append(sample)

    output = dict(payload)
    output["samples"] = samples
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(output, args.output)
    print(f"repeated_samples={len(samples)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
