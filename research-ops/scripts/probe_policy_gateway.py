#!/usr/bin/env python3
"""Check the policy gateway contract needed by the OpenReward Slime adapter.

The probe records only counts, hashes, status codes, and weight-version
identifiers.  It never writes response text, API keys, or full token arrays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _version(payload: Any) -> str | None:
    data = _mapping(payload)
    for key in ("weight_version", "version", "checkpoint", "model_version"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    nested = data.get("data")
    if isinstance(nested, Mapping):
        return _version(nested)
    return None


def _extract_logprobs(payload: Mapping[str, Any]) -> tuple[int, int, str | None]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0, 0, None
    choice = _mapping(choices[0])
    message = _mapping(choice.get("message"))
    text = str(message.get("content") or choice.get("text") or "")
    logprobs = _mapping(choice.get("logprobs"))
    content = logprobs.get("content")
    if not isinstance(content, list):
        content = _mapping(choice.get("meta_info")).get("output_token_logprobs")
    if not isinstance(content, list):
        content = _mapping(payload.get("meta_info")).get("output_token_logprobs")
    token_count = len(content) if isinstance(content, list) else 0
    valid_count = 0
    if isinstance(content, list):
        for item in content:
            raw_logprob: Any = None
            if isinstance(item, Mapping):
                raw_logprob = item.get("logprob", item.get("log_prob"))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                raw_logprob = item[0]
            try:
                finite = raw_logprob is not None and math.isfinite(float(raw_logprob))
            except (TypeError, ValueError):
                finite = False
            if finite:
                valid_count += 1
    return token_count, valid_count, hashlib.sha256(text.encode()).hexdigest()


def _extract_sglang_logprobs(payload: Mapping[str, Any]) -> tuple[int, int, str | None]:
    meta = _mapping(payload.get("meta_info"))
    records = meta.get("output_token_logprobs")
    if not isinstance(records, list):
        return 0, 0, None
    valid = 0
    token_ids: list[str] = []
    for item in records:
        if isinstance(item, Mapping):
            token_id = item.get("token_id", item.get("tokenId"))
            logprob = item.get("logprob", item.get("log_prob"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            logprob, token_id = item[0], item[1]
        else:
            continue
        if token_id is None or logprob is None:
            continue
        try:
            int(token_id)
            numeric_logprob = float(logprob)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric_logprob):
            continue
        valid += 1
        token_ids.append(str(token_id))
    text = str(payload.get("text") or "")
    digest_input = "|".join(token_ids) if token_ids else text
    return len(records), valid, hashlib.sha256(digest_input.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("MOPD_POLICY_API_URL", ""))
    parser.add_argument("--model", default=os.environ.get("MOPD_POLICY_MODEL_NAME", ""))
    parser.add_argument("--api-key-env", default="MOPD_POLICY_API_KEY")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args()
    base_url = str(args.base_url).strip().rstrip("/")
    if not base_url or not args.model:
        raise SystemExit("policy gateway URL and model name are required")
    key = str(os.environ.get(args.api_key_env, "")).strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = key if key.casefold().startswith("bearer ") else f"Bearer {key}"
    started = datetime.now(UTC)
    evidence: dict[str, Any] = {
        "version": 1,
        "created_at": started.isoformat(),
        "base_url": base_url,
        "model": args.model,
        "checks": {},
    }
    with httpx.Client(base_url=base_url, headers=headers, timeout=120, trust_env=False) as client:
        for path in ("/health", "/v1/models", "/weight_version", "/model_info"):
            try:
                response = client.get(path)
                item: dict[str, Any] = {"status_code": response.status_code}
                if response.is_success:
                    try:
                        item["weight_version"] = _version(response.json())
                    except (TypeError, ValueError):
                        item["json"] = False
                evidence["checks"][path] = item
            except httpx.HTTPError as exc:
                evidence["checks"][path] = {"error_type": type(exc).__name__}
        try:
            response = client.get("/get_weight_version")
            item = {"status_code": response.status_code}
            if response.is_success:
                item["weight_version"] = _version(response.json())
            evidence["checks"]["/get_weight_version"] = item
        except httpx.HTTPError as exc:
            evidence["checks"]["/get_weight_version"] = {"error_type": type(exc).__name__}
        payload = {
            "model": args.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Return a short JSON object with an integer field named ok.",
                }
            ],
            "max_tokens": args.max_tokens,
            "temperature": 0.0,
            "logprobs": True,
            "top_logprobs": 0,
            "return_logprob": True,
            "logprob_start_len": 0,
            "metadata": {"episode_id": "ors-gateway-probe"},
        }
        response = client.post("/v1/chat/completions", json=payload)
        evidence["checks"]["/v1/chat/completions"] = {"status_code": response.status_code}
        if response.is_success:
            body = response.json()
            token_count, valid_count, text_hash = _extract_logprobs(body)
            evidence["checks"]["/v1/chat/completions"].update(
                {
                    "token_count": token_count,
                    "valid_logprob_count": valid_count,
                    "text_sha256": text_hash,
                    "weight_version": _version(body),
                }
            )
        raw_payload = {
            "model": args.model,
            "input_ids": [1],
            "sampling_params": {"temperature": 0.0, "max_new_tokens": args.max_tokens},
            "return_logprob": True,
        }
        try:
            raw_response = client.post("/generate", json=raw_payload)
            raw_item: dict[str, Any] = {"status_code": raw_response.status_code}
            if raw_response.is_success:
                raw_body = raw_response.json()
                token_count, valid_count, token_hash = _extract_sglang_logprobs(raw_body)
                raw_item.update(
                    {
                        "token_count": token_count,
                        "valid_logprob_count": valid_count,
                        "token_ids_sha256": token_hash,
                        "weight_version": _version(raw_body),
                    }
                )
            evidence["checks"]["/generate"] = raw_item
        except httpx.HTTPError as exc:
            evidence["checks"]["/generate"] = {"error_type": type(exc).__name__}
    completion = evidence["checks"]["/v1/chat/completions"]
    raw_generate = evidence["checks"].get("/generate", {})
    evidence["ready"] = bool(
        completion.get("status_code") == 200
        and completion.get("token_count", 0) > 0
        and completion.get("valid_logprob_count") == completion.get("token_count")
        and raw_generate.get("status_code") == 200
        and raw_generate.get("token_count", 0) > 0
        and raw_generate.get("valid_logprob_count") == raw_generate.get("token_count")
    )
    evidence["duration_seconds"] = (datetime.now(UTC) - started).total_seconds()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
