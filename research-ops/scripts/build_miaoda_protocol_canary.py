#!/usr/bin/env python3
"""Build an eight-record protocol canary from one proven AgentOS record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_ops.agentos import AgentOSClient
from research_ops.openreward.correlation import strip_episode_correlation


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _record_query(record: Mapping[str, Any]) -> str:
    delegate = _mapping(record.get("delegate_arg"))
    state_input = _mapping(delegate.get("state_input"))
    query = str(delegate.get("query") or state_input.get("query") or "").strip()
    cleaned, _correlation_id = strip_episode_correlation(query)
    cleaned = str(cleaned).strip()
    if not cleaned:
        raise RuntimeError("source record has no query")
    if "<SKILL>" in cleaned or "</SKILL>" in cleaned:
        raise RuntimeError("source record contains historical skill injection")
    return cleaned


def build_canary_records(
    record: Mapping[str, Any],
    *,
    batch_task_id: str,
    count: int = 8,
) -> list[dict[str, Any]]:
    if str(record.get("status") or "").strip().casefold() != "completed":
        raise RuntimeError("source AgentOS record did not complete")
    query = _record_query(record)
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    case_id = str(record.get("case_id") or "").strip()
    record_id = str(record.get("id") or "").strip()
    if not case_id or not record_id:
        raise RuntimeError("source AgentOS record has no stable IDs")
    return [
        {
            "id": f"miaoda-protocol-{digest[:16]}-{index:02d}",
            "query": query,
            "metadata": {
                "split": "canary",
                "source_batch_id": batch_task_id,
                "source_record_id": record_id,
                "source_case_id": case_id,
                "query_sha256": digest,
                "protocol_repeat_index": index,
            },
        }
        for index in range(count)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-task-id", required=True)
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--datawheel-token-env", default="DATAWHEEL_TOKEN")
    args = parser.parse_args()
    if args.count != 8:
        raise SystemExit("protocol acceptance requires exactly eight records")
    base_url = str(os.environ.get("ROPS_AGENTOS_DATAWHEEL_BASE_URL", "")).strip()
    if not base_url:
        raise SystemExit("runtime reference is empty: ROPS_AGENTOS_DATAWHEEL_BASE_URL")
    if not os.environ.get(args.datawheel_token_env, "").strip():
        raise SystemExit(
            f"runtime credential reference is empty: {args.datawheel_token_env}"
        )

    client = AgentOSClient(
        base_url,
        token_env=args.datawheel_token_env,
        timeout_seconds=180,
        trust_env=False,
    )
    snapshot = client.get_batch(args.batch_task_id)
    form_data = _mapping(snapshot.raw.get("form_data"))
    initial_retry_count = form_data.get("remainingAutoRetryCount")
    records = client.list_batch_records(args.batch_task_id)
    matches = [record for record in records if record.get("case_id") == args.case_id]
    if len(matches) != 1:
        raise RuntimeError("source batch does not contain exactly one requested case")
    source = matches[0]
    if (
        not isinstance(initial_retry_count, int)
        or isinstance(initial_retry_count, bool)
        or source.get("remaining_auto_retry_count") != initial_retry_count
    ):
        raise RuntimeError("source AgentOS record consumed an automatic retry")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    values = build_canary_records(
        source,
        batch_task_id=args.batch_task_id,
        count=args.count,
    )
    temporary = output.with_suffix(output.suffix + ".part")
    temporary.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )
    os.replace(temporary, output)
    query = str(values[0]["query"])
    print(
        json.dumps(
            {
                "ready": True,
                "records": len(values),
                "query_chars": len(query),
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "output": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
