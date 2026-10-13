#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PARALLEL_PHRASE = "Switch to parallel mode immediately"
SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder(?:\s[^>]*)?>.*?</system-reminder>",
    flags=re.IGNORECASE | re.DOTALL,
)


def strip_parallel_blocks(value: str) -> tuple[str, int]:
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        block = match.group(0)
        if PARALLEL_PHRASE not in block:
            return block
        removed += 1
        return ""

    cleaned = SYSTEM_REMINDER_RE.sub(replace, value)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, removed


def identity(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    result = {}
    for key in ("app_id", "conv_id", "source_app_id", "source_line"):
        value = record.get(key, metadata.get(key))
        if value is not None:
            result[key] = value
    return result


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transform(input_path: Path, output_path: Path, report_path: Path) -> dict[str, Any]:
    for path in (output_path, report_path):
        if path.exists():
            raise SystemExit(f"refusing to overwrite existing output: {path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(output_path.name + ".partial")
    if partial.exists():
        raise SystemExit(f"refusing to reuse partial output: {partial}")

    stats: Counter[str] = Counter()
    deleted_mask_values: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    input_digest = hashlib.sha256()
    output_digest = hashlib.sha256()

    try:
        with input_path.open("rb") as source, partial.open("wb") as target:
            for line_number, raw in enumerate(source, start=1):
                if not raw.strip():
                    stats["blank_lines"] += 1
                    continue
                input_digest.update(raw)
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"{input_path}:{line_number}: invalid JSON: {exc}") from exc
                if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
                    raise ValueError(
                        f"{input_path}:{line_number}: expected object with messages list"
                    )

                stats["rows"] += 1
                original_non_messages = {
                    key: value for key, value in record.items() if key != "messages"
                }
                changed = False
                output_messages = []
                for message_index, message in enumerate(record["messages"]):
                    if not isinstance(message, dict):
                        raise ValueError(
                            f"{input_path}:{line_number}: message {message_index} is not an object"
                        )
                    content = message.get("content")
                    has_phrase = isinstance(content, str) and PARALLEL_PHRASE in content
                    if not has_phrase:
                        output_messages.append(message)
                        continue
                    if message.get("role") != "user":
                        stats["parallel_phrase_outside_user"] += 1
                        output_messages.append(message)
                        continue

                    cleaned, removed = strip_parallel_blocks(content)
                    if removed == 0:
                        raise ValueError(
                            f"{input_path}:{line_number}: message {message_index} contains the "
                            "parallel phrase outside a complete system-reminder block"
                        )
                    changed = True
                    stats["user_messages_with_parallel"] += 1
                    stats["parallel_blocks_removed"] += removed
                    action = "strip_block"
                    if cleaned:
                        updated = dict(message)
                        updated["content"] = cleaned
                        output_messages.append(updated)
                        stats["co_located_user_messages_retained"] += 1
                    else:
                        action = "drop_user_message"
                        stats["pure_parallel_user_messages_deleted"] += 1
                        deleted_mask_values[repr(message.get("step_loss_mask", "<missing>"))] += 1
                    if len(examples) < 12:
                        examples.append(
                            {
                                "line": line_number,
                                "message_index": message_index,
                                "identity": identity(record),
                                "action": action,
                                "blocks_removed": removed,
                                "before_chars": len(content),
                                "after_chars": len(cleaned),
                                "after_preview": cleaned[:240],
                            }
                        )

                if changed:
                    stats["records_changed"] += 1
                record["messages"] = output_messages
                if original_non_messages != {
                    key: value for key, value in record.items() if key != "messages"
                }:
                    raise AssertionError(f"{input_path}:{line_number}: non-message fields changed")
                for message_index, message in enumerate(output_messages):
                    content = message.get("content") if isinstance(message, dict) else None
                    if (
                        message.get("role") == "user"
                        and isinstance(content, str)
                        and PARALLEL_PHRASE in content
                    ):
                        raise AssertionError(
                            f"{input_path}:{line_number}: message {message_index} still contains "
                            "the parallel reminder"
                        )
                encoded = (
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                target.write(encoded)
                output_digest.update(encoded)
                stats["output_rows"] += 1
                if line_number % 100 == 0:
                    print(
                        f"processed={line_number} changed={stats['records_changed']} "
                        f"removed={stats['parallel_blocks_removed']}",
                        flush=True,
                    )
        partial.replace(output_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    report = {
        "schema_version": 1,
        "operation": "remove_parallel_tool_call_system_reminders",
        "policy": "pure_user_drop",
        "parallel_phrase": PARALLEL_PHRASE,
        "input": str(input_path),
        "output": str(output_path),
        "stats": dict(stats),
        "deleted_message_step_loss_mask_values": dict(deleted_mask_values),
        "input_bytes": input_path.stat().st_size,
        "output_bytes": output_path.stat().st_size,
        "input_sha256": input_digest.hexdigest(),
        "output_sha256": output_digest.hexdigest(),
        "examples": examples,
        "assertions": {
            "row_count_preserved": stats["rows"] == stats["output_rows"] and stats["rows"] > 0,
            "invalid_json_rows": 0,
            "non_message_fields_changed": 0,
            "remaining_parallel_blocks_in_user": 0,
        },
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove the exact multi-tool-call system reminder from SFT user messages."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = transform(args.input, args.output, args.report)
    print(json.dumps({"output": report["output"], "stats": report["stats"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
