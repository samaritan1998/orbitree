#!/usr/bin/env python3
"""Build a small OpenReward SWE catalog from a pinned SWE-bench parquet file."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data/example-swe-manifests/manifests/swebench_test.parquet"


def _array(value: Any) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("SWE verifier collection is not an array")
    return [str(item) for item in value]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--instance-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise SystemExit("duckdb is required") from exc

    rows = duckdb.connect().execute(
        "SELECT * FROM read_parquet(?) WHERE instance_id IN (SELECT unnest(?))",
        [str(args.input), args.instance_id],
    )
    names = [item[0] for item in rows.description]
    instance_column = names.index("instance_id")
    by_id = {
        str(row[instance_column]): dict(zip(names, row, strict=True))
        for row in rows.fetchall()
    }
    missing = [item for item in args.instance_id if item not in by_id]
    if missing:
        raise SystemExit(f"missing instance IDs: {','.join(missing)}")

    tasks: list[dict[str, Any]] = []
    for task_id in args.instance_id:
        row = by_id[task_id]
        patch = str(row["patch"] or "")
        tasks.append(
            {
                "base_commit": str(row["base_commit"]),
                "benchmark_family": "swebench",
                "gold_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                "query": str(row["problem_statement"] or ""),
                "repo_ref": str(row["repo"]),
                "setup": {
                    "environment_setup_commit": str(row["environment_setup_commit"] or ""),
                    "version": str(row["version"]),
                },
                "task_id": task_id,
                "verifier": {
                    "fail_to_pass": _array(row["FAIL_TO_PASS"]),
                    "pass_to_pass": _array(row["PASS_TO_PASS"]),
                    "test_patch": str(row["test_patch"] or ""),
                },
            }
        )
    payload = {"version": 1, "environments": {"swe": {"train": tasks}}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "task_ids": args.instance_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
