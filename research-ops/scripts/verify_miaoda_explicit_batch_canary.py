#!/usr/bin/env python3
"""Verify an already-submitted Miaoda B8 canary without creating another job."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from research_ops.agentos import AgentOSClient
from research_ops.openreward.miaoda_online import (
    _capture_file_name,
    _load_policy_capture_records,
    _load_policy_http_statuses,
    _policy_call_from_capture,
)
from run_miaoda_explicit_batch_canary import (
    _dashboard_matches,
    _policy_summary,
    _safe_id,
    _trajectory_message_count,
    _write,
)


def _read_start_evidence(path: Path) -> tuple[str, datetime, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("start evidence is not an object")
    records = value.get("records")
    if not isinstance(records, list) or len(records) != 8:
        raise RuntimeError("start evidence does not contain eight records")
    normalized = [dict(item) for item in records if isinstance(item, dict)]
    if len(normalized) != 8:
        raise RuntimeError("start evidence contains an invalid record")
    batch_ids = {
        _safe_id(item.get("batch_task_id"))
        for item in normalized
        if _safe_id(item.get("batch_task_id"))
    }
    if not batch_ids:
        top_level_batch_id = _safe_id(value.get("batch_task_id"))
        if top_level_batch_id:
            batch_ids = {top_level_batch_id}
    capture_ids = {_safe_id(item.get("capture_episode_id")) for item in normalized}
    if "" in batch_ids or len(batch_ids) != 1:
        raise RuntimeError("start evidence does not identify exactly one batch")
    if "" in capture_ids or len(capture_ids) != 8:
        raise RuntimeError("start evidence does not contain eight capture IDs")
    started_at = datetime.fromisoformat(str(value.get("started_at") or ""))
    return next(iter(batch_ids)), started_at, normalized


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--datawheel-token-env", default="DATAWHEEL_TOKEN")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-polls", type=int, default=720)
    args = parser.parse_args()
    if args.poll_interval < 0.1 or args.max_polls < 1:
        raise SystemExit("poll limits must be positive")
    if not os.environ.get(args.datawheel_token_env, "").strip():
        raise SystemExit(
            f"runtime credential reference is empty: {args.datawheel_token_env}"
        )
    base_url = str(os.environ.get("ROPS_AGENTOS_DATAWHEEL_BASE_URL", "")).strip()
    if not base_url:
        raise SystemExit("runtime reference is empty: ROPS_AGENTOS_DATAWHEEL_BASE_URL")

    batch_task_id, started_at, start_records = _read_start_evidence(
        args.start_evidence.resolve(strict=True)
    )
    capture_dir = args.capture_dir.resolve(strict=True)
    client = AgentOSClient(
        base_url,
        token_env=args.datawheel_token_env,
        timeout_seconds=180,
        trust_env=False,
    )
    snapshot = client.get_batch(batch_task_id)
    polls = 0
    while snapshot.status.casefold() not in {
        "completed",
        "failed",
        "cancelled",
        "canceled",
        "terminated",
        "error",
    }:
        if polls >= args.max_polls:
            raise RuntimeError("polling_limit_exceeded")
        _write(
            args.output,
            {
                "schema_version": "openreward.miaoda_explicit_batch_canary.v1",
                "status": "running",
                "batch_task_id": batch_task_id,
                "upstream_status": snapshot.status,
                "total_size": snapshot.total_size,
                "status_counts": snapshot.status_counts,
                "polls": polls,
            },
        )
        time.sleep(args.poll_interval)
        snapshot = client.get_batch(batch_task_id)
        polls += 1
    if snapshot.status.casefold() != "completed":
        raise RuntimeError(f"agentos_batch_terminal_{_safe_id(snapshot.status)}")
    if snapshot.total_size != 8:
        raise RuntimeError("agentos_batch_total_size_is_not_eight")

    batch_records = client.list_batch_records(batch_task_id)
    if len(batch_records) != 8:
        raise RuntimeError("agentos_record_count_is_not_eight")
    records_by_case_id = {
        int(record["case_id"]): record
        for record in batch_records
        if isinstance(record.get("case_id"), int)
        and not isinstance(record.get("case_id"), bool)
    }
    if set(records_by_case_id) != set(range(1, 9)):
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

    task_name = _safe_id(
        snapshot.raw.get("task_name")
        or snapshot.raw.get("taskName")
        or snapshot.raw.get("name")
    )
    dashboard_matches = _dashboard_matches(client, task_name=task_name)
    record_evidence: list[dict[str, Any]] = []
    app_ids: set[str] = set()
    started_timestamp = started_at.timestamp()
    for item in sorted(start_records, key=lambda value: int(value["member_index"])):
        member_index = int(item["member_index"])
        episode_id = _safe_id(item.get("episode_id"))
        capture_episode_id = _safe_id(item.get("capture_episode_id"))
        capture_path = capture_dir / f"capture-{_capture_file_name(capture_episode_id)}"
        http_path = capture_dir / f"http-{_capture_file_name(capture_episode_id)}"
        records = _load_policy_capture_records(
            capture_path,
            capture_episode_id=capture_episode_id,
            started_at=started_timestamp,
        )
        calls = [
            call
            for value in records
            if (call := _policy_call_from_capture(value)) is not None
        ]
        statuses = _load_policy_http_statuses(
            http_path,
            capture_episode_id=capture_episode_id,
            started_at=started_timestamp,
        )
        policy = _policy_summary(
            {
                "policy_calls": calls,
                "miaoda_policy_http_statuses": statuses,
            }
        )
        batch_record = records_by_case_id[member_index + 1]
        app_id = _safe_id(
            batch_record.get("generated_app_id") or batch_record.get("app_id")
        )
        if not app_id:
            raise RuntimeError(f"episode_{member_index}_missing_app_id")
        trajectory = client.get_trajectory(app_id)
        message_count = _trajectory_message_count(trajectory)
        if message_count < 1:
            raise RuntimeError(f"episode_{member_index}_trajectory_missing")
        app_ids.add(app_id)
        record_evidence.append(
            {
                "member_index": member_index,
                "episode_id": episode_id,
                "capture_episode_id": capture_episode_id,
                "app_id": app_id,
                "agentos_remaining_auto_retry_count": batch_record.get(
                    "remaining_auto_retry_count"
                ),
                "trajectory_message_count": message_count,
                "trajectory_logprob_joined": True,
                **policy,
            }
        )

    acceptance = {
        "dashboard_task_count": len(dashboard_matches),
        "dashboard_exactly_one_task": len(dashboard_matches) == 1,
        "total_size": snapshot.total_size,
        "total_size_is_8": snapshot.total_size == 8,
        "independent_episode_ids": len(
            {item["capture_episode_id"] for item in record_evidence}
        )
        == 8,
        "independent_app_ids": len(app_ids) == 8,
        "policy_all_2xx": all(
            item["policy_http_all_2xx"] for item in record_evidence
        ),
        "agentos_auto_retry_disabled": all(
            item["agentos_remaining_auto_retry_count"] == 0
            for item in record_evidence
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
        and acceptance["policy_all_2xx"]
        and acceptance["agentos_auto_retry_disabled"]
        and acceptance["trajectory_logprob_joins"] == 8
    )
    evidence = {
        "schema_version": "openreward.miaoda_explicit_batch_canary.v1",
        "status": "passed" if passed else "failed",
        "completed_at": datetime.now(started_at.tzinfo).isoformat(),
        "batch_task_id": batch_task_id,
        "task_name": task_name,
        "upstream_status": snapshot.status,
        "polls": polls,
        "acceptance": acceptance,
        "dashboard_matches": dashboard_matches,
        "records": record_evidence,
    }
    _write(args.output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
