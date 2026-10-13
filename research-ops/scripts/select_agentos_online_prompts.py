#!/usr/bin/env python3
"""Select reproducible non-benchmark Miaoda PRDs for AgentOS online rollout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import unicodedata
from pathlib import Path
from typing import Any


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).strip().lower()
    return re.sub(r"\s+", " ", value)


def _digest(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _benchmark_hashes(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("benchmark cache must contain a data list")
    hashes: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        state = record.get("state_input") or {}
        for value in (
            record.get("query"),
            record.get("prd"),
            state.get("query") if isinstance(state, dict) else None,
            state.get("prd") if isinstance(state, dict) else None,
        ):
            if isinstance(value, str) and value.strip():
                hashes.add(_digest(value))
    return hashes


def _user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    return "\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ).strip()


def _atomic_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--benchmark-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--train-count", type=int, default=64)
    parser.add_argument("--canary-count", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260717)
    args = parser.parse_args()

    benchmark_hashes = _benchmark_hashes(args.benchmark_cache)
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    with args.source.open(encoding="utf-8") as stream:
        for source_line, line in enumerate(stream, start=1):
            record = json.loads(line)
            metadata = record.get("metadata") or {}
            if metadata.get("domain") != "miaoda":
                continue
            query = _user_text(record.get("messages"))
            if not query.startswith("# 需求文档") or not 500 <= len(query) <= 5_000:
                continue
            query_hash = _digest(query)
            if query_hash in benchmark_hashes or query_hash in seen:
                continue
            seen.add(query_hash)
            eligible.append(
                {
                    "id": str(metadata.get("original_id") or f"source-{source_line}"),
                    "agentos_query": query,
                    "messages": record.get("messages") or [],
                    "metadata": {
                        **metadata,
                        "source_line": source_line,
                        "normalized_query_sha256": query_hash,
                        "target_benchmark": "example-app-300",
                        "target_benchmark_overlap": False,
                    },
                }
            )

    required = args.train_count + args.canary_count
    if len(eligible) < required:
        raise ValueError(f"only {len(eligible)} eligible prompts for {required} requested")
    random.Random(args.seed).shuffle(eligible)
    train = eligible[: args.train_count]
    canary = eligible[args.train_count : required]
    for record in train:
        record["metadata"]["online_split"] = "train"
    for record in canary:
        record["metadata"]["online_split"] = "canary"

    args.output_dir.mkdir(parents=True, exist_ok=True)
    train_path = args.output_dir / "agentos_train_candidates.jsonl"
    canary_path = args.output_dir / "agentos_canary_candidates.jsonl"
    _atomic_jsonl(train_path, train)
    _atomic_jsonl(canary_path, canary)
    summary = {
        "source": str(args.source.resolve()),
        "source_sha256": _file_digest(args.source),
        "benchmark_cache": str(args.benchmark_cache.resolve()),
        "benchmark_cache_sha256": _file_digest(args.benchmark_cache),
        "benchmark_query_hashes": len(benchmark_hashes),
        "eligible": len(eligible),
        "seed": args.seed,
        "train_count": len(train),
        "canary_count": len(canary),
        "train_sha256": _file_digest(train_path),
        "canary_sha256": _file_digest(canary_path),
        "overlap_with_target_benchmark": 0,
    }
    summary_path = args.output_dir / "selection_summary.json"
    temporary = summary_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, summary_path)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
