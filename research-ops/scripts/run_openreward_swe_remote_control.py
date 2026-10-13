#!/usr/bin/env python3
"""Run one empty-patch SWE control episode through the deployed ORS bridge.

This deliberately does not send a policy response.  It exercises the real
ORS -> Kubernetes SWE sandbox -> clean official verifier path and should
return the canonical zero reward for an untouched checkout.  The output is
compact evidence only; task patches, test logs, credentials, and full prompts
are never written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"missing runtime reference: {name}")
    return value


def _sse_output(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if payload.get("ok") is True:
            output = payload.get("output")
            if isinstance(output, dict):
                return dict(output)
        if payload.get("ok") is False:
            raise RuntimeError("ORS tool call returned an error")
    raise RuntimeError("ORS tool call omitted a terminal SSE payload")


def _compact_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "runtime",
        "harness_version",
        "resolved",
        "patch_applied",
        "patch_sha256",
        "patch_bytes",
        "fail_to_pass",
        "fail_to_pass_passed",
        "fail_to_pass_total",
        "pass_to_pass",
        "pass_to_pass_passed",
        "pass_to_pass_total",
        "eval_exit_code",
        "eval_timed_out",
        "verifier",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
    return result


def run(
    *,
    base_url: str,
    index: int,
    timeout_seconds: float,
    task_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = _required("ORS_API_KEY")
    headers = {"X-API-Key": api_key}
    started = datetime.now(UTC)
    base = base_url.rstrip("/")
    evidence: dict[str, Any] = {
        "version": 1,
        "created_at": started.isoformat(),
        "environment": "swe",
        "split": "train",
        "index": index,
        "control": "empty_patch",
        "ors_url_sha256": hashlib.sha256(base.encode()).hexdigest(),
    }
    sid = ""
    with httpx.Client(
        base_url=base,
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds, connect=30.0),
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        created = client.post("/create_session")
        created.raise_for_status()
        sid = str(created.json()["sid"])
        session_headers = {**headers, "X-Session-ID": sid}
        try:
            configured = client.post(
                "/create",
                headers=session_headers,
                json=(
                    {"env_name": "swe", "task_spec": task_spec}
                    if task_spec is not None
                    else {"env_name": "swe", "split": "train", "index": index}
                ),
            )
            configured.raise_for_status()
            prompt = client.get("/swe/prompt", headers=session_headers)
            prompt.raise_for_status()
            tools = client.get("/swe/task_tools", headers=session_headers)
            tools.raise_for_status()
            submitted = client.post(
                "/swe/call",
                headers=session_headers,
                json={"name": "submit", "input": {}},
            )
            output = _sse_output(submitted)
            metadata = _compact_metadata(output.get("metadata"))
            evidence.update(
                {
                    "status": "completed" if output.get("finished") else "failed",
                    "finished": bool(output.get("finished")),
                    "reward": output.get("reward"),
                    "prompt_block_count": len(prompt.json()),
                    "tool_count": len(tools.json().get("tools", [])),
                    "terminal_metadata": metadata,
                }
            )
        finally:
            if sid:
                with httpx.Client(
                    base_url=base,
                    headers=session_headers,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    trust_env=False,
                ) as cleanup:
                    cleanup.post("/delete", headers=session_headers)
    evidence["duration_seconds"] = (datetime.now(UTC) - started).total_seconds()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ORS_URL", ""))
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument(
        "--task-spec",
        type=Path,
        default=(Path(os.environ["SWE_TASK_SPEC_PATH"])
                 if str(os.environ.get("SWE_TASK_SPEC_PATH", "")).strip()
                 else None),
        help="optional task-spec JSON; overrides split/index selection",
    )
    parser.add_argument("--timeout-seconds", type=float, default=2100.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not str(args.base_url).strip():
        raise RuntimeError("--base-url or ORS_URL is required")
    if args.index < 0:
        raise RuntimeError("--index must be non-negative")
    task_spec: dict[str, Any] | None = None
    if args.task_spec is not None:
        payload = json.loads(args.task_spec.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("--task-spec must contain a JSON object")
        environments = payload.get("environments")
        if isinstance(environments, dict):
            swe = environments.get("swe")
            train = swe.get("train") if isinstance(swe, dict) else None
            if not isinstance(train, list) or args.index >= len(train):
                raise RuntimeError("SWE task catalog does not contain the selected index")
            selected = train[args.index]
            if not isinstance(selected, dict):
                raise RuntimeError("selected SWE catalog task is not an object")
            task_spec = selected
        else:
            task_spec = payload
    try:
        evidence = run(
            base_url=str(args.base_url),
            index=args.index,
            timeout_seconds=args.timeout_seconds,
            task_spec=task_spec,
        )
    except Exception as exc:
        detail = re.sub(r"https?://\S+", "<url>", str(exc).strip())
        evidence = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "environment": "swe",
            "split": "train",
            "index": args.index,
            "control": "empty_patch",
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": detail[:300] or type(exc).__name__,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0 if evidence.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
