#!/usr/bin/env python3
"""Drive one real Miaoda ORS episode without allocating a training pod.

The policy and teacher services remain external. This controller only creates
and polls the AgentOS/Datawheel episode through the ORS API. It writes bounded,
text-free progress evidence and never persists credentials or model text.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_ops.openreward import ORSClient, ORSClientError


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    return "".join(
        char if (char.isalnum() or char in "._:-") else "_" for char in text
    )[:160]


def _read_task(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Miaoda task spec must be an object")
    if not str(value.get("query") or "").strip():
        raise RuntimeError("Miaoda task spec requires a query")
    if str(value.get("training_objective") or "").strip() != "pure_opd":
        raise RuntimeError("API controller only accepts pure_opd tasks")
    if value.get("task_reward_required") is not False:
        raise RuntimeError("pure_opd task must set task_reward_required=false")
    return dict(value)


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _tool_output(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("ors_tool_result_not_object")
    if result.get("ok") is False:
        reason = _safe_id(result.get("reason") or result.get("error") or "remote_tool_error")
        raise RuntimeError(f"ors_tool_error:{reason}")
    output = result.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("ors_tool_output_missing")
    return output


def _progress(
    *,
    episode_id: str,
    polls: int,
    output: dict[str, Any],
    started_at: datetime,
) -> dict[str, Any]:
    metadata = output.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    value: dict[str, Any] = {
        "version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "started_at": started_at.isoformat(),
        "episode_id": _safe_id(episode_id),
        "status": "completed" if bool(output.get("finished")) else "running",
        "finished": bool(output.get("finished")),
        "polls": polls,
        "phase": _safe_id(metadata.get("phase")),
        "upstream_status": _safe_id(metadata.get("status")),
        "training_objective": "pure_opd",
        "task_reward_used": False,
        "goblin_job_submitted": False,
    }
    for key in ("miaoda_batch_task_id", "batch_task_id", "batch_id"):
        identifier = _safe_id(metadata.get(key))
        if identifier:
            value["miaoda_batch_task_id"] = identifier
            break
    if bool(output.get("finished")):
        value["usable"] = bool(metadata.get("usable"))
        value["terminal_reason"] = _safe_id(
            metadata.get("reason") or metadata.get("terminal_reason")
        )
        artifact = str(metadata.get("miaoda_artifact") or "").strip()
        if artifact:
            value["miaoda_artifact"] = artifact
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ors-url", required=True)
    parser.add_argument("--task-spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-env", default="ORS_API_KEY")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=720)
    args = parser.parse_args()
    if args.max_polls < 1 or args.poll_interval < 0.1:
        raise SystemExit("poll limits must be positive")
    if not os.environ.get(args.token_env, "").strip():
        raise SystemExit(f"runtime credential reference is empty: {args.token_env}")

    task = _read_task(args.task_spec)
    episode_id = str(task.get("episode_id") or "miaoda-api-controller")
    started_at = datetime.now(UTC)
    client = ORSClient(
        args.ors_url,
        timeout_seconds=180.0,
        token_env=args.token_env,
        trust_env=False,
    )
    polls = 0
    try:
        with client.session("miaoda", task_spec=task) as session:
            call = session.call("start_rollout", {})
            while polls < args.max_polls:
                result = call if polls == 0 else session.call("poll_rollout", {})
                output = _tool_output(result)
                polls += 1
                progress = _progress(
                    episode_id=episode_id,
                    polls=polls,
                    output=output,
                    started_at=started_at,
                )
                _write(args.output, progress)
                print(json.dumps(progress, ensure_ascii=False, separators=(",", ":")), flush=True)
                if progress["finished"]:
                    return 0 if progress.get("usable") else 3
                time.sleep(args.poll_interval)
    except (ORSClientError, OSError, RuntimeError) as exc:
        _write(
            args.output,
            {
                "version": 1,
                "updated_at": datetime.now(UTC).isoformat(),
                "started_at": started_at.isoformat(),
                "episode_id": _safe_id(episode_id),
                "status": "failed",
                "finished": False,
                "polls": polls,
                "error_type": type(exc).__name__,
                "error_reason": _safe_id(exc),
                "training_objective": "pure_opd",
                "task_reward_used": False,
                "goblin_job_submitted": False,
            },
        )
        return 2
    _write(
        args.output,
        {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "started_at": started_at.isoformat(),
            "episode_id": _safe_id(episode_id),
            "status": "failed",
            "finished": False,
            "polls": polls,
            "error_type": "PollingLimitExceeded",
            "training_objective": "pure_opd",
            "task_reward_used": False,
            "goblin_job_submitted": False,
        },
    )
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
