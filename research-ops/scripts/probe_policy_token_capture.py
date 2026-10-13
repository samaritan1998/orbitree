#!/usr/bin/env python3
"""Validate tokenizer-side policy capture against a live safe-prompt response."""

from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import httpx


def _load_proxy(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("policy_capture_probe_proxy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("weight-version proxy module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokenizer-path", required=True)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--proxy-script", type=Path, required=True)
    parser.add_argument("--episode-id", default="policy-token-capture-canary")
    args = parser.parse_args()

    proxy = _load_proxy(args.proxy_script)
    capture = proxy.PolicyTokenCapture.from_tokenizer_path(
        args.capture_dir,
        args.tokenizer_path,
    )
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Reply exactly OK."}],
        "max_tokens": 8,
        "temperature": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
        "logprobs": True,
        "top_logprobs": 0,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path = f"/v1/chat/completions?mopd_episode_id={args.episode_id}"
    with httpx.Client(base_url=args.gateway_url, timeout=120, trust_env=False) as client:
        response = client.post(path, content=body, headers={"Content-Type": "application/json"})
    response.raise_for_status()
    captured = capture.capture(
        request_body=body,
        response_body=response.content,
        request_path=path,
        request_headers={},
    )
    capture_path = args.capture_dir / f"capture-{args.episode_id}.jsonl"
    if not captured or not capture_path.is_file():
        raise RuntimeError("tokenizer-side capture did not materialize")
    rows = [json.loads(line) for line in capture_path.read_text().splitlines() if line]
    record = rows[-1]
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    prompt_count = int(record.get("prompt_token_count") or 0)
    output_count = int(record.get("output_token_count") or 0)
    if prompt_count != int(usage.get("prompt_tokens") or -1):
        raise RuntimeError("captured prompt token count differs from gateway usage")
    if output_count != int(usage.get("completion_tokens") or -1):
        raise RuntimeError("captured output token count differs from gateway usage")
    if output_count != len(record.get("behavior_logprobs") or []):
        raise RuntimeError("captured response token/logprob counts differ")
    receipt = {
        "version": 1,
        "status": "verified",
        "verified_at": datetime.now(UTC).isoformat(),
        "episode_id": args.episode_id,
        "prompt_token_count": prompt_count,
        "output_token_count": output_count,
        "weight_version": str(record.get("weight_version") or ""),
        "raw_text_persisted": False,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
