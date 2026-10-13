#!/usr/bin/env python3
"""Build the PPT trajectory canary in process-isolated shards and merge it."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts.build_ppt_trajectory_canary import (
        DEFAULT_BATCH_ID,
        DEFAULT_OUTPUT_NAME,
        DEFAULT_RUN_ROOT,
        PRODUCER_VERSION,
        read_json,
        read_jsonl,
        utc_now,
        write_json,
        write_parquet,
    )
except ModuleNotFoundError:
    from build_ppt_trajectory_canary import (
        DEFAULT_BATCH_ID,
        DEFAULT_OUTPUT_NAME,
        DEFAULT_RUN_ROOT,
        PRODUCER_VERSION,
        read_json,
        read_jsonl,
        utc_now,
        write_json,
        write_parquet,
    )


def partitions(total: int, count: int) -> list[tuple[int, int]]:
    count = min(max(count, 1), max(total, 1))
    base, remainder = divmod(total, count)
    result = []
    offset = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        result.append((offset, size))
        offset += size
    return result


def tail(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return "<missing log>"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def run_shards(
    *,
    run_root: Path,
    output_dir: Path,
    batch_id: str,
    built_at: str,
    task_count: int,
    process_count: int,
) -> list[Path]:
    builder = Path(__file__).with_name("build_ppt_trajectory_canary.py").resolve()
    shard_root = output_dir / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[subprocess.Popen, Path, Any]] = []
    shard_dirs: list[Path] = []
    for index, (offset, size) in enumerate(partitions(task_count, process_count)):
        shard_dir = shard_root / f"part_{index:02d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        shard_dirs.append(shard_dir)
        log_path = shard_dir / "build.log"
        log_handle = log_path.open("w", encoding="utf-8")
        command = [
            sys.executable,
            str(builder),
            "--run-root",
            str(run_root),
            "--output-dir",
            str(shard_dir),
            "--logical-output-dir",
            str(output_dir),
            "--batch-id",
            batch_id,
            "--built-at",
            built_at,
            "--task-offset",
            str(offset),
            "--limit-tasks",
            str(size),
            "--workers",
            "1",
            "--skip-parquet",
        ]
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append((process, log_path, log_handle))
        print(f"started shard {index + 1}/{len(shard_dirs)}: offset={offset} tasks={size}")
    last_report = 0.0
    while True:
        running = sum(process.poll() is None for process, _, _ in processes)
        if running == 0:
            break
        now = time.monotonic()
        if now - last_report >= 10:
            completed = len(processes) - running
            progress = []
            for _, log_path, _ in processes:
                if not log_path.is_file():
                    progress.append(0)
                    continue
                progress.append(
                    sum(
                        1
                        for line in log_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()
                        if line.startswith("[")
                    )
                )
            print(
                f"shards completed={completed}/{len(processes)} running={running}; "
                f"rows reported={sum(progress)}",
                flush=True,
            )
            last_report = now
        time.sleep(2)
    failures = []
    for index, (process, log_path, log_handle) in enumerate(processes):
        log_handle.close()
        if process.returncode != 0:
            failures.append((index, process.returncode, log_path))
    if failures:
        detail = "\n\n".join(
            f"shard={index} returncode={returncode}\n{tail(log_path)}"
            for index, returncode, log_path in failures
        )
        raise RuntimeError(f"shard build failed:\n{detail}")
    return shard_dirs


def merge_images(shard_dirs: list[Path], output_dir: Path) -> int:
    destination_dir = output_dir / "assets" / "images"
    destination_dir.mkdir(parents=True, exist_ok=True)
    for shard_dir in shard_dirs:
        source_dir = shard_dir / "assets" / "images"
        if not source_dir.is_dir():
            continue
        for source in source_dir.iterdir():
            if not source.is_file() or ".tmp" in source.name:
                continue
            destination = destination_dir / source.name
            if destination.exists():
                if destination.stat().st_size != source.stat().st_size:
                    raise ValueError(f"image digest collision with different size: {source.name}")
                continue
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
    return sum(
        1 for path in destination_dir.iterdir() if path.is_file() and ".tmp" not in path.name
    )


def merge_rows(shard_dirs: list[Path], output_dir: Path) -> tuple[list[dict[str, Any]], str]:
    data_path = output_dir / "data.jsonl"
    preview_path = output_dir / "rows_preview.jsonl"
    index_path = output_dir / "row_index.jsonl"
    data_temp = data_path.with_suffix(".jsonl.merge.tmp")
    preview_temp = preview_path.with_suffix(".jsonl.merge.tmp")
    index_temp = index_path.with_suffix(".jsonl.merge.tmp")
    summaries: list[dict[str, Any]] = []
    offset = 0
    digest = hashlib.sha256()
    with (
        data_temp.open("wb") as data_out,
        preview_temp.open("w", encoding="utf-8") as preview_out,
        index_temp.open("w", encoding="utf-8") as index_out,
    ):
        for shard_dir in shard_dirs:
            with (
                (shard_dir / "data.jsonl").open("rb") as data_in,
                (shard_dir / "rows_preview.jsonl").open("r", encoding="utf-8") as preview_in,
            ):
                for preview_line in preview_in:
                    raw_line = data_in.readline()
                    if not raw_line:
                        raise ValueError(f"data rows shorter than preview: {shard_dir}")
                    preview = json.loads(preview_line)
                    data_out.write(raw_line)
                    preview_out.write(preview_line)
                    digest.update(raw_line)
                    index_out.write(
                        json.dumps(
                            {
                                "row_number": len(summaries) + 1,
                                "trajectory_id": preview["trajectory_id"],
                                "task_id": preview["task_id"],
                                "attempt_index": preview["attempt_index"],
                                "offset": offset,
                                "length": len(raw_line),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    offset += len(raw_line)
                    summaries.append(preview)
                if data_in.readline():
                    raise ValueError(f"data rows longer than preview: {shard_dir}")
    os.replace(data_temp, data_path)
    os.replace(preview_temp, preview_path)
    os.replace(index_temp, index_path)
    return summaries, digest.hexdigest()


def build_validation(
    *,
    output_dir: Path,
    summaries: list[dict[str, Any]],
    unique_image_count: int,
    image_decode_errors: int,
    built_at: str,
    data_sha256: str,
) -> dict[str, Any]:
    keys = [(row["run_id"], row["task_id"], row["attempt_index"]) for row in summaries]
    llm_request_count = sum(int(row["llm_request_count"] or 0) for row in summaries)
    signature_count = sum(int(row["signature_count"] or 0) for row in summaries)
    decoded_count = sum(int(row["decoded_thinking_count"] or 0) for row in summaries)
    failed_count = sum(int(row["decode_failed_count"] or 0) for row in summaries)
    not_attempted_count = sum(int(row["decode_not_attempted_count"] or 0) for row in summaries)
    secret_fail_count = sum(row["secret_scan_status"] == "fail" for row in summaries)
    integrity_fail_count = sum(row["integrity_status"] == "fail" for row in summaries)
    actual = {
        "row_count": len(summaries),
        "task_count": len({row["task_id"] for row in summaries}),
        "retry_row_count": sum(row["attempt_index"] == 2 for row in summaries),
        "unique_key_count": len(set(keys)),
        "status_counts": dict(collections.Counter(row["status"] for row in summaries)),
        "artifact_complete_count": sum(row["artifact_status"] == "complete" for row in summaries),
        "trajectory_inline_count": sum(
            int(row["trajectory_size_bytes"] or 0) > 2 for row in summaries
        ),
        "llm_trace_inline_count": sum(
            int(row["llm_trace_size_bytes"] or 0) > 2 for row in summaries
        ),
        "llm_request_count": llm_request_count,
        "signature_occurrence_count": signature_count,
        "eligible_signature_count": decoded_count + failed_count,
        "decoded_signature_count": decoded_count,
        "decode_failed_count": failed_count,
        "decode_not_attempted_count": not_attempted_count,
        "decode_status_counts": dict(
            collections.Counter(row["thinking_decode_status"] for row in summaries)
        ),
        "multimodal_row_count": sum(bool(row["is_multimodal"]) for row in summaries),
        "trajectory_image_occurrence_count": sum(int(row["image_count"] or 0) for row in summaries),
        "unique_externalized_image_count": unique_image_count,
        "image_base64_decode_error_count": image_decode_errors,
        "reward_status_counts": dict(
            collections.Counter(row["reward_status"] for row in summaries)
        ),
        "reward_complete_count": sum(row["reward_status"] == "complete" for row in summaries),
        "reward_detail_inline_count": sum(
            row["reward_detail_status"] == "complete" for row in summaries
        ),
        "reward_detail_item_count": sum(
            int(row["reward_detail_item_count"] or 0) for row in summaries
        ),
        "reward_detail_source_hash_count": sum(
            bool(row["reward_detail_source_sha256"]) for row in summaries
        ),
        "reward_detail_hash_count": sum(bool(row["reward_detail_sha256"]) for row in summaries),
        "reward_detail_integrity_pass_count": sum(
            row["reward_detail_integrity_status"] == "pass" for row in summaries
        ),
        "secret_scan_fail_count": secret_fail_count,
        "integrity_fail_count": integrity_fail_count,
        "data_jsonl_size_bytes": (output_dir / "data.jsonl").stat().st_size,
        "data_jsonl_sha256": data_sha256,
    }
    checks = {
        "unique_business_keys": len(set(keys)) == len(summaries),
        "all_rows_inline": actual["trajectory_inline_count"] == len(summaries)
        and actual["llm_trace_inline_count"] == len(summaries),
        "all_secret_scans_pass": secret_fail_count == 0,
        "all_integrity_checks_pass": integrity_fail_count == 0,
        "expected_attempt_count": len(summaries) == 508,
        "expected_task_count": actual["task_count"] == 500,
        "expected_llm_response_count": llm_request_count == 21146,
        "expected_signature_count": signature_count == 2778,
        "expected_eligible_signature_count": decoded_count + failed_count == 2078,
        "expected_decoded_count": decoded_count == 2036,
        "expected_failed_decode_count": failed_count == 42,
        "expected_not_attempted_count": not_attempted_count == 700,
        "all_images_externalized": image_decode_errors == 0,
        "all_completed_rewards_have_inline_detail": sum(
            row["reward_detail_status"] == "complete" for row in summaries
        )
        == sum(row["reward_status"] == "complete" for row in summaries),
        "all_reward_details_have_source_hash": sum(
            bool(row["reward_detail_source_sha256"]) for row in summaries
        )
        == sum(row["reward_status"] == "complete" for row in summaries),
        "all_reward_details_have_content_hash": sum(
            bool(row["reward_detail_sha256"]) for row in summaries
        )
        == sum(row["reward_status"] == "complete" for row in summaries),
        "all_reward_detail_scores_match": sum(
            row["reward_detail_integrity_status"] == "pass" for row in summaries
        )
        == sum(row["reward_status"] == "complete" for row in summaries),
        "expected_reward_complete_count": sum(
            row["reward_status"] == "complete" for row in summaries
        )
        == 496,
        "expected_reward_detail_item_count": sum(
            int(row["reward_detail_item_count"] or 0) for row in summaries
        )
        == 12213,
    }
    return {
        "schema": "ppt_trajectory_canary_validation.v1",
        "producer_version": PRODUCER_VERSION,
        "created_at": built_at,
        "expected": {
            "task_count": 500,
            "attempt_count": 508,
            "llm_response_count": 21146,
            "signature_occurrence_count": 2778,
            "eligible_signature_count": 2078,
            "decoded_signature_count": 2036,
            "failed_decode_count": 42,
            "not_attempted_signature_count": 700,
            "reward_complete_count": 496,
            "reward_detail_item_count": 12213,
        },
        "actual": actual,
        "checks": checks,
        "files": {
            "data_jsonl": str(output_dir / "data.jsonl"),
            "rows_preview": str(output_dir / "rows_preview.jsonl"),
            "row_index": str(output_dir / "row_index.jsonl"),
            "image_assets": str(output_dir / "assets" / "images"),
        },
    }


def cleanup_shard_payloads(shard_dirs: list[Path]) -> None:
    for shard_dir in shard_dirs:
        for name in ("data.jsonl", "row_index.jsonl"):
            path = shard_dir / name
            if path.exists():
                path.unlink()
        image_dir = shard_dir / "assets" / "images"
        if image_dir.is_dir():
            shutil.rmtree(shard_dir / "assets")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--processes", type=int, default=8)
    parser.add_argument("--skip-parquet", action="store_true")
    parser.add_argument("--keep-shard-data", action="store_true")
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else run_root / "postprocess" / DEFAULT_OUTPUT_NAME
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    built_at = utc_now()
    task_count = sum(1 for _ in read_jsonl(run_root / "selection" / "selected_500.manifest.jsonl"))
    shard_dirs = run_shards(
        run_root=run_root,
        output_dir=output_dir,
        batch_id=args.batch_id,
        built_at=built_at,
        task_count=task_count,
        process_count=args.processes,
    )
    print("all shards complete; merging rows and image assets", flush=True)
    summaries, data_sha256 = merge_rows(shard_dirs, output_dir)
    unique_image_count = merge_images(shard_dirs, output_dir)
    image_decode_errors = sum(
        int(
            read_json(shard_dir / "validation_report.json")
            .get("actual", {})
            .get("image_base64_decode_error_count", 0)
        )
        for shard_dir in shard_dirs
    )
    validation = build_validation(
        output_dir=output_dir,
        summaries=summaries,
        unique_image_count=unique_image_count,
        image_decode_errors=image_decode_errors,
        built_at=built_at,
        data_sha256=data_sha256,
    )
    registry = read_json(shard_dirs[0] / "schema_registry.json")
    registry["record_hash_algorithm"] = "sorted_length_prefixed_field_values_sha256.v1"
    write_json(output_dir / "schema_registry.json", registry)
    write_json(output_dir / "validation_report.json", validation)
    write_json(
        output_dir / "batch.json",
        {
            "batch_id": args.batch_id,
            "table_key": "ppt_trajectory",
            "status": "mock_canary_not_registered",
            "created_at": built_at,
            "producer_version": PRODUCER_VERSION,
            "source_run": str(run_root),
            "row_count": len(summaries),
            "task_count": task_count,
            "shard_count": len(shard_dirs),
            "record_hash_algorithm": "sorted_length_prefixed_field_values_sha256.v1",
            "notes": [
                "trajectory and llm_trace are both permanently inline",
                (
                    "complete Reward evaluations and all item_results are permanently "
                    "inline in reward_detail"
                ),
                "reward_record_id is retained only as a stable trace key",
                "raw judge API logs and prompts remain in the controlled source sidecar",
                "failed decoder raw responses remain in the controlled source sidecar",
                "this canary is not registered in the formal Warehouse",
            ],
            "files": validation["files"],
        },
    )
    if not all(validation["checks"].values()):
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        raise RuntimeError("merged canary validation failed")
    if not args.skip_parquet:
        parquet_path = output_dir / "data.parquet"
        print("writing Parquet", flush=True)
        write_parquet(output_dir / "data.jsonl", parquet_path, output_dir)
        validation["files"]["data_parquet"] = str(parquet_path)
        validation["actual"]["data_parquet_size_bytes"] = parquet_path.stat().st_size
        write_json(output_dir / "validation_report.json", validation)
    if not args.keep_shard_data:
        cleanup_shard_payloads(shard_dirs)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
