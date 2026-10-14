#!/usr/bin/env python3
"""Run an empty-state Toolathlon control through the Orbitree ORS proxy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from research_ops.openreward import (
    RemoteORSBackend,
    RemoteORSConfig,
    build_default_environments,
    create_openreward_app,
    load_task_catalogs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PINNED_REVISION = "9c82859d05bc1f0376150d749cd2e3d43471d583"
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-tooluse-canary.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data"
    / "openreward_tooluse_canary_20260720"
    / "empty_state_evidence.json"
)
_SESSION_DATABASE = re.compile(r"^s_[0-9a-f]{16}$")
_TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_INFRASTRUCTURE_FAILURES = (
    "Traceback (most recent call last)",
    "Evaluation error:",
    "connection to server",
    "could not connect to server",
    "FATAL:",
)


def _sse_output(response: object) -> dict[str, object]:
    text = str(getattr(response, "text", ""))
    for line in text.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            payload = json.loads(line[6:])
            if payload.get("ok") is not True:
                raise RuntimeError("ToolUse ORS action failed")
            return dict(payload["output"])
    raise RuntimeError("ToolUse ORS response omitted terminal SSE payload")


def _output_text(output: dict[str, object]) -> str:
    blocks = output.get("blocks") or []
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _database_probe(client: TestClient, headers: dict[str, str]) -> str:
    code = """import os
import psycopg2

name = os.environ.get("PGDATABASE", "")
with psycopg2.connect(
    host=os.environ.get("PGHOST", "localhost"),
    port=5432,
    dbname=name,
    user="eigent",
    password="camel",
) as connection:
    with connection.cursor() as cursor:
        cursor.execute("select current_database()")
        current = cursor.fetchone()[0]
print(f"PGDATABASE={name}")
print(f"current_database={current}")
"""
    output = _sse_output(
        client.post(
            "/tooluse/call",
            headers=headers,
            json={"name": "python_execute", "input": {"code": code}},
        )
    )
    text = _output_text(output)
    values = dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line.startswith(("PGDATABASE=", "current_database="))
    )
    database = values.get("PGDATABASE", "")
    if not _SESSION_DATABASE.fullmatch(database):
        raise RuntimeError("Toolathlon did not inject a per-session database")
    if values.get("current_database") != database:
        raise RuntimeError("Toolathlon process is not connected to its session database")
    return database


def _mcp_probe(
    client: TestClient,
    headers: dict[str, str],
    probe: dict[str, object],
) -> tuple[str, str]:
    name = str(probe.get("name") or "").strip()
    expected_text = str(probe.get("expected_text") or "").strip()
    arguments = probe.get("arguments") or {}
    if not name or not expected_text or not isinstance(arguments, dict):
        raise RuntimeError("Toolathlon MCP health probe is invalid")
    output = _sse_output(
        client.post(
            "/tooluse/call",
            headers=headers,
            json={
                "name": "call_tool",
                "input": {"name": name, "arguments": arguments},
            },
        )
    )
    text = _output_text(output)
    if expected_text.casefold() not in text.casefold():
        raise RuntimeError("Toolathlon MCP health probe returned an unexpected result")
    return name, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _empty_control(
    client: TestClient,
    *,
    index: int,
    task: dict[str, object],
) -> dict[str, object]:
    started = datetime.now(UTC)
    sid = client.post("/create_session").json()["sid"]
    headers = {"X-Session-ID": sid}
    try:
        created = client.post(
            "/create",
            headers=headers,
            json={"env_name": "tooluse", "split": "train", "index": index},
        )
        created.raise_for_status()
        prompt = client.get("/tooluse/prompt", headers=headers)
        prompt.raise_for_status()
        tools = client.get("/tooluse/task_tools", headers=headers)
        tools.raise_for_status()
        names = [str(item["name"]) for item in tools.json()["tools"]]
        if "claim_done" not in names:
            raise RuntimeError("official Toolathlon tools omitted claim_done")
        database = _database_probe(client, headers)
        probe_name = None
        probe_output_sha256 = None
        raw_probe = task.get("canary_probe")
        if isinstance(raw_probe, dict):
            probe_name, probe_output_sha256 = _mcp_probe(client, headers, raw_probe)
        output = _sse_output(
            client.post(
                "/tooluse/call",
                headers=headers,
                json={"name": "claim_done", "input": {}},
            )
        )
        evaluator_text = _output_text(output)
        if any(
            marker.casefold() in evaluator_text.casefold()
            for marker in _INFRASTRUCTURE_FAILURES
        ):
            raise RuntimeError("Toolathlon evaluator reported an infrastructure failure")
    finally:
        client.post("/delete", headers=headers)
    control = {
        "task_id": task["task_id"],
        "source_revision": dict(task["source"])["revision"],
        "control": "claim_done_after_health_probes",
        "reward": output.get("reward"),
        "finished": output.get("finished"),
        "session_database_prefix": database.split("_", 1)[0] + "_",
        "evaluator_output_sha256": hashlib.sha256(evaluator_text.encode("utf-8")).hexdigest(),
        "evaluator_infrastructure_error": False,
        "mcp_probe_tool": probe_name,
        "mcp_probe_output_sha256": probe_output_sha256,
        "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
        "prompt_blocks": len(prompt.json()),
        "tool_count": len(names),
        "tool_names": names,
    }
    if control["reward"] != 0.0 or control["finished"] is not True:
        raise RuntimeError("empty Toolathlon control must terminate with reward 0")
    return control


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--task-name",
        help="Run one supplemental official Toolathlon task outside the catalog",
    )
    parser.add_argument("--service-url-env", default="ORS_TOOLUSE_SERVICE_URL")
    parser.add_argument("--service-token-env")
    args = parser.parse_args()

    service_url = str(os.environ.get(args.service_url_env, "")).strip()
    if not service_url:
        raise SystemExit(f"missing Toolathlon service reference: {args.service_url_env}")
    with httpx.Client(base_url=service_url, timeout=30, trust_env=False) as remote:
        health = remote.get("/health")
        version = remote.get("/version")
    health.raise_for_status()
    version.raise_for_status()
    task_name = str(args.task_name or "").strip()
    if task_name:
        if args.all:
            raise SystemExit("--task-name and --all are mutually exclusive")
        if not _TASK_NAME.fullmatch(task_name):
            raise SystemExit("--task-name is invalid")
        tasks = [
            {
                "task_id": task_name,
                "task_name": task_name,
                "query": "Use the prompt supplied by the official Toolathlon environment.",
                "source": {
                    "repository": "EnvCommons/toolathlon-gym",
                    "revision": PINNED_REVISION,
                },
                "canary_probe": {
                    "name": "google_forms.get_form",
                    "arguments": {"formId": "ors-health-probe-missing"},
                    "expected_text": "Form not found",
                },
            }
        ]
    else:
        tasks = [
            dict(item)
            for item in load_task_catalogs(args.catalog)["tooluse"]["train"]
        ]
    indexes = list(range(len(tasks))) if args.all else [args.index]
    auth = (
        {"Authorization": args.service_token_env}
        if args.service_token_env
        else {}
    )
    backend = RemoteORSBackend(
        RemoteORSConfig(
            environment="tooluse",
            remote_environment="toolathlongym",
            base_url_env=args.service_url_env,
            auth_header_envs=auth,
            timeout_seconds=300,
        )
    )
    app = create_openreward_app(
        build_default_environments(
            backends={"tooluse": backend},
            task_catalogs={"tooluse": {"train": tasks}},
        )
    )

    with TestClient(app) as client:
        controls = [
            _empty_control(client, index=index, task=tasks[index])
            for index in indexes
        ]

    evidence = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "remote_health": health.json(),
        "remote_version": version.json(),
        "controls": controls,
    }
    output = args.output or DEFAULT_OUTPUT
    if task_name and args.output is None:
        output = DEFAULT_OUTPUT.with_name(f"{task_name}_evidence.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
