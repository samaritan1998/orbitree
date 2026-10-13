#!/usr/bin/env python3
"""Evaluate one already-rendered PPT artifact through the live SlideForge adapter."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from research_ops.openreward import inspect_pptx, load_task_catalogs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-ppt-canary.json"
DEFAULT_BUNDLE_ROOT = (
    PROJECT_ROOT / "data" / "example-ppt-bundles" / "bundles"
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _write_output(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-url", default="http://127.0.0.1:9104")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=6000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    tasks = load_task_catalogs(args.catalog)["ppt"]["train"]
    task = next(
        (item for item in tasks if str(item.get("task_id")) == args.task_id),
        None,
    )
    if task is None:
        raise RuntimeError("requested task is absent from the PPT catalog")

    artifact = args.artifact.expanduser().resolve()
    render_dir = args.render_dir.expanduser().resolve()
    bundle = args.bundle_root.expanduser().resolve() / args.task_id
    inspection = inspect_pptx(artifact.read_bytes())
    render_paths = sorted(
        path
        for path in render_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
    )
    if len(render_paths) != inspection.slides:
        raise RuntimeError(
            f"render count {len(render_paths)} does not match {inspection.slides} slides"
        )

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = str(os.environ.get("ORS_PPT_RUBRIC_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = (
            token if token.casefold().startswith("bearer ") else f"Bearer {token}"
        )
    body = {
        "task_id": args.task_id,
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
    try:
        result: object = response.json()
    except ValueError:
        result = {"detail": "SlideForge adapter returned a non-JSON response"}
    evidence = {
        "schema_version": "openreward.ppt.slideforge_artifact_evaluation.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": args.task_id,
        "artifact_path": str(artifact),
        "artifact_sha256": inspection.sha256,
        "slides": inspection.slides,
        "status_code": response.status_code,
        "result": result,
    }
    _write_output(args.output, evidence)
    print(
        json.dumps(
            {
                "task_id": args.task_id,
                "artifact_sha256": inspection.sha256,
                "slides": inspection.slides,
                "status_code": response.status_code,
                "reward": result.get("reward") if isinstance(result, dict) else None,
                "usable": result.get("usable") if isinstance(result, dict) else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
