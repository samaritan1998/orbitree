#!/usr/bin/env python3
"""Run one explicit eight-record Miaoda ORS batch and write bounded evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from research_ops.agentos import AgentOSClient
from research_ops.openreward import ORSClient, ORSClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(
    "/srv/research-ops/projects/"
    "example-app-data/data/miaoda_raw_query/"
    "miaoda_raw_query_canary8.jsonl"
)


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    filtered = "".join(
        character if character.isalnum() or character in "._:-" else "_"
        for character in text
    )
    return filtered[:200]


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_tasks(path: Path, *, count: int, optimizer_batch_id: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                continue
            domain = str(value.get("domain") or "").strip().casefold()
            task = value.get("task") if domain else value
            if domain and domain != "miaoda":
                continue
            if not isinstance(task, dict) or not str(task.get("query") or "").strip():
                continue
            source_id = _safe_id(
                task.get("episode_id") or task.get("task_id") or task.get("id")
            )
            if not source_id:
                continue
            member_index = len(tasks)
            occurrence_id = f"{source_id}:batch-canary:{optimizer_batch_id}:{member_index}"
            prepared = dict(task)
            prepared.update(
                {
                    "source_episode_id": source_id,
                    "episode_id": occurrence_id,
                    "training_objective": "pure_opd",
                    "task_reward_required": False,
                    "optimizer_batch_id": optimizer_batch_id,
                    "optimizer_batch_expected_count": count,
                    "optimizer_batch_member_index": member_index,
                }
            )
            tasks.append(prepared)
            if len(tasks) == count:
                break
    if len(tasks) != count:
        raise RuntimeError(f"Miaoda source contains {len(tasks)} usable tasks, expected {count}")
    if len({task["episode_id"] for task in tasks}) != count:
        raise RuntimeError("Miaoda canary episode IDs are not unique")
    return tasks


def _tool_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("ors_tool_result_not_object")
    if value.get("ok") is False:
        raise RuntimeError(f"ors_tool_error:{_safe_id(value.get('reason') or value.get('error'))}")
    output = value.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("ors_tool_output_missing")
    return output


def _metadata(output: dict[str, Any]) -> dict[str, Any]:
    value = output.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _policy_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    calls = metadata.get("policy_calls")
    statuses = metadata.get("miaoda_policy_http_statuses")
    if not isinstance(calls, list) or not calls:
        raise RuntimeError("policy_capture_missing")
    if not isinstance(statuses, list) or len(statuses) != len(calls):
        raise RuntimeError("policy_http_status_count_mismatch")
    if not all(isinstance(value, int) and 200 <= value < 300 for value in statuses):
        raise RuntimeError("policy_http_non_2xx")
    prompt_tokens = 0
    response_tokens = 0
    behavior_logprobs = 0
    versions: set[str] = set()
    for call in calls:
        if not isinstance(call, dict):
            raise RuntimeError("policy_capture_invalid")
        prompt_ids = call.get("prompt_token_ids")
        response_ids = call.get("response_token_ids")
        logprobs = call.get("behavior_logprobs")
        if (
            not isinstance(prompt_ids, list)
            or not prompt_ids
            or not isinstance(response_ids, list)
            or not response_ids
            or not isinstance(logprobs, list)
            or len(response_ids) != len(logprobs)
            or any(not math.isfinite(float(value)) for value in logprobs)
        ):
            raise RuntimeError("policy_token_logprob_join_invalid")
        prompt_tokens += len(prompt_ids)
        response_tokens += len(response_ids)
        behavior_logprobs += len(logprobs)
        version = _safe_id(call.get("weight_version"))
        if version:
            versions.add(version)
    return {
        "policy_http_all_2xx": True,
        "policy_http_statuses": statuses,
        "policy_call_count": len(calls),
        "prompt_token_count": prompt_tokens,
        "response_token_count": response_tokens,
        "behavior_logprob_count": behavior_logprobs,
        "weight_versions": sorted(versions),
    }


def _trajectory_message_count(value: Any) -> int:
    maximum = 0

    def visit(node: Any) -> None:
        nonlocal maximum
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"messages", "coding_messages_all"} and isinstance(child, list):
                    maximum = max(maximum, len(child))
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return maximum


def _dashboard_matches(client: AgentOSClient, *, task_name: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for page in range(1, 11):
        rows = client.list_batches(page=page, page_size=100)
        for row in rows:
            name = _safe_id(row.get("task_name") or row.get("taskName") or row.get("name"))
            deleted = row.get("deleted")
            if name == task_name and deleted not in {True, "1", "true", "True"}:
                matches.append(
                    {
                        "batch_task_id": _safe_id(
                            row.get("batch_task_id") or row.get("batchTaskId") or row.get("id")
                        ),
                        "task_name": name,
                        "status": _safe_id(row.get("status")),
                    }
                )
        if len(rows) < 100:
            break
    return matches


def _progress(
    *,
    optimizer_batch_id: str,
    started_at: datetime,
    expected_count: int,
    outputs: list[dict[str, Any]],
    polls: int,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for index, output in enumerate(outputs):
        metadata = _metadata(output)
        records.append(
            {
                "member_index": index,
                "finished": bool(output.get("finished")),
                "usable": bool(metadata.get("usable")),
                "phase": _safe_id(metadata.get("phase")),
                "upstream_status": _safe_id(metadata.get("status")),
                "episode_id": _safe_id(metadata.get("episode_id")),
                "capture_episode_id": _safe_id(
                    metadata.get("miaoda_capture_episode_id")
                ),
                "batch_task_id": _safe_id(metadata.get("miaoda_batch_task_id")),
                "terminal_reason": _safe_id(metadata.get("terminal_reason")),
            }
        )
    return {
        "schema_version": "openreward.miaoda_explicit_batch_canary.v1",
        "status": "running",
        "started_at": started_at.isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "optimizer_batch_id": optimizer_batch_id,
        "expected_count": expected_count,
        "polls": polls,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ors-url", default="http://127.0.0.1:8022")
    parser.add_argument("--task-source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--ors-token-env", default="ORS_API_KEY")
    parser.add_argument("--datawheel-token-env", default="DATAWHEEL_TOKEN")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=720)
    args = parser.parse_args()
    if args.count != 8:
        raise SystemExit("acceptance canary requires exactly eight records")
    if args.poll_interval < 0.1 or args.max_polls < 1:
        raise SystemExit("poll limits must be positive")
    for name in (args.ors_token_env, args.datawheel_token_env):
        if not os.environ.get(name, "").strip():
            raise SystemExit(f"runtime credential reference is empty: {name}")
    datawheel_url = str(os.environ.get("ROPS_AGENTOS_DATAWHEEL_BASE_URL", "")).strip()
    if not datawheel_url:
        raise SystemExit("runtime reference is empty: ROPS_AGENTOS_DATAWHEEL_BASE_URL")

    optimizer_batch_id = f"canary8-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    sessions = []
    outputs: list[dict[str, Any]] = []
    try:
        tasks = _read_tasks(
            args.task_source.resolve(strict=True),
            count=args.count,
            optimizer_batch_id=optimizer_batch_id,
        )
        ors = ORSClient(
            args.ors_url,
            timeout_seconds=300,
            token_env=args.ors_token_env,
            trust_env=False,
        )
        sessions = [ors.session("miaoda", task_spec=task) for task in tasks]
        with ThreadPoolExecutor(max_workers=args.count) as executor:
            starts = list(executor.map(lambda session: session.call("start_rollout", {}), sessions))
        outputs = [_tool_output(value) for value in starts]

        start_metadata = [_metadata(output) for output in outputs]
        batch_ids = {_safe_id(value.get("miaoda_batch_task_id")) for value in start_metadata}
        task_names = {_safe_id(value.get("miaoda_task_name")) for value in start_metadata}
        capture_ids = {
            _safe_id(value.get("miaoda_capture_episode_id")) for value in start_metadata
        }
        member_indexes = {
            value.get("miaoda_optimizer_batch_member_index") for value in start_metadata
        }
        if "" in batch_ids or len(batch_ids) != 1:
            raise RuntimeError("start_outputs_do_not_share_one_batch")
        if "" in task_names or len(task_names) != 1:
            raise RuntimeError("start_outputs_do_not_share_one_task_name")
        if "" in capture_ids or len(capture_ids) != args.count:
            raise RuntimeError("capture_episode_ids_not_unique")
        if member_indexes != set(range(args.count)):
            raise RuntimeError("optimizer_member_indexes_invalid")
        if {
            value.get("miaoda_optimizer_batch_expected_count") for value in start_metadata
        } != {args.count}:
            raise RuntimeError("optimizer_expected_count_invalid")

        polls = 0
        while polls < args.max_polls and not all(
            bool(output.get("finished")) for output in outputs
        ):
            indexes = [
                index for index, output in enumerate(outputs) if not bool(output.get("finished"))
            ]
            with ThreadPoolExecutor(max_workers=len(indexes)) as executor:
                values = list(
                    executor.map(
                        lambda index: sessions[index].call("poll_rollout", {}), indexes
                    )
                )
            for index, value in zip(indexes, values, strict=True):
                outputs[index] = _tool_output(value)
            polls += 1
            _write(
                args.output,
                _progress(
                    optimizer_batch_id=optimizer_batch_id,
                    started_at=started_at,
                    expected_count=args.count,
                    outputs=outputs,
                    polls=polls,
                ),
            )
            if not all(bool(output.get("finished")) for output in outputs):
                time.sleep(args.poll_interval)
        if not all(bool(output.get("finished")) for output in outputs):
            raise RuntimeError("polling_limit_exceeded")

        datawheel = AgentOSClient(
            datawheel_url,
            token_env=args.datawheel_token_env,
            timeout_seconds=180,
            trust_env=False,
        )
        batch_task_id = next(iter(batch_ids))
        submitted_task_name = next(iter(task_names))
        snapshot = datawheel.get_batch(batch_task_id)
        task_name = _safe_id(
            snapshot.raw.get("task_name")
            or snapshot.raw.get("taskName")
            or snapshot.raw.get("name")
        )
        if not task_name:
            raise RuntimeError("agentos_dashboard_task_name_missing")
        dashboard_matches = _dashboard_matches(datawheel, task_name=task_name)
        batch_records = datawheel.list_batch_records(batch_task_id)
        if len(batch_records) != args.count:
            raise RuntimeError("agentos_record_count_is_not_eight")
        records_by_case_id = {
            int(record["case_id"]): record
            for record in batch_records
            if isinstance(record.get("case_id"), int)
            and not isinstance(record.get("case_id"), bool)
        }
        if set(records_by_case_id) != set(range(1, args.count + 1)):
            raise RuntimeError("agentos_case_ids_are_not_one_through_eight")
        if any(
            record.get("remaining_auto_retry_count") != 0
            for record in batch_records
        ):
            raise RuntimeError("agentos_auto_retry_was_not_disabled")
        if any(
            str(record.get("status") or "").strip().casefold() != "completed"
            for record in batch_records
        ):
            raise RuntimeError("agentos_record_not_completed")

        record_evidence: list[dict[str, Any]] = []
        app_ids: set[str] = set()
        for index, output in enumerate(outputs):
            metadata = _metadata(output)
            if not bool(metadata.get("usable")):
                raise RuntimeError(f"episode_{index}_not_usable")
            policy = _policy_summary(metadata)
            app_id = _safe_id(metadata.get("miaoda_app_id"))
            if not app_id:
                raise RuntimeError(f"episode_{index}_missing_app_id")
            trajectory = datawheel.get_trajectory(app_id)
            trajectory_message_count = _trajectory_message_count(trajectory)
            if trajectory_message_count < 1:
                raise RuntimeError(f"episode_{index}_trajectory_missing")
            app_ids.add(app_id)
            record_evidence.append(
                {
                    "member_index": index,
                    "episode_id": _safe_id(metadata.get("episode_id")),
                    "capture_episode_id": _safe_id(
                        metadata.get("miaoda_capture_episode_id")
                    ),
                    "app_id": app_id,
                    "agentos_remaining_auto_retry_count": records_by_case_id[
                        index + 1
                    ].get("remaining_auto_retry_count"),
                    "trajectory_message_count": trajectory_message_count,
                    "trajectory_logprob_joined": True,
                    **policy,
                }
            )

        acceptance = {
            "dashboard_task_count": len(dashboard_matches),
            "dashboard_exactly_one_task": len(dashboard_matches) == 1,
            "total_size": snapshot.total_size,
            "total_size_is_8": snapshot.total_size == args.count,
            "independent_episode_ids": len(capture_ids) == args.count,
            "independent_app_ids": len(app_ids) == args.count,
            "agentos_auto_retry_disabled": all(
                item["agentos_remaining_auto_retry_count"] == 0
                for item in record_evidence
            ),
            "policy_all_2xx": all(
                item["policy_http_all_2xx"] for item in record_evidence
            ),
            "trajectory_logprob_joins": sum(
                bool(item["trajectory_logprob_joined"]) for item in record_evidence
            ),
        }
        passed = (
            acceptance["dashboard_exactly_one_task"]
            and acceptance["total_size_is_8"]
            and acceptance["independent_episode_ids"]
            and acceptance["independent_app_ids"]
            and acceptance["agentos_auto_retry_disabled"]
            and acceptance["policy_all_2xx"]
            and acceptance["trajectory_logprob_joins"] == args.count
        )
        evidence = {
            "schema_version": "openreward.miaoda_explicit_batch_canary.v1",
            "status": "passed" if passed else "failed",
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "optimizer_batch_id": optimizer_batch_id,
            "batch_task_id": batch_task_id,
            "task_name": task_name,
            "submitted_task_name": submitted_task_name,
            "upstream_status": snapshot.status,
            "acceptance": acceptance,
            "dashboard_matches": dashboard_matches,
            "records": record_evidence,
        }
        _write(args.output, evidence)
        print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 0 if passed else 3
    except (ORSClientError, OSError, RuntimeError, ValueError) as exc:
        evidence = _progress(
            optimizer_batch_id=optimizer_batch_id,
            started_at=started_at,
            expected_count=args.count,
            outputs=outputs,
            polls=0,
        )
        evidence.update(
            {
                "status": "failed",
                "completed_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
                "error_reason": _safe_id(exc),
            }
        )
        _write(args.output, evidence)
        print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), flush=True)
        return 2
    finally:
        for session in sessions:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
