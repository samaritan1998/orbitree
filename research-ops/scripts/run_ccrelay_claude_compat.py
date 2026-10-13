#!/usr/bin/env python3
"""Run ccrelay 1.0.0 with Claude Code 2.1 metadata compatibility."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_LEGACY_USER_ID = re.compile(
    r"^user_[^_]+_account_[^_]+_session_[^_]+$",
)


def _segment(value: Any, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "")).strip("-.")
    return normalized[:96] or fallback


def normalize_claude_metadata(body: bytes, *, session_hint: str = "") -> bytes:
    """Convert Claude's JSON user_id into ccrelay's six-segment legacy value."""

    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeError):
        return body
    if not isinstance(payload, Mapping):
        return body
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return body
    raw_user_id = metadata.get("user_id")
    if isinstance(raw_user_id, str) and _LEGACY_USER_ID.fullmatch(raw_user_id):
        return body
    decoded: Mapping[str, Any] = {}
    if isinstance(raw_user_id, Mapping):
        decoded = raw_user_id
    elif isinstance(raw_user_id, str):
        try:
            candidate = json.loads(raw_user_id)
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, Mapping):
            decoded = candidate
    fingerprint = hashlib.sha256(str(raw_user_id or "").encode()).hexdigest()[:24]
    uid = _segment(decoded.get("device_id"), fallback=fingerprint)
    account = _segment(decoded.get("account_uuid"), fallback="anonymous")
    session = _segment(decoded.get("session_id") or session_hint, fallback="unknown")
    updated = dict(payload)
    updated_metadata = dict(metadata)
    updated_metadata["user_id"] = f"user_{uid}_account_{account}_session_{session}"
    updated["metadata"] = updated_metadata
    return json.dumps(updated, ensure_ascii=False, separators=(",", ":")).encode()


def _replace_request_body(request: Any, body: bytes) -> None:
    request._body = body
    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    request.scope["headers"] = headers
    request.__dict__.pop("_headers", None)


def _temporary_config(upstreams: list[str]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(
            {
                "default_endpoint": "upstream0",
                "endpoints": [
                    {"name": f"upstream{index}", "base_url": value, "weight": 1}
                    for index, value in enumerate(upstreams)
                ],
            },
            handle,
        )
        path = Path(handle.name)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--upstream", action="append", required=True)
    parser.add_argument("--log-path", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = _temporary_config([item.rstrip("/") for item in args.upstream])
    os.environ["RELAY_CONFIG_PATH"] = str(config_path)
    os.environ["RELAY_IO_LOG_PATH"] = args.log_path
    try:
        import uvicorn
        from app import main as relay_main
    except ImportError as exc:  # pragma: no cover - supplied by the rollout image
        raise RuntimeError("the configured image does not contain ccrelay 1.0.0") from exc

    original_forward = relay_main.forward_request

    async def compatible_forward(
        request: Any,
        client: Any,
        endpoint_name: str | None = None,
        force_stream: bool | None = None,
    ) -> Any:
        body = await request.body()
        normalized = normalize_claude_metadata(
            body,
            session_hint=str(request.headers.get("X-Session-Id") or ""),
        )
        if normalized != body:
            _replace_request_body(request, normalized)
        return await original_forward(
            request=request,
            client=client,
            endpoint_name=endpoint_name,
            force_stream=force_stream,
        )

    relay_main.forward_request = compatible_forward
    try:
        uvicorn.run(relay_main.app, host=args.host, port=args.port)
    finally:
        config_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
