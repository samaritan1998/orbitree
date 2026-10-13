#!/usr/bin/env python3
"""Stamp and verify a concrete SGLang weight version on a live server."""

from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def _request(url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise RuntimeError(f"SGLang returned a non-object from {url.rsplit('/', 1)[-1]}")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="SGLang base URL, without a trailing slash")
    parser.add_argument("--version", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--advertised-url",
        default="",
        help="Optional externally reachable endpoint to record in the receipt",
    )
    args = parser.parse_args()
    base = args.url.rstrip("/")
    version = str(args.version).strip()
    if not version or version == "default":
        raise SystemExit("version must be concrete and cannot be default")

    before = _request(base + "/get_weight_version").get("weight_version")
    _request(base + "/update_weight_version", method="POST", payload={"new_version": version})
    after = _request(base + "/get_weight_version").get("weight_version")
    if str(after) != version:
        raise RuntimeError(f"SGLang weight version did not update: {after!r}")
    result = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "verified",
        "endpoint": str(args.advertised_url).strip() or base,
        "local_endpoint": base,
        "previous_weight_version": before,
        "weight_version": str(after),
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=args.receipt.parent,
        prefix=f".{args.receipt.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(args.receipt)
    print(json.dumps({"status": "verified", "weight_version": str(after)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
