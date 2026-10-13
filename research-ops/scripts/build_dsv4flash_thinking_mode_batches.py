#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_WAREHOUSE_ROOT = Path(
    "/data/trajectory-warehouse"
)
DEFAULT_DATASET_ID = "ds_dsv4flash-v21-xml-ctfalse-usersim149-v1"
WAREHOUSE_CLI = Path(
    "/data/datasets/miaoda_online_flywheel/scripts/warehouse.py"
)
DEFAULT_OUTPUT_ROOT = Path(
    "/srv/research-ops/workspaces/alice/qianfan_coder/datasets/"
    "dsv4flash_v21_thinking_mode_system_v1_20260807"
)
REPLACEMENTS = {
    "20260731_glm52-v20-usersim-xml-parallel-pure-user-drop-v1": (
        "20260807_20260727-second-round-usersimulator-excellent-xml-sft-"
        "keep-reasoning-masked-parallel-pure-user-drop-v1"
    )
}
INSTRUCTION = (
    "If thinking_mode is enabled (triggered by <think>), you MUST output your complete "
    "reasoning inside <think>...</think> BEFORE any tool calls or final response.\n\n"
    "Otherwise, output directly after </think> with tool calls or final response."
)
TOOL_LIST_ANCHORS = (
    "You have access to the following functions:",
    "We have access to the following functions:",
)
TOOL_FORMAT_ANCHORS = (
    "If you need to call one or more tools,",
    "If you choose to call a function ONLY reply",
)
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class TransformError(ValueError):
    pass


@dataclass(frozen=True)
class BatchPlan:
    order: int
    source_batch_id: str
    source_data: Path
    source_metadata: dict[str, Any]
    derived_name: str
    output_dir: Path


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_batch_name(batch_id: str) -> str:
    name = re.sub(r"^\d{8}_", "", batch_id)
    return f"{name}-thinking-mode-system-v1"


def insert_before_anchor(content: str, anchor: str) -> str:
    position = content.find(anchor)
    if position < 0:
        raise TransformError(f"anchor not found: {anchor}")
    before = content[:position].rstrip()
    after = content[position:].lstrip()
    return "\n\n".join(part for part in (before, INSTRUCTION, after) if part)


def inject_instruction(messages: list[Any]) -> tuple[list[Any], str, dict[str, bool]]:
    system_indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "system"
    ]
    system_contents = [
        message.get("content")
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "system"
        and isinstance(message.get("content"), str)
    ]
    if sum(content.count(INSTRUCTION) for content in system_contents) != 0:
        raise TransformError("target thinking-mode instruction is already present")

    markers = {
        "has_parameters_description": any("Parameters:" in value for value in system_contents),
        "has_parameters_json_schema": any(
            "Parameters JSON Schema:" in value for value in system_contents
        ),
        "has_tool_list_anchor": any(
            anchor in value for value in system_contents for anchor in TOOL_LIST_ANCHORS
        ),
    }
    result = deepcopy(messages)

    for index in system_indices:
        content = result[index].get("content")
        if not isinstance(content, str):
            continue
        for anchor in TOOL_LIST_ANCHORS:
            if anchor in content:
                result[index]["content"] = insert_before_anchor(content, anchor)
                return result, "before_tool_schema_list", markers

    for index in system_indices:
        content = result[index].get("content")
        if not isinstance(content, str):
            continue
        for anchor in TOOL_FORMAT_ANCHORS:
            if anchor in content:
                result[index]["content"] = insert_before_anchor(content, anchor)
                return result, "before_tool_call_format", markers

    for index in system_indices:
        content = result[index].get("content")
        if isinstance(content, str):
            result[index]["content"] = "\n\n".join(
                part for part in (content.rstrip(), INSTRUCTION) if part
            )
            return result, "append_existing_system", markers

    result.insert(0, {"role": "system", "content": INSTRUCTION})
    return result, "create_system_message", markers


def validate_transformation(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_other = {key: value for key, value in before.items() if key != "messages"}
    after_other = {key: value for key, value in after.items() if key != "messages"}
    if canonical(before_other) != canonical(after_other):
        raise TransformError("non-message fields changed")

    instruction_count = 0
    for message in after.get("messages") or []:
        if not isinstance(message, dict):
            raise TransformError("output contains a non-object message")
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            instruction_count += message["content"].count(INSTRUCTION)
    if instruction_count != 1:
        raise TransformError(
            f"expected exactly one instruction in system messages, got {instruction_count}"
        )

    before_non_system = [
        message
        for message in before.get("messages") or []
        if not isinstance(message, dict) or message.get("role") != "system"
    ]
    after_non_system = [
        message
        for message in after.get("messages") or []
        if not isinstance(message, dict) or message.get("role") != "system"
    ]
    if canonical(before_non_system) != canonical(after_non_system):
        raise TransformError("non-system messages changed")


def transform_record(record: Any) -> tuple[dict[str, Any], str, dict[str, bool]]:
    if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
        raise TransformError("expected an object with a messages list")
    output = deepcopy(record)
    output["messages"], strategy, markers = inject_instruction(record["messages"])
    validate_transformation(record, output)
    return output, strategy, markers


def read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise TransformError(f"{label} does not exist: {path}")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise TransformError(f"{label} exceeds the 4 MiB metadata limit: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransformError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise TransformError(f"{label} must contain a JSON object")
    return value


def resolve_plan(
    warehouse_root: Path,
    dataset_id: str,
    output_root: Path,
) -> tuple[dict[str, Any], list[BatchPlan]]:
    if not ID_PATTERN.fullmatch(dataset_id):
        raise TransformError("dataset ID contains unsupported characters")
    warehouse_root = warehouse_root.resolve()
    dataset_dir = (warehouse_root / "datasets" / dataset_id).resolve()
    datasets_root = (warehouse_root / "datasets").resolve()
    if not dataset_dir.is_relative_to(datasets_root):
        raise TransformError("dataset path escapes the Warehouse datasets root")
    dataset = read_json(dataset_dir / "dataset.json", "dataset.json")
    batches = dataset.get("batches")
    if not isinstance(batches, list) or not batches:
        raise TransformError("dataset.json does not contain batches")

    replacement_hits = Counter()
    plans = []
    sft_root = (warehouse_root / "sft").resolve()
    for order, item in enumerate(batches):
        if not isinstance(item, dict) or item.get("table_type") != "sft":
            raise TransformError(f"batch #{order + 1} is not an SFT batch")
        original_batch_id = str(item.get("batch_id") or "")
        source_batch_id = REPLACEMENTS.get(original_batch_id, original_batch_id)
        if original_batch_id in REPLACEMENTS:
            replacement_hits[original_batch_id] += 1
        if not ID_PATTERN.fullmatch(source_batch_id):
            raise TransformError(f"invalid batch ID: {source_batch_id}")
        batch_dir = (sft_root / source_batch_id).resolve()
        if not batch_dir.is_relative_to(sft_root) or not batch_dir.is_dir():
            raise TransformError(f"SFT batch does not exist: {source_batch_id}")
        source_data = (batch_dir / "data.jsonl").resolve()
        if not source_data.is_file() or not source_data.is_relative_to(warehouse_root):
            raise TransformError(f"invalid SFT data path: {source_data}")
        metadata = read_json(batch_dir / "batch.json", f"{source_batch_id}/batch.json")
        name = logical_batch_name(source_batch_id)
        plans.append(
            BatchPlan(
                order=order,
                source_batch_id=source_batch_id,
                source_data=source_data,
                source_metadata=metadata,
                derived_name=name,
                output_dir=output_root / name,
            )
        )

    for source, hits in replacement_hits.items():
        if hits != 1:
            raise TransformError(f"replacement source {source} appeared {hits} times")
    missing = set(REPLACEMENTS) - set(replacement_hits)
    if missing:
        raise TransformError(f"replacement source is absent from dataset: {sorted(missing)}")
    if len({plan.derived_name for plan in plans}) != len(plans):
        raise TransformError("derived batch names are not unique")
    return dataset, plans


def iter_records(path: Path, limit: int = 0) -> Iterable[tuple[int, bytes, Any]]:
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if limit and line_number > limit:
                return
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TransformError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            yield line_number, raw, record


def process_batch(plan: BatchPlan, *, mode: str, limit: int) -> dict[str, Any]:
    if mode == "build" and plan.output_dir.exists():
        raise TransformError(f"refusing to overwrite output directory: {plan.output_dir}")

    output_path = plan.output_dir / "data.jsonl"
    partial_path = plan.output_dir / "data.jsonl.partial"
    if mode == "build":
        plan.output_dir.mkdir(parents=True)

    input_digest = hashlib.sha256()
    output_digest = hashlib.sha256()
    strategies: Counter[str] = Counter()
    marker_counts: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    rows = 0
    output_handle = partial_path.open("wb") if mode == "build" else None
    try:
        for line_number, raw, record in iter_records(
            plan.source_data, limit=0 if mode == "build" else limit
        ):
            rows += 1
            input_digest.update(raw)
            transformed, strategy, markers = transform_record(record)
            strategies[strategy] += 1
            for key, present in markers.items():
                marker_counts[key] += int(present)
            encoded = (
                json.dumps(transformed, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            output_digest.update(encoded)
            if output_handle is not None:
                output_handle.write(encoded)
            if len(examples) < 4:
                examples.append(
                    {
                        "line": line_number,
                        "strategy": strategy,
                        "instruction_sha256": hashlib.sha256(
                            INSTRUCTION.encode()
                        ).hexdigest(),
                    }
                )
            if mode == "build" and rows % 1000 == 0:
                print(
                    f"{plan.source_batch_id}: rows={rows} strategies={dict(strategies)}",
                    flush=True,
                )
    except BaseException:
        if output_handle is not None:
            output_handle.close()
        partial_path.unlink(missing_ok=True)
        raise
    finally:
        if output_handle is not None and not output_handle.closed:
            output_handle.close()

    if mode == "build":
        partial_path.replace(output_path)
    report = {
        "order": plan.order,
        "source_batch_id": plan.source_batch_id,
        "source_status": plan.source_metadata.get("status"),
        "source_record_count": plan.source_metadata.get("record_count"),
        "source_data": str(plan.source_data),
        "derived_name": plan.derived_name,
        "output": str(output_path) if mode == "build" else None,
        "rows_processed": rows,
        "full_build": mode == "build",
        "strategies": dict(strategies),
        "marker_counts": dict(marker_counts),
        "input_sha256": input_digest.hexdigest(),
        "output_sha256": output_digest.hexdigest(),
        "examples": examples,
    }
    if mode == "build":
        expected = int(plan.source_metadata.get("record_count") or 0)
        if rows != expected:
            output_path.unlink(missing_ok=True)
            raise TransformError(
                f"{plan.source_batch_id}: output rows {rows} != expected {expected}"
            )
        manifest = {
            "schema_version": 1,
            "status": "local_candidate_before_warehouse_ingest",
            "operation": "inject_thinking_mode_system_instruction",
            "instruction": INSTRUCTION,
            "source_batch_id": plan.source_batch_id,
            "source_batch_status": plan.source_metadata.get("status"),
            "derived_name": plan.derived_name,
            "rows": rows,
            "lineage": {
                "source_batch": plan.source_batch_id,
                "transform": "inject_thinking_mode_system_instruction",
                "instruction_sha256": hashlib.sha256(INSTRUCTION.encode()).hexdigest(),
            },
            "report": report,
        }
        (plan.output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def ingest_built_batch(
    plan: BatchPlan, *, user: str, source_dataset_id: str
) -> dict[str, Any]:
    data_path = plan.output_dir / "data.jsonl"
    manifest_path = plan.output_dir / "manifest.json"
    manifest = read_json(manifest_path, f"{plan.derived_name}/manifest.json")
    if manifest.get("source_batch_id") != plan.source_batch_id:
        raise TransformError(f"{plan.derived_name}: source batch mismatch in manifest")
    if manifest.get("derived_name") != plan.derived_name:
        raise TransformError(f"{plan.derived_name}: derived name mismatch in manifest")
    if int(manifest.get("rows") or 0) != int(plan.source_metadata.get("record_count") or 0):
        raise TransformError(f"{plan.derived_name}: row count mismatch in manifest")
    if not data_path.is_file():
        raise TransformError(f"{plan.derived_name}: built data.jsonl is missing")

    lineage = {
        "source_dataset": source_dataset_id,
        "source_batch": plan.source_batch_id,
        "transform": "inject_thinking_mode_system_instruction",
        "instruction_sha256": hashlib.sha256(INSTRUCTION.encode()).hexdigest(),
        "rows": manifest["rows"],
    }
    command = [
        sys.executable,
        str(WAREHOUSE_CLI),
        "--table",
        "sft",
        "ingest",
        str(data_path),
        "--name",
        plan.derived_name,
        "--tag",
        "dsv4flash",
        "--tag",
        "v21",
        "--tag",
        "think",
        "--tag",
        "xml",
        "--tag",
        "thinking-mode-system",
        "--lineage",
        json.dumps(lineage, ensure_ascii=False, separators=(",", ":")),
    ]
    environment = os.environ.copy()
    environment["WH_USER"] = user
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise TransformError(
            f"{plan.derived_name}: Warehouse ingest failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    match = re.search(r"batch_id:\s+(\S+)", completed.stdout)
    if match is None:
        raise TransformError(
            f"{plan.derived_name}: cannot parse batch ID from Warehouse ingest output"
        )
    return {
        "source_batch_id": plan.source_batch_id,
        "derived_name": plan.derived_name,
        "warehouse_batch_id": match.group(1),
        "status": "pending",
        "rows": manifest["rows"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replace the selected usersim batch, then inject the exact thinking-mode "
            "instruction into every batch in a Warehouse dataset."
        )
    )
    parser.add_argument("--warehouse-root", type=Path, default=DEFAULT_WAREHOUSE_ROOT)
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--mode", choices=("inspect", "build", "ingest"), default="inspect")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Rows per batch in inspect mode; 0 scans all. Build mode always scans all rows.",
    )
    parser.add_argument("--user", default="alice", help="Warehouse write attribution")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    dataset, plans = resolve_plan(args.warehouse_root, args.dataset_id, args.output_root)
    if args.mode == "ingest":
        report_path = args.output_root / "ingest_report.json"
        if report_path.exists():
            raise SystemExit(f"refusing to overwrite ingest report: {report_path}")
        reports = [
            ingest_built_batch(
                plan,
                user=args.user,
                source_dataset_id=args.dataset_id,
            )
            for plan in plans
        ]
        result = {
            "schema_version": 1,
            "mode": "ingest",
            "source_dataset_id": args.dataset_id,
            "batch_count": len(reports),
            "reports": reports,
            "note": "All batches remain pending; this script never confirms them.",
        }
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.mode == "build" and args.output_root.exists():
        raise SystemExit(f"refusing to overwrite output root: {args.output_root}")
    if args.mode == "build":
        args.output_root.mkdir(parents=True)

    reports = [
        process_batch(plan, mode=args.mode, limit=args.limit)
        for plan in plans
    ]
    result = {
        "schema_version": 1,
        "mode": args.mode,
        "source_dataset_id": args.dataset_id,
        "source_dataset_total_records": dataset.get("total_records"),
        "replacement": REPLACEMENTS,
        "instruction": INSTRUCTION,
        "batch_count": len(plans),
        "planned_dataset_batches": [report["derived_name"] for report in reports],
        "reports": reports,
    }
    if args.mode == "build":
        (args.output_root / "build_report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
