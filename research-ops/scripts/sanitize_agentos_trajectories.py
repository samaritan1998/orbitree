#!/usr/bin/env python3
"""Redact credential-like values from downloaded AgentOS trajectory JSON."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "jwt",
    "password",
    "secret",
    "token",
    "access_token",
    "auth_token",
    "bearer_token",
    "refresh_token",
    "x_authorization",
}
BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!<redacted>)[^\s,\"']+")
SIGNED_URL_RE = re.compile(
    r"(?i)https?://[^\s\"']*[?&](?:authorization|signature|token)=[^\s\"']+"
)


@dataclass
class Counts:
    sensitive_fields: int = 0
    bearer_strings: int = 0
    signed_urls: int = 0


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret")
    )


def sanitize(value: Any, *, key: str = "", counts: Counts | None = None) -> Any:
    counts = counts or Counts()
    if _sensitive_key(key):
        if value not in (None, "", "<redacted>"):
            counts.sensitive_fields += 1
        return "<redacted>"
    if isinstance(value, str):
        value, bearer_count = BEARER_RE.subn("Bearer <redacted>", value)
        value, signed_count = SIGNED_URL_RE.subn("<signed-url-redacted>", value)
        counts.bearer_strings += bearer_count
        counts.signed_urls += signed_count
        return value
    if isinstance(value, dict):
        return {str(k): sanitize(v, key=str(k), counts=counts) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, counts=counts) for item in value]
    return value


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--in-place", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    paths = sorted(args.input_dir.glob("*.json"))
    if not paths:
        parser.error(f"no JSON files found in {args.input_dir}")
    output_dir = args.input_dir if args.in_place else args.output_dir
    assert output_dir is not None
    reports: list[dict[str, Any]] = []
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        counts = Counts()
        cleaned = sanitize(value, counts=counts)
        output = output_dir / path.name
        _write(output, cleaned)
        text = output.read_text(encoding="utf-8")
        verified = not BEARER_RE.search(text) and not SIGNED_URL_RE.search(text)
        reports.append(
            {
                "file": path.name,
                "output": str(output.resolve()),
                "sensitive_fields_redacted": counts.sensitive_fields,
                "bearer_strings_redacted": counts.bearer_strings,
                "signed_urls_redacted": counts.signed_urls,
                "verified": verified,
                "bytes": output.stat().st_size,
            }
        )
    summary = {
        "files": len(reports),
        "in_place": args.in_place,
        "sensitive_fields_redacted": sum(
            item["sensitive_fields_redacted"] for item in reports
        ),
        "bearer_strings_redacted": sum(item["bearer_strings_redacted"] for item in reports),
        "signed_urls_redacted": sum(item["signed_urls_redacted"] for item in reports),
        "verified_files": sum(bool(item["verified"]) for item in reports),
        "reports": reports,
    }
    _write(args.report, summary)
    print(
        json.dumps(
            {key: summary[key] for key in summary if key != "reports"},
            ensure_ascii=False,
        )
    )
    return 0 if summary["verified_files"] == summary["files"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
