#!/usr/bin/env python3
"""Run a guarded Miaoda Datawheel -> Goblin Reward batch.

Without ``--submit`` this command only builds and prints redacted routing
metadata.  With ``--submit`` it submits one Datawheel batch and one Goblin job,
then writes a correlation manifest.  Tokens are read from environment variable
names and are never printed or persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from research_ops.agentos import AgentOSClient, AgentOSClientError
from research_ops.agentos.miaoda import build_miaoda_e2e_batch_payload
from research_ops.reward import (
    GoblinJobFailed,
    GoblinRewardClient,
    GoblinRewardError,
    reward_map_from_file,
)


def _rows(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} is not an object")
            records.append(value)
    if not records:
        raise ValueError("input JSONL has no records")
    return records


def _safe_manifest(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_manifest(v) for k, v in value.items() if str(k).casefold() not in {
            "api_key",
            "apikey",
            "authorization",
            "token",
            "secret",
        }}
    if isinstance(value, list):
        return [_safe_manifest(item) for item in value]
    return value


def _poll_datawheel(
    client: AgentOSClient,
    batch_id: str,
    *,
    interval: float,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + max(timeout, 0.1)
    while True:
        snapshot = client.get_batch(batch_id)
        status = snapshot.status.casefold()
        if status in {"completed", "failed", "cancelled", "error"}:
            return snapshot
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Datawheel batch polling timed out: {batch_id}")
        time.sleep(min(max(interval, 0.05), max(deadline - time.monotonic(), 0.0)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-api-url", required=True)
    parser.add_argument("--generated-user-id", required=True)
    parser.add_argument("--datawheel-url", required=True)
    parser.add_argument("--goblin-url", required=True)
    parser.add_argument("--benchmark", default="miaoda-online-mopd-train-v1")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--datawheel-token-env", default="DATAWHEEL_TOKEN")
    parser.add_argument("--goblin-token-env", default="GOBLIN_REWARD_API_TOKEN")
    parser.add_argument("--model-api-key-env", default="AGENTOS_MODEL_API_KEY")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--poll-timeout", type=float, default=3_600.0)
    parser.add_argument("--reward-prompt-version", default="v1_6")
    parser.add_argument("--reward-model", default="gpt-5.5-agent")
    parser.add_argument("--no-eval-mm", action="store_true")
    parser.add_argument("--no-eval-miniprog", action="store_true")
    parser.add_argument("--submit", action="store_true", help="perform external submissions")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    records = _rows(args.input_jsonl)
    model_key = os.environ.get(args.model_api_key_env, "").strip()
    if args.submit and not model_key:
        raise SystemExit(
            f"missing model API key; configure environment variable {args.model_api_key_env}"
        )
    payload = build_miaoda_e2e_batch_payload(
        records,
        task_name=f"ors-miaoda-{args.model_name}",
        generated_user_id=args.generated_user_id,
        model_name=args.model_name,
        model_api_url=args.model_api_url,
        model_api_key=model_key or "<runtime-only>",
        concurrency=args.concurrency,
    )
    goblin = GoblinRewardClient(args.goblin_url, token_env=args.goblin_token_env)
    goblin_payload = goblin.build_job_payload(
        rollout_model=args.model_name,
        benchmark_dataset_name=args.benchmark,
        concurrency=args.concurrency,
        prompt_version=args.reward_prompt_version,
        reward_model=args.reward_model,
        eval_mm=not args.no_eval_mm,
        eval_miniprog=not args.no_eval_miniprog,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.submit:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "records": len(records),
                    "datawheel": {
                        "url": args.datawheel_url,
                        "payload": _safe_manifest(payload),
                    },
                    "goblin": {"url": args.goblin_url, "payload": goblin_payload},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    datawheel = AgentOSClient(args.datawheel_url, token_env=args.datawheel_token_env)
    try:
        submitted = datawheel.submit_batch(
            payload,
            idempotency_key=f"ors-miaoda-batch-{args.model_name}",
        )
        batch = _poll_datawheel(
            datawheel,
            submitted.batch_task_id,
            interval=args.poll_interval,
            timeout=args.poll_timeout,
        )
        manifest: dict[str, Any] = {
            "batch_task_id": submitted.batch_task_id,
            "datawheel_status": batch.status,
            "rollout_model": args.model_name,
            "records": len(records),
        }
        if batch.status.casefold() != "completed":
            manifest["usable"] = False
            manifest["failure_reason"] = "agentos_execution_failure"
            (args.output_dir / "miaoda-online-manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 2
        artifact = args.output_dir / f"datawheel-{submitted.batch_task_id}.xlsx"
        datawheel.download_result(submitted.batch_task_id, artifact)
        job = goblin.submit_file(
            artifact,
            rollout_model=args.model_name,
            benchmark_dataset_name=args.benchmark,
            concurrency=args.concurrency,
            prompt_version=args.reward_prompt_version,
            reward_model=args.reward_model,
            eval_mm=not args.no_eval_mm,
            eval_miniprog=not args.no_eval_miniprog,
            upload=True,
        )
        final_job = goblin.wait(
            job.job_id,
            poll_interval_seconds=args.poll_interval,
            timeout_seconds=args.poll_timeout,
        )
        rewards = reward_map_from_file(final_job.result_file or "")
        manifest.update(
            {
                "artifact": str(artifact),
                "goblin_job_id": final_job.job_id,
                "goblin_status": final_job.status,
                "goblin_result_file": final_job.result_file,
                "reward_join_rows": len(rewards),
                "usable": True,
            }
        )
    except (AgentOSClientError, GoblinRewardError, OSError, TimeoutError) as exc:
        manifest = {
            "rollout_model": args.model_name,
            "records": len(records),
            "usable": False,
            "failure_type": type(exc).__name__,
        }
        if isinstance(exc, GoblinJobFailed):
            manifest["goblin_job_id"] = exc.snapshot.job_id
            manifest["goblin_status"] = exc.snapshot.status
        (args.output_dir / "miaoda-online-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 3
    (args.output_dir / "miaoda-online-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

