#!/usr/bin/env python3
"""Run one real Toolathlon positive control through the deployed ORS service.

This is deliberately a control: ``policy_driven`` stays false.  It verifies
the complete K8s ORS -> Toolathlon -> PostgreSQL/MCP -> evaluator path before
the policy gateway is connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# An unrelated ``scripts`` package is present in some runtime images.  Import
# the adjacent helper module by its filename instead of relying on that name.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_openreward_tooluse_positive_canary import (
    _assignment_rows,
    _call,
    _output_text,
    _write_artifacts,
)

PINNED_REVISION = "9c82859d05bc1f0376150d749cd2e3d43471d583"
TASK_NAME = "canvas-assignment-stats"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ors-url-env", default="ORS_URL")
    parser.add_argument("--api-key-env", default="ORS_API_KEY")
    parser.add_argument("--output", type=Path, default=Path("/tmp/tooluse-ors-positive.json"))
    args = parser.parse_args()

    base_url = str(os.environ.get(args.ors_url_env, "")).strip().rstrip("/")
    api_key = str(os.environ.get(args.api_key_env, "")).strip()
    if not base_url or not api_key:
        raise SystemExit("ORS URL or API key reference is missing")
    started = datetime.now(UTC)
    task = {
        "task_id": TASK_NAME,
        "task_name": TASK_NAME,
        "query": "Use the prompt supplied by the official Toolathlon environment.",
        "source": {
            "repository": "EnvCommons/toolathlon-gym",
            "revision": PINNED_REVISION,
        },
    }
    headers = {"X-API-Key": api_key}
    terminal: dict[str, Any] = {}
    evaluator_text = ""
    with httpx.Client(base_url=base_url, timeout=600, trust_env=False) as client:
        response = client.post("/create_session", headers=headers)
        response.raise_for_status()
        sid = str(response.json()["sid"])
        session_headers = {**headers, "X-Session-ID": sid}
        try:
            created = client.post(
                "/create",
                headers=session_headers,
                json={"env_name": "tooluse", "task_spec": task},
            )
            created.raise_for_status()
            prompt = client.get("/tooluse/prompt", headers=session_headers)
            prompt.raise_for_status()
            tools = client.get("/tooluse/task_tools", headers=session_headers)
            tools.raise_for_status()
            rows = _assignment_rows(client, session_headers)
            xlsx_sha256, docx_sha256 = _write_artifacts(client, session_headers, rows)
            terminal = _call(client, session_headers, "claim_done", {})
            evaluator_text = _output_text(terminal)
        finally:
            client.post("/delete", headers=session_headers)

    if terminal.get("finished") is not True:
        raise RuntimeError("Toolathlon positive control did not finish")
    evidence = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": TASK_NAME,
        "source_revision": PINNED_REVISION,
        "runtime": "kubernetes-ors-toolathlon",
        "policy_driven": False,
        "reward": terminal.get("reward"),
        "finished": terminal.get("finished"),
        "prompt_blocks": len(prompt.json()),
        "tool_count": len(tools.json().get("tools", [])),
        "course_count": len(rows),
        "assignment_count": sum(int(item["total_assignments"]) for item in rows),
        "xlsx_sha256": xlsx_sha256,
        "docx_sha256": docx_sha256,
        "evaluator_output_sha256": hashlib.sha256(evaluator_text.encode()).hexdigest(),
        "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
