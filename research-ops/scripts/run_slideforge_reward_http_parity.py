#!/usr/bin/env python3
"""Re-evaluate one materialized historical PPT through the live HTTP adapter."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
from research_ops.openreward import inspect_pptx, load_task_catalogs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-ppt-canary.json"
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "data" / "example-ppt-bundles" / "bundles"
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "data" / "example-ppt-bundles" / "http_parity_evidence.json"
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-url", default="http://127.0.0.1:9104")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--reward-tolerance", type=float, default=0.15)
    parser.add_argument("--timeout-seconds", type=float, default=6000)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not 0 <= args.reward_tolerance <= 1:
        raise ValueError("--reward-tolerance must be between 0 and 1")

    tasks = load_task_catalogs(args.catalog)["ppt"]["train"]
    task = next(
        (
            item
            for item in tasks
            if args.task_id is None or str(item["task_id"]) == args.task_id
        ),
        None,
    )
    if task is None:
        raise RuntimeError("requested task is absent from the PPT catalog")
    task_id = str(task["task_id"])
    bundle = args.bundle_root.expanduser().resolve() / task_id
    source_artifact = bundle / "historical" / "slides.pptx"
    source_renders = sorted(
        path
        for path in (bundle / "historical" / "rendered").iterdir()
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
    )
    raw = source_artifact.read_bytes()
    inspection = inspect_pptx(raw)
    if len(source_renders) != inspection.slides:
        raise RuntimeError("historical render count does not match the PPTX")

    artifact_dir = args.artifact_root.expanduser().resolve() / "historical-parity" / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / "slides.pptx"
    shutil.copy2(source_artifact, artifact)
    render_paths = []
    for index, source in enumerate(source_renders, start=1):
        destination = artifact_dir / f"slide-{index:03d}{source.suffix.casefold()}"
        shutil.copy2(source, destination)
        render_paths.append(destination)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = str(os.environ.get("ORS_PPT_RUBRIC_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = (
            token if token.casefold().startswith("bearer ") else f"Bearer {token}"
        )
    body = {
        "task_id": task_id,
        "query": task["query"],
        "task_spec": task,
        "artifact_path": str(artifact),
        "render_paths": [str(path) for path in render_paths],
        "inputs_path": str(bundle / "inputs"),
        "inspection": {
            "slides": inspection.slides,
            "text_runs": inspection.text_runs,
            "characters": inspection.characters,
            "sha256": inspection.sha256,
        },
    }
    with httpx.Client(
        base_url=args.adapter_url.rstrip("/"),
        timeout=args.timeout_seconds,
        trust_env=False,
    ) as client:
        response = client.post("/v1/evaluate", headers=headers, json=body)
    response.raise_for_status()
    result = response.json()
    expected = float(task["source"]["historical_reward"])
    actual = float(result["reward"])
    delta = abs(actual - expected)
    usable = result.get("usable") is True
    parity_passed = usable and delta <= args.reward_tolerance
    failure_reasons = []
    if not usable:
        failure_reasons.append("reward_not_training_usable")
    if delta > args.reward_tolerance:
        failure_reasons.append("reward_delta_exceeds_tolerance")
    evidence = {
        "schema_version": "openreward.ppt.slideforge_http_parity.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "ready": parity_passed,
        "evaluation_completed": True,
        "parity_passed": parity_passed,
        "failure_reasons": failure_reasons,
        "task_id": task_id,
        "expected_historical_reward": expected,
        "actual_reward": actual,
        "absolute_delta": delta,
        "reward_tolerance": args.reward_tolerance,
        "usable": result["usable"],
        "rubric": result["rubric"],
        "grader_id": result.get("grader_id"),
        "artifact_sha256": inspection.sha256,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    if not parity_passed:
        raise RuntimeError("live SlideForge reward parity failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
