#!/usr/bin/env python3
"""Build token-matched B/C prompt arms for one online MOPD update."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def _tokens(record: dict[str, Any]) -> int:
    value = (record.get("metadata") or {}).get("prompt_token_count")
    return int(value or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--miaoda", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ppt-count", type=int, default=16)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    miaoda = _read_jsonl(args.miaoda)
    if len(miaoda) <= args.ppt_count:
        raise ValueError("miaoda arm must be larger than the replacement slice")
    ppt = [
        record
        for record in _read_jsonl(args.source)
        if (record.get("metadata") or {}).get("domain") == "ppt" and _tokens(record) > 0
    ]
    if len(ppt) < args.ppt_count:
        raise ValueError("not enough PPT prompts")

    candidates: list[tuple[int, int, int]] = []
    for miaoda_index, miaoda_record in enumerate(miaoda):
        for ppt_index, ppt_record in enumerate(ppt):
            candidates.append(
                (abs(_tokens(miaoda_record) - _tokens(ppt_record)), miaoda_index, ppt_index)
            )
    candidates.sort()
    used_miaoda: set[int] = set()
    used_ppt: set[int] = set()
    pairs: list[tuple[int, int, int]] = []
    for difference, miaoda_index, ppt_index in candidates:
        if miaoda_index in used_miaoda or ppt_index in used_ppt:
            continue
        used_miaoda.add(miaoda_index)
        used_ppt.add(ppt_index)
        pairs.append((difference, miaoda_index, ppt_index))
        if len(pairs) == args.ppt_count:
            break
    if len(pairs) != args.ppt_count:
        raise ValueError("failed to build requested token-matched pairs")

    replacement_by_slot = {miaoda_index: ppt_index for _, miaoda_index, ppt_index in pairs}
    arm_b: list[dict[str, Any]] = []
    arm_c: list[dict[str, Any]] = []
    pair_manifest: list[dict[str, Any]] = []
    for slot, miaoda_record in enumerate(miaoda):
        b_record = json.loads(json.dumps(miaoda_record))
        b_record.setdefault("metadata", {})["ablation_arm"] = "B"
        b_record["metadata"]["ablation_slot"] = slot
        b_record["metadata"]["matched_role"] = (
            "non_ppt_control" if slot in replacement_by_slot else "common_miaoda"
        )
        arm_b.append(b_record)

        if slot not in replacement_by_slot:
            c_record = json.loads(json.dumps(miaoda_record))
            c_record.setdefault("metadata", {})["ablation_arm"] = "C"
            c_record["metadata"]["ablation_slot"] = slot
            c_record["metadata"]["matched_role"] = "common_miaoda"
            arm_c.append(c_record)
            continue

        ppt_record = json.loads(json.dumps(ppt[replacement_by_slot[slot]]))
        ppt_record.setdefault("metadata", {})["ablation_arm"] = "C"
        ppt_record["metadata"]["ablation_slot"] = slot
        ppt_record["metadata"]["matched_role"] = "ppt_replacement"
        arm_c.append(ppt_record)
        pair_manifest.append(
            {
                "slot": slot,
                "miaoda_id": b_record.get("id")
                or b_record["metadata"].get("original_id"),
                "miaoda_tokens": _tokens(b_record),
                "ppt_id": ppt_record.get("id")
                or ppt_record["metadata"].get("original_id"),
                "ppt_tokens": _tokens(ppt_record),
                "absolute_token_difference": abs(_tokens(b_record) - _tokens(ppt_record)),
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    b_path = args.output_dir / "arm_b_online_miaoda_control.jsonl"
    c_path = args.output_dir / "arm_c_online_miaoda_ppt.jsonl"
    pairs_path = args.output_dir / "matched_pairs.jsonl"
    _write_jsonl(b_path, arm_b)
    _write_jsonl(c_path, arm_c)
    _write_jsonl(pairs_path, pair_manifest)
    b_tokens = sum(_tokens(record) for record in arm_b)
    c_tokens = sum(_tokens(record) for record in arm_c)
    summary = {
        "miaoda_source": str(args.miaoda.resolve()),
        "ppt_source": str(args.source.resolve()),
        "rows_per_arm": len(arm_b),
        "common_miaoda_rows": len(arm_b) - args.ppt_count,
        "matched_replacement_rows": args.ppt_count,
        "arm_b_tokens": b_tokens,
        "arm_c_tokens": c_tokens,
        "token_delta_c_minus_b": c_tokens - b_tokens,
        "token_delta_fraction": (c_tokens - b_tokens) / b_tokens,
        "mean_absolute_pair_token_difference": sum(item[0] for item in pairs) / len(pairs),
        "max_absolute_pair_token_difference": max(item[0] for item in pairs),
        "arm_b_sha256": _sha256(b_path),
        "arm_c_sha256": _sha256(c_path),
        "matched_pairs_sha256": _sha256(pairs_path),
    }
    summary_path = args.output_dir / "arm_summary.json"
    temporary = summary_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, summary_path)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
