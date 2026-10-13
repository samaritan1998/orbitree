#!/usr/bin/env python3
"""Extract raw Miaoda app queries from confirmed warehouse trajectories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

QUERY_MARKER = "# User Requirement"
SEPARATOR = re.compile(r"\n-{8,}")


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", text)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item) for item in value)))
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        content = value.get("content")
        if content is not None:
            return _content_text(content)
    return ""


def _request_messages(record: dict[str, Any]) -> list[list[dict[str, Any]]]:
    found: list[list[dict[str, Any]]] = []
    trajectory = record.get("trajectory")
    if not isinstance(trajectory, list):
        return found
    for event in trajectory:
        if not isinstance(event, dict):
            continue
        request = event.get("llm_request_body")
        if not isinstance(request, dict):
            continue
        params = request.get("call_llm_params")
        if not isinstance(params, dict):
            params = request
        messages = params.get("messages")
        if isinstance(messages, list) and all(isinstance(item, dict) for item in messages):
            found.append(messages)
    return found


def _trajectory_ext(record: dict[str, Any]) -> dict[str, Any]:
    trajectory = record.get("trajectory")
    if not isinstance(trajectory, list):
        return {}
    for event in trajectory:
        if isinstance(event, dict) and isinstance(event.get("ext"), dict):
            return event["ext"]
    return {}


def extract_raw_query(record: dict[str, Any]) -> str | None:
    """Return the first initial user requirement embedded in an AgentOS request."""

    for messages in _request_messages(record):
        for message in messages:
            if message.get("role") != "user":
                continue
            text = _content_text(message.get("content"))
            marker_index = text.find(QUERY_MARKER)
            if marker_index < 0:
                continue
            remainder = text[marker_index + len(QUERY_MARKER) :].lstrip(" :\n\r\t")
            match = SEPARATOR.search(remainder)
            query = (remainder[: match.start()] if match else remainder).strip()
            if query and query != "立即创作":
                return query
    return None


def _benchmark_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            state_input = record.get("state_input")
            state_input = state_input if isinstance(state_input, dict) else {}
            query = state_input.get("query") or record.get("query")
            if isinstance(query, str) and query.strip():
                hashes.add(_digest(_normalize(query)))
    return hashes


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _length_summary(values: list[int]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "min": min(ordered, default=0),
        "median": statistics.median(ordered) if ordered else 0,
        "p95": ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]
        if ordered
        else 0,
        "max": max(ordered, default=0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-batch-id", required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--train-count", type=int, default=64)
    parser.add_argument("--canary-count", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260717)
    parser.add_argument("--min-query-chars", type=int, default=10)
    parser.add_argument("--max-query-chars", type=int, default=4000)
    args = parser.parse_args()

    benchmark_hashes = _benchmark_hashes(args.benchmark)
    seen: set[str] = set()
    pool: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    app_types: Counter[str] = Counter()
    ratings: Counter[str] = Counter()

    with args.source.open(encoding="utf-8") as stream:
        for source_line, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            counters["input"] += 1
            record = json.loads(line)
            if not _truthy(record.get("general_filter_pass")):
                counters["excluded_general_filter"] += 1
                continue
            if not _truthy(record.get("miaoda_filter_pass")):
                counters["excluded_miaoda_filter"] += 1
                continue
            trajectory_ext = _trajectory_ext(record)
            query_turns = str(trajectory_ext.get("query_turns") or "").strip().casefold()
            if query_turns != "head":
                counters["excluded_non_initial_turn"] += 1
                continue
            app_type = str(record.get("app_type") or "UNKNOWN").strip().upper()
            if app_type == "TOOL":
                counters["excluded_tool"] += 1
                continue
            multimodal = record.get("multimodal_files")
            if isinstance(multimodal, list) and multimodal:
                counters["excluded_multimodal"] += 1
                continue
            query = extract_raw_query(record)
            if not query:
                counters["excluded_missing_query"] += 1
                continue
            if not args.min_query_chars <= len(query) <= args.max_query_chars:
                counters["excluded_query_length"] += 1
                continue
            query_hash = _digest(_normalize(query))
            if query_hash in benchmark_hashes:
                counters["excluded_target_benchmark"] += 1
                continue
            if query_hash in seen:
                counters["excluded_duplicate"] += 1
                continue
            seen.add(query_hash)
            app_id = str(record.get("app_id") or "").strip()
            version_id = str(record.get("version_id") or "").strip()
            prd = str(record.get("prd") or "")
            rating = str(record.get("codex_reward_rating") or "unknown")
            row = {
                "id": f"{app_id}:{version_id}" if version_id else app_id,
                "query": query,
                "metadata": {
                    "source_table_type": "apps",
                    "source_batch_id": args.source_batch_id,
                    "source_line_no": source_line,
                    "app_id": app_id,
                    "conv_id": str(record.get("conv_id") or ""),
                    "version_id": version_id,
                    "app_type": app_type,
                    "query_turns": query_turns,
                    "has_db": bool(record.get("has_db")),
                    "query_sha256": query_hash,
                    "prd_sha256": _digest(prd) if prd else None,
                    "codex_reward_rating": rating,
                    "created_at": record.get("created_at"),
                },
            }
            pool.append(row)
            app_types[app_type] += 1
            ratings[rating] += 1
            counters["kept"] += 1

    required = args.train_count + args.canary_count
    if len(pool) < required:
        raise ValueError(f"only {len(pool)} eligible rows for {required} requested")
    selected = list(pool)
    random.Random(args.seed).shuffle(selected)
    canary = selected[: args.canary_count]
    train = selected[args.canary_count : required]
    for row in canary:
        row["metadata"]["split"] = "canary"
    for row in train:
        row["metadata"]["split"] = "train"

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pool_path = args.output_dir / "miaoda_raw_query_pool.jsonl"
    train_path = args.output_dir / "miaoda_raw_query_train64.jsonl"
    canary_path = args.output_dir / "miaoda_raw_query_canary8.jsonl"
    report_path = args.output_dir / "miaoda_raw_query_report.json"
    _write_jsonl(pool_path, pool)
    _write_jsonl(train_path, train)
    _write_jsonl(canary_path, canary)
    report = {
        "schema_version": "miaoda-agentos-raw-query-v1",
        "source": {
            "path": str(args.source.resolve()),
            "batch_id": args.source_batch_id,
            "sha256": _file_digest(args.source),
        },
        "target_benchmark": {
            "path": str(args.benchmark.resolve()),
            "query_hashes": len(benchmark_hashes),
        },
        "filters": {
            "general_filter_pass": True,
            "miaoda_filter_pass": True,
            "query_turns": "head",
            "excluded_app_types": ["TOOL"],
            "text_only": True,
            "query_chars": [args.min_query_chars, args.max_query_chars],
        },
        "seed": args.seed,
        "counts": dict(sorted(counters.items())),
        "pool_rows": len(pool),
        "train_rows": len(train),
        "canary_rows": len(canary),
        "app_types": dict(sorted(app_types.items())),
        "reward_ratings": dict(sorted(ratings.items())),
        "query_chars": _length_summary([len(row["query"]) for row in pool]),
        "outputs": {
            "pool": {"path": str(pool_path.resolve()), "sha256": _file_digest(pool_path)},
            "train": {"path": str(train_path.resolve()), "sha256": _file_digest(train_path)},
            "canary": {"path": str(canary_path.resolve()), "sha256": _file_digest(canary_path)},
        },
    }
    _write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
