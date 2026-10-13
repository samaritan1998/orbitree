#!/usr/bin/env python3
"""Build a prompt-only PPT research catalog without overwriting the real canary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_ops.openreward.ppt_manifest import build_ppt_online_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "openreward-ppt-prompt-only.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.limit < 1:
        raise ValueError("--limit must be positive")

    tasks: list[dict[str, object]] = []
    with args.input.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            tasks.append(build_ppt_online_task(json.loads(line)))
            if len(tasks) == args.limit:
                break
    if len(tasks) != args.limit:
        raise RuntimeError(f"requested {args.limit} PPT tasks but found {len(tasks)}")

    payload = {"environments": {"ppt": {"train": tasks}}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"ready": True, "tasks": len(tasks), "output": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
