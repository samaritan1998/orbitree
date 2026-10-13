#!/usr/bin/env python3
"""Run/resume a small Miaoda AgentOS raw-query canary batch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


def _read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _is_success(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        payload.get("requirement", {}).get("terminal_state") == "awaiting_user_input"
        and payload.get("coding", {}).get("terminal_state") in {"finished", "stopped", "completed"}
    )


def _run_one(args: argparse.Namespace, index: int) -> dict[str, Any]:
    output = args.output_dir / f"miaoda-e2e-canary-{index:03d}.json"
    if _is_success(output):
        return {"index": index, "status": "already_succeeded", "output": str(output)}
    command = [
        sys.executable,
        str(args.rollout_script),
        "--record-jsonl",
        str(args.input_jsonl),
        "--record-index",
        str(index),
        "--model-name",
        args.model_name,
        "--model-api-url",
        args.model_api_url,
        "--output",
        str(output),
        "--workspace-url",
        args.workspace_url,
        "--entrypoint-url",
        args.entrypoint_url,
        "--user-id",
        args.user_id,
        "--timeout",
        str(args.timeout),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=args.repo,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=args.timeout + 120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "index": index,
            "status": "timeout",
            "timeout_seconds": args.timeout + 120,
            "output": str(output),
            "stdout_tail": (exc.stdout or "")[-500:] if isinstance(exc.stdout, str) else None,
            "stderr_tail": (exc.stderr or "")[-500:] if isinstance(exc.stderr, str) else None,
        }
    summary: dict[str, Any] = {
        "index": index,
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "output": str(output),
    }
    if completed.stdout.strip():
        try:
            summary["child"] = json.loads(completed.stdout.strip().splitlines()[-1])
        except ValueError:
            summary["child_stdout_type"] = "non_json"
    if completed.stderr.strip():
        summary["stderr_tail"] = completed.stderr.strip()[-500:]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--rollout-script", type=Path, required=True)
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-api-url", required=True)
    parser.add_argument("--workspace-url", required=True)
    parser.add_argument("--entrypoint-url", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("concurrency must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_rows(args.input_jsonl)
    results: list[dict[str, Any]] = []
    indices = list(range(len(rows)))
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(_run_one, args, index): index for index in indices}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    results.sort(key=lambda item: item["index"])
    summary = {
        "input": str(args.input_jsonl.resolve()),
        "rows": len(rows),
        "concurrency": args.concurrency,
        "results": results,
        "success_count": sum(
            item["status"] in {"succeeded", "already_succeeded"} for item in results
        ),
        "failed_count": sum(item["status"] in {"failed", "timeout"} for item in results),
    }
    summary_path = args.output_dir / "miaoda-e2e-canary-summary.json"
    temporary = summary_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, summary_path)
    print(
        json.dumps(
            {k: summary[k] for k in ("rows", "success_count", "failed_count")},
            ensure_ascii=False,
        )
    )
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
