#!/usr/bin/env python3
"""Build a deterministic, repository-diverse OpenReward SWE canary catalog."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    PROJECT_ROOT
    / "data"
    / "example-swe-manifests"
    / "output"
    / "swebench.joined.jsonl"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "openreward-swe-canary.json"


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(row, Mapping):
                raise ValueError(f"JSONL line {line_number} is not an object")
            yield dict(row)


def select_canaries(rows: Iterable[Mapping[str, Any]], count: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen_repos: set[str] = set()
    for row in rows:
        task = row.get("task_spec")
        if not isinstance(task, Mapping):
            continue
        item = dict(task)
        repo = str(item.get("repo_ref") or "")
        if repo and repo not in seen_repos and len(selected) < count:
            selected.append(item)
            seen_repos.add(repo)
        else:
            deferred.append(item)
    if len(selected) < count:
        selected.extend(deferred[: count - len(selected)])
    if len(selected) != count:
        raise ValueError(f"requested {count} canaries but found only {len(selected)} tasks")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be positive")
    tasks = select_canaries(read_jsonl(args.input), args.count)
    payload = {
        "version": 1,
        "environments": {"swe": {"train": tasks}},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "tasks": len(tasks),
                "task_ids": [item["task_id"] for item in tasks],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
