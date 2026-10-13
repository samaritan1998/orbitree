#!/usr/bin/env python3
"""Prepare or submit an 8-row Miaoda raw-query Datawheel canary.

Without ``--submit`` this command only writes a redacted request preview and
does not contact AgentOS.  With ``--submit`` the bearer is resolved by
``AgentOSClient`` from ``DATAWHEEL_TOKEN`` (or ``--token-env``); it is never a
CLI argument, output field, or artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from research_ops.agentos import (  # noqa: E402
    AgentOSClient,
    AgentOSClientError,
    MiaodaBatchPayloadError,
    build_miaoda_e2e_batch_payload,
    miaoda_batch_identity,
)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not an object")
            rows.append(value)
    return rows


def _redacted_preview(payload: dict[str, Any]) -> dict[str, Any]:
    preview = json.loads(json.dumps(payload, ensure_ascii=False))
    model_config = (
        preview.get("metadata", {})
        .get("codingConfig", {})
        .get("codingPromptiModelConfig", {})
    )
    if isinstance(model_config, dict) and "api_key" in model_config:
        model_config["api_key"] = "<runtime-only>"
    return preview


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=os.environ.get("DATAWHEEL_API_BASE", ""))
    parser.add_argument("--token-env", default="DATAWHEEL_TOKEN")
    parser.add_argument(
        "--generated-user-id",
        default=os.environ.get("DATAWHEEL_GENERATED_USER_ID", ""),
    )
    parser.add_argument(
        "--task-name",
        default="HARN-0001-online-mopd-miaoda-canary8",
    )
    parser.add_argument(
        "--task-name-postfix",
        default=os.environ.get("DATAWHEEL_TASK_POSTFIX", "-maasdefault-Miaoda123"),
    )
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-api-url", required=True)
    parser.add_argument("--provider", default="qianfancoder_adapter_v4")
    parser.add_argument("--coding-prompti-variant", default="qianfan-coder")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--expected-count", type=int, default=8)
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()

    try:
        rows = _read_rows(args.input_jsonl)
        if len(rows) != args.expected_count:
            raise MiaodaBatchPayloadError(
                f"expected {args.expected_count} rows, found {len(rows)}"
            )
        generated_user_id = str(args.generated_user_id).strip()
        if not generated_user_id:
            raise MiaodaBatchPayloadError(
                "generated user id is required; set DATAWHEEL_GENERATED_USER_ID"
            )
        model_api_key = os.environ.get("AGENTOS_MODEL_API_KEY", "").strip() or str(uuid4())
        payload = build_miaoda_e2e_batch_payload(
            rows,
            task_name=args.task_name,
            generated_user_id=generated_user_id,
            model_name=args.model_name,
            model_api_url=args.model_api_url,
            model_api_key=model_api_key,
            task_name_postfix=args.task_name_postfix,
            provider=args.provider,
            coding_prompti_variant=args.coding_prompti_variant,
            concurrency=args.concurrency,
        )
    except (OSError, ValueError, json.JSONDecodeError, MiaodaBatchPayloadError) as exc:
        print(f"cannot prepare Miaoda batch: {exc}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    identity = miaoda_batch_identity(payload)
    idempotency_key = f"{args.task_name}:{identity[:24]}"
    _write_json(
        args.output_dir / "datawheel_request.redacted.json",
        _redacted_preview(payload),
    )
    manifest: dict[str, Any] = {
        "task_name": args.task_name,
        "idempotency_key": idempotency_key,
        "payload_identity": identity,
        "record_count": len(rows),
        "pipeline": payload["selectedPipeline"],
        "query_mode": payload["queryMode"],
        "environment": payload["env"]["value"],
        "prepared_at": datetime.now(UTC).isoformat(),
        "submitted": False,
    }

    if args.submit:
        if not args.base_url.strip():
            print(
                "cannot submit: set DATAWHEEL_API_BASE or pass --base-url",
                file=sys.stderr,
            )
            return 2
        try:
            client = AgentOSClient(args.base_url, token_env=args.token_env)
            remote = client.submit_batch(payload, idempotency_key=idempotency_key)
        except AgentOSClientError as exc:
            print(
                "AgentOS submission failed "
                f"({type(exc).__name__}); no credential details were logged",
                file=sys.stderr,
            )
            return 1
        manifest.update(
            {
                "submitted": True,
                "batch_task_id": remote.batch_task_id,
                "remote_status": remote.status,
            }
        )

    _write_json(args.output_dir / "submission_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
