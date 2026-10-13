#!/usr/bin/env python3
"""Materialize one raw-query Miaoda task without replaying historical PRD."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-jsonl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--row-index", type=int, default=0)
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in args.source_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.row_index < 0 or args.row_index >= len(rows):
        raise SystemExit("row index is outside the source JSONL")
    source = rows[args.row_index]
    if not isinstance(source, dict):
        raise SystemExit("source row is not an object")
    query = str(source.get("query") or source.get("user_query") or "").strip()
    if not query:
        raise SystemExit("source row has no raw query")
    value: dict[str, Any] = {
        "episode_id": str(args.episode_id).strip(),
        "query": query,
        "app_type": str(source.get("app_type") or "MINIPROGRAM").strip(),
        "split": "train",
        "source_record_id": str(source.get("id") or "").strip(),
        # Pure MOPD learns from teacher token log-probabilities.  Goblin can
        # still run asynchronously for monitoring, but is not a rollout gate.
        "training_objective": "pure_opd",
        "task_reward_required": False,
    }
    if not value["episode_id"]:
        raise SystemExit("episode id must not be empty")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, args.output)
    print(json.dumps({"output": str(args.output), "episode_id": value["episode_id"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
