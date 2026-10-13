#!/usr/bin/env python3
"""Run real SWE-bench control episodes through the complete ORS HTTP bridge."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
from fastapi.testclient import TestClient
from research_ops.openreward import (
    RemoteServiceConfig,
    SweSandboxBackend,
    build_default_environments,
    create_openreward_app,
    load_task_catalogs,
)
from research_ops.openreward.swe_runtime import (
    DockerSweEngine,
    OfficialSwebenchHarness,
    SweBenchTask,
    SweRuntimeConfig,
    SweRuntimeService,
    create_swe_runtime_app,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-swe-canary.json"
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "data"
    / "example-swe-manifests"
    / "manifests"
    / "swebench_test.parquet"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data"
    / "openreward_swe_canary_20260720"
    / "evidence.json"
)
DEFAULT_HARNESS = Path("/srv/research-ops/tools/SWE-bench")


def _sse_output(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            payload = json.loads(line[6:])
            if payload.get("ok") is not True:
                raise RuntimeError("ORS action failed")
            return dict(payload["output"])
    raise RuntimeError("ORS response omitted terminal SSE payload")


def _gold_patch(path: Path, task_id: str, expected_sha256: str) -> str:
    with duckdb.connect() as connection:
        row = connection.execute(
            "SELECT patch FROM read_parquet(?) WHERE instance_id = ?",
            [str(path), task_id],
        ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("gold control patch is absent from the evaluator manifest")
    patch = row[0]
    if hashlib.sha256(patch.encode()).hexdigest() != expected_sha256:
        raise RuntimeError("gold control patch digest does not match joined provenance")
    return patch


def _episode(client: TestClient, task: dict[str, Any], patch: str | None) -> dict[str, Any]:
    sid = client.post("/create_session").json()["sid"]
    headers = {"X-Session-ID": sid}
    started = datetime.now(UTC)
    try:
        created = client.post(
            "/create",
            headers=headers,
            json={"env_name": "swe", "task_spec": task},
        )
        created.raise_for_status()
        prompt = client.get("/swe/prompt", headers=headers)
        prompt.raise_for_status()
        tools = client.get("/swe/task_tools", headers=headers)
        tools.raise_for_status()
        if patch is not None:
            control_path = "/testbed/.ors-evaluator-control.patch"
            wrote = client.post(
                "/swe/call",
                headers=headers,
                json={
                    "name": "write_file",
                    "input": {"path": control_path, "content": patch},
                },
            )
            _sse_output(wrote)
            applied = client.post(
                "/swe/call",
                headers=headers,
                json={
                    "name": "bash",
                    "input": {
                        "command": (
                            "git apply /testbed/.ors-evaluator-control.patch && "
                            "rm /testbed/.ors-evaluator-control.patch"
                        )
                    },
                },
            )
            if _sse_output(applied)["metadata"]["exit_code"] != 0:
                raise RuntimeError("gold control patch could not be applied")
        submitted = client.post(
            "/swe/call",
            headers=headers,
            json={"name": "submit", "input": {}},
        )
        output = _sse_output(submitted)
        metadata = dict(output.get("metadata") or {})
        return {
            "control": "gold_patch" if patch is not None else "empty_patch",
            "control_input_sha256": hashlib.sha256((patch or "").encode()).hexdigest(),
            "reward": output.get("reward"),
            "finished": output.get("finished"),
            "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
            "runtime": metadata.get("runtime"),
            "harness_version": metadata.get("harness_version"),
            "resolved": metadata.get("resolved"),
            "patch_applied": metadata.get("patch_applied"),
            "patch_sha256": metadata.get("patch_sha256"),
            "patch_bytes": metadata.get("patch_bytes"),
            "fail_to_pass": metadata.get("fail_to_pass"),
            "fail_to_pass_passed": metadata.get("fail_to_pass_passed"),
            "fail_to_pass_total": metadata.get("fail_to_pass_total"),
            "pass_to_pass": metadata.get("pass_to_pass"),
            "pass_to_pass_passed": metadata.get("pass_to_pass_passed"),
            "pass_to_pass_total": metadata.get("pass_to_pass_total"),
            "eval_exit_code": metadata.get("eval_exit_code"),
            "eval_timed_out": metadata.get("eval_timed_out"),
            "prompt_blocks": len(prompt.json()),
            "tool_count": len(tools.json()["tools"]),
        }
    finally:
        client.post("/delete", headers=headers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--harness-root", type=Path, default=DEFAULT_HARNESS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--mode", choices=("empty", "gold", "both"), default="both")
    args = parser.parse_args()

    catalog = load_task_catalogs(args.catalog)
    tasks = list(catalog["swe"]["train"])
    task = dict(tasks[args.index])
    parsed = SweBenchTask.from_spec(task)
    harness = OfficialSwebenchHarness(harness_root=args.harness_root)
    config = SweRuntimeConfig.from_environ()
    service = SweRuntimeService(
        DockerSweEngine(config),
        harness,
        ttl_seconds=config.session_ttl_seconds,
    )
    sidecar = create_swe_runtime_app(service)
    transport = httpx.ASGITransport(app=sidecar)
    backend = SweSandboxBackend(
        RemoteServiceConfig(
            environment="swe",
            base_url_env="SWE_CANARY_URL",
            create_operation="POST /v1/sandboxes",
            action_operations={
                "bash": "POST /v1/sandboxes/{sandbox_id}/exec",
                "read_file": "POST /v1/sandboxes/{sandbox_id}/files/read",
                "write_file": "POST /v1/sandboxes/{sandbox_id}/files/write",
                "submit": "POST /v1/sandboxes/{sandbox_id}/verify",
            },
            destroy_operation="DELETE /v1/sandboxes/{sandbox_id}",
            terminal_actions=frozenset({"submit"}),
            timeout_seconds=config.verify_seconds + 300,
        ),
        environ={"SWE_CANARY_URL": "http://swe-runtime.local"},
        transport=transport,
    )
    ors = create_openreward_app(
        build_default_environments(
            backends={"swe": backend},
            task_catalogs={"swe": {"train": [task]}},
        )
    )

    controls: list[tuple[str, str | None]] = []
    if args.mode in {"empty", "both"}:
        controls.append(("empty", None))
    if args.mode in {"gold", "both"}:
        controls.append(
            (
                "gold",
                _gold_patch(args.manifest, parsed.task_id, str(task["gold_patch_sha256"])),
            )
        )
    results: list[dict[str, Any]] = []
    with TestClient(ors) as client:
        for _, patch in controls:
            results.append(_episode(client, task, patch))
    asyncio.run(service.shutdown())

    evidence = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": parsed.task_id,
        "repo": parsed.repo,
        "base_commit": parsed.base_commit,
        "image_ref": harness.plan(parsed).image_ref,
        "image_id": service.engine.image_id(harness.plan(parsed).image_ref),
        "harness_version": harness.version,
        "controls": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
