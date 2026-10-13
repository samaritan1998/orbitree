#!/usr/bin/env python3
"""Build an eight-task Toolathlon-Gym ORS catalog from a locked GitHub revision."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import httpx
from research_ops.openreward.toolathlon_manifest import build_toolathlon_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "config" / "openreward-tooluse-canary.json"
DEFAULT_REPOSITORY = "EnvCommons/toolathlon-gym"
DEFAULT_REVISION = "9c82859d05bc1f0376150d749cd2e3d43471d583"
DEFAULT_TASKS = (
    "howtocook-meal-plan-gcal",
    "wc-sales-tax-summary",
    "canvas-enrollment-notion",
    "yf-stock-volatility-terminal",
    "arxiv-conference-prep",
    "12306-beijing-shanghai-trip-notion-gcal-word",
    "sf-hr-attrition-gcal",
    "canvas-assignment-effectiveness-ppt-notion-email",
)


def _fetch(client: httpx.Client, base_url: str, path: str) -> bytes:
    response = client.get(f"{base_url}/{quote(path, safe='/')}")
    response.raise_for_status()
    return response.content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    tasks = tuple(args.tasks or DEFAULT_TASKS)
    base_url = f"https://raw.githubusercontent.com/{args.repository}/{args.revision}"
    catalog_tasks: list[dict[str, object]] = []
    with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
        for task_name in tasks:
            root = f"tasks/finalpool/{task_name}"
            query = _fetch(client, base_url, f"{root}/docs/task.md").decode("utf-8")
            task_config = json.loads(
                _fetch(client, base_url, f"{root}/task_config.json").decode("utf-8")
            )
            preprocess = _fetch(client, base_url, f"{root}/preprocess/main.py")
            evaluator = _fetch(client, base_url, f"{root}/evaluation/main.py")
            catalog_tasks.append(
                build_toolathlon_task(
                    task_name=task_name,
                    query=query,
                    task_config=task_config,
                    repository=args.repository,
                    revision=args.revision,
                    preprocess_sha256=hashlib.sha256(preprocess).hexdigest(),
                    evaluator_sha256=hashlib.sha256(evaluator).hexdigest(),
                )
            )

    payload = {"environments": {"tooluse": {"train": catalog_tasks}}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"ready": True, "tasks": len(catalog_tasks), "output": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
