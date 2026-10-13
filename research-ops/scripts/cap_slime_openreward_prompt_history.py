#!/usr/bin/env python3
"""Cap debug-rollout sequences by removing only old prompt tokens.

The response suffix and every response-aligned training field are kept byte for
byte in length.  This is intended for pure OPD replay where the old prompt
history is not part of the loss mask, but the generated response and both
policy/teacher log-probability arrays must remain intact.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _list_value(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        converted = tolist()
        if isinstance(converted, list):
            return converted
    raise ValueError(f"{field} must be a list-like value")


def cap_sample(
    sample: Mapping[str, Any], *, max_tokens: int, index: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a capped sample and a secret-safe transformation summary."""

    if not isinstance(sample, Mapping):
        raise ValueError(f"sample {index} is not a mapping")
    item = copy.deepcopy(dict(sample))
    tokens = _list_value(item.get("tokens"), field=f"sample {index}.tokens")
    response_length = int(item.get("response_length") or 0)
    if response_length < 1 or response_length > len(tokens):
        raise ValueError(f"sample {index} has invalid response_length={response_length}")
    if response_length > max_tokens:
        raise ValueError(
            f"sample {index} response_length={response_length} exceeds max_tokens={max_tokens}; "
            "refusing to truncate response tokens"
        )

    for field in ("loss_mask", "rollout_log_probs", "teacher_log_probs"):
        value = item.get(field)
        if value is None:
            continue
        if len(_list_value(value, field=f"sample {index}.{field}")) != response_length:
            raise ValueError(
                f"sample {index} {field} length does not match response_length={response_length}"
            )

    original_length = len(tokens)
    dropped_prompt_tokens = max(original_length - max_tokens, 0)
    multimodal = bool(item.get("multimodal_inputs") or item.get("multimodal_train_inputs"))
    if dropped_prompt_tokens and multimodal:
        raise ValueError(
            f"sample {index} has multimodal inputs and would need left trimming; "
            "refusing to risk image-token alignment"
        )
    if dropped_prompt_tokens:
        item["tokens"] = tokens[dropped_prompt_tokens:]
        # Debug dumps normally omit text.  Avoid leaving a stale full prompt in
        # artifacts that were created with include_text=True.
        if isinstance(item.get("prompt"), str):
            item["prompt"] = ""
        metadata = dict(item.get("metadata") or {})
        metadata["openreward_prompt_history_capped"] = True
        metadata["openreward_prompt_tokens_dropped"] = dropped_prompt_tokens
        metadata["openreward_prompt_cap_max_tokens"] = max_tokens
        item["metadata"] = metadata

    return item, {
        "index": item.get("index", index),
        "original_token_count": original_length,
        "token_count": original_length - dropped_prompt_tokens,
        "response_length": response_length,
        "dropped_prompt_tokens": dropped_prompt_tokens,
        "multimodal": multimodal,
    }


def cap_payload(
    payload: Mapping[str, Any], *, max_tokens: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("artifact must be a mapping")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("artifact must contain a non-empty samples list")
    capped_samples: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        capped, row = cap_sample(sample, max_tokens=max_tokens, index=index)
        capped_samples.append(capped)
        rows.append(row)
    output = dict(payload)
    output["samples"] = capped_samples
    output["openreward_prompt_cap"] = {
        "version": 1,
        "max_tokens": max_tokens,
        "source_sample_count": len(samples),
        "sample_count": len(capped_samples),
        "dropped_prompt_tokens": sum(row["dropped_prompt_tokens"] for row in rows),
        "capped_sample_count": sum(bool(row["dropped_prompt_tokens"]) for row in rows),
    }
    summary = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "max_tokens": max_tokens,
        "sample_count": len(rows),
        "capped_sample_count": output["openreward_prompt_cap"]["capped_sample_count"],
        "dropped_prompt_tokens": output["openreward_prompt_cap"]["dropped_prompt_tokens"],
        "max_original_token_count": max(row["original_token_count"] for row in rows),
        "max_token_count": max(row["token_count"] for row in rows),
        "max_response_length": max(row["response_length"] for row in rows),
        "rows": rows,
    }
    return output, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    args = parser.parse_args()
    if args.max_tokens < 1:
        raise SystemExit("--max-tokens must be positive")
    if args.input.resolve() == args.output.resolve():
        raise SystemExit("--output must differ from --input")

    # Keep torch import lazy so the pure transformation helpers remain usable
    # in the lightweight Research Ops environment; LTP's Slime image provides it.
    import torch

    payload = torch.load(args.input, map_location="cpu", weights_only=False)
    output, summary = cap_payload(payload, max_tokens=args.max_tokens)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(output, args.output)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    public_summary = {key: value for key, value in summary.items() if key != "rows"}
    print(json.dumps(public_summary, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
