#!/usr/bin/env python3
"""Check ORS mapping parity against materialized historical SlideForge rewards."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from research_ops.openreward import inspect_pptx, load_task_catalogs
from research_ops.openreward.slideforge_reward import map_slideforge_evaluation

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-ppt-canary.json"
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "data" / "example-ppt-bundles" / "bundles"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data"
    / "example-ppt-bundles"
    / "reward_mapping_evidence.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    tasks = load_task_catalogs(args.catalog)["ppt"]["train"]
    root = args.bundle_root.expanduser().resolve()
    rows = []
    for task in tasks:
        task_id = str(task["task_id"])
        bundle = root / task_id
        evaluation = json.loads(
            (bundle / "historical" / "evaluation.json").read_text(encoding="utf-8")
        )
        rubric = json.loads((bundle / "rubric.json").read_text(encoding="utf-8"))
        raw = (bundle / "historical" / "slides.pptx").read_bytes()
        inspection = inspect_pptx(raw)
        mapped = map_slideforge_evaluation(
            evaluation,
            rubric,
            {
                "slides": inspection.slides,
                "text_runs": inspection.text_runs,
                "characters": inspection.characters,
                "sha256": inspection.sha256,
            },
            cache_hit=True,
        )
        expected_reward = float(task["source"]["historical_reward"])
        if mapped["reward"] != expected_reward or mapped["usable"] is not True:
            raise RuntimeError(f"historical reward mapping parity failed for {task_id}")
        rows.append(
            {
                "task_id": task_id,
                "task_axes": task["task_axes"],
                "reward": mapped["reward"],
                "usable": mapped["usable"],
                "rubric": mapped["rubric"],
                "slide_count": inspection.slides,
                "artifact_sha256": inspection.sha256,
            }
        )
    evidence = {
        "schema_version": "openreward.ppt.slideforge_mapping_evidence.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "ready": True,
        "tasks": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ready": True, "tasks": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
