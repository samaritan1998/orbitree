#!/usr/bin/env python3
"""Check whether every PPT catalog task has its declared /inputs bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from research_ops.openreward import PptTask, load_task_catalogs


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--split", default="train")
    args = parser.parse_args()

    root = args.bundle_root.expanduser()
    if not root.is_absolute():
        raise SystemExit("--bundle-root must be absolute")
    tasks = load_task_catalogs(args.catalog)["ppt"][args.split]
    ready: list[str] = []
    missing: list[dict[str, str]] = []
    for task_spec in tasks:
        task = PptTask.from_spec(task_spec)
        try:
            inputs_path = task.resolve_inputs(root.resolve())
            for asset in task_spec.get("assets") or []:
                expected = str(asset.get("sha256") or "").strip().casefold()
                if not expected:
                    continue
                relative = str(asset["path"]).removeprefix("/inputs/")
                if sha256_file(inputs_path / relative) != expected:
                    raise ValueError("asset digest mismatch")
        except Exception as exc:
            missing.append(
                {
                    "task_id": task.task_id,
                    "reason": type(exc).__name__,
                }
            )
        else:
            ready.append(task.task_id)
    report = {
        "ready": not missing,
        "tasks": len(tasks),
        "ready_tasks": len(ready),
        "missing_tasks": missing,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
