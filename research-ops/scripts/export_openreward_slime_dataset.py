#!/usr/bin/env python3
"""Export public-query ORS catalog selectors as a Slime JSONL dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_ops.openreward import build_slime_selector_rows, load_task_catalogs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--environment", default="swe")
    parser.add_argument("--split", default="train")
    parser.add_argument("--mode", choices=("auto", "managed", "interactive"), default="interactive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = build_slime_selector_rows(
        load_task_catalogs(args.catalog),
        environment=args.environment,
        split=args.split,
        mode=args.mode,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ready": True,
                "environment": args.environment,
                "split": args.split,
                "rows": len(rows),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
