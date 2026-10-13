#!/usr/bin/env python3
"""Build a Warehouse-shaped PPT trajectory canary from the 500-task rollout."""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import struct
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PRODUCER_VERSION = "ppt_trajectory_canary.v0.4.20260811"
SCHEMA_VERSION = "ppt_trajectory.v1"
DEFAULT_RUN_ROOT = Path(
    "/srv/research-ops/projects/example/ppt/pipelinetest/example-ppt-run"
)
DEFAULT_OUTPUT_NAME = "ppt_trajectory_mock_canary_reward_inline_20260811"
DEFAULT_BATCH_ID = "canary_ppt_trajectory_500_reward_inline_20260811"
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)((?:authorization|x-api-key|api[_-]?key)\s*[:=]\s*)[^\s,;\"']+"),
)
URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
FORBIDDEN_KEYS = {
    "api_key_sha256",
    "authorization",
    "proxy-authorization",
    "proxy_authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x_api_key",
}
JSON_COLUMNS = {
    "task_axes",
    "reference_files",
    "input_assets",
    "task_metadata",
    "skill_refs",
    "runtime_config",
    "trajectory",
    "trajectory_transformations",
    "llm_trace",
    "trajectory_images",
    "raw_manifest",
    "thinking_decode",
    "error_detail",
    "risk_flags",
    "rendered_slides",
    "output_files",
    "usage_detail",
    "reward_judge_models",
    "reward_detail",
    "validation_errors",
}
PRE_SERIALIZED_JSON_COLUMNS = {"trajectory", "llm_trace"}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_record_hash(values: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in sorted(values):
        value = values[key]
        encoded_key = key.encode("utf-8")
        encoded_value = (
            value.encode("utf-8")
            if isinstance(value, str)
            else canonical_json(value).encode("utf-8")
        )
        digest.update(len(encoded_key).to_bytes(4, "big"))
        digest.update(encoded_key)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            yield value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def redact_string(value: str) -> str:
    result = value.replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)((?:authorization"):
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return URL_USERINFO_RE.sub(r"\1[REDACTED]@", result)


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "-")
            if str(key).lower() in FORBIDDEN_KEYS or normalized in FORBIDDEN_KEYS:
                continue
            result[str(key)] = sanitize(item)
        return result
    return value


def secret_hits(value: str) -> list[str]:
    hits: list[str] = []
    labels = ("sk_token", "bearer_token", "credential_assignment")
    for label, pattern in zip(labels, SECRET_PATTERNS, strict=True):
        if pattern.search(value):
            hits.append(label)
    if URL_USERINFO_RE.search(value):
        hits.append("url_userinfo")
    return hits


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset : offset + 2], "big")
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            } and offset + 7 <= len(data):
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return width, height
            offset += max(length, 2)
    return None, None


def media_extension(media_type: str) -> str:
    known = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return known.get(media_type.lower()) or mimetypes.guess_extension(media_type) or ".bin"


class AssetStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.image_dir = output_dir / "assets" / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.unique: dict[str, dict[str, Any]] = {}
        self.encoded_cache: dict[str, dict[str, Any]] = {}
        self.occurrence_count = 0
        self.decode_errors = 0
        self.lock = threading.Lock()

    def materialize(self, encoded: str, media_type: str) -> dict[str, Any]:
        encoded_digest = sha256_text(encoded)
        with self.lock:
            self.occurrence_count += 1
            cached = self.encoded_cache.get(encoded_digest)
        if cached is not None:
            return dict(cached)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception:
            with self.lock:
                self.decode_errors += 1
            return {
                "type": "file",
                "media_type": media_type,
                "path": None,
                "sha256": None,
                "size_bytes": None,
                "width": None,
                "height": None,
                "externalization_status": "invalid_base64",
            }
        digest = sha256_bytes(raw)
        extension = media_extension(media_type)
        relative = Path("assets") / "images" / f"{digest}{extension}"
        destination = self.output_dir / relative
        width, height = image_dimensions(raw)
        meta = {
            "type": "file",
            "media_type": media_type,
            "path": relative.as_posix(),
            "sha256": digest,
            "size_bytes": len(raw),
            "width": width,
            "height": height,
            "externalization_status": "complete",
        }
        with self.lock:
            cached = self.encoded_cache.get(encoded_digest)
            if cached is not None:
                return dict(cached)
            if not destination.exists():
                temp = destination.with_name(destination.name + f".{threading.get_ident()}.tmp")
                temp.write_bytes(raw)
                os.replace(temp, destination)
            self.unique[digest] = meta
            self.encoded_cache[encoded_digest] = meta
        return dict(meta)


def externalize_images(
    value: Any,
    store: AssetStore,
    *,
    observations: list[dict[str, Any]] | None = None,
    pointer: str = "",
    event_index: int | None = None,
    tool_call_id: str | None = None,
) -> Any:
    if isinstance(value, list):
        return [
            externalize_images(
                item,
                store,
                observations=observations,
                pointer=f"{pointer}/{index}",
                event_index=event_index,
                tool_call_id=tool_call_id,
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    local_tool_id = tool_call_id
    if value.get("type") == "tool_result" and value.get("tool_use_id"):
        local_tool_id = str(value["tool_use_id"])
    source = value.get("source")
    if (
        value.get("type") in {"image", "input_image"}
        and isinstance(source, dict)
        and source.get("type") == "base64"
        and isinstance(source.get("data"), str)
    ):
        media_type = str(source.get("media_type") or "application/octet-stream")
        replacement = store.materialize(source["data"], media_type)
        result = dict(value)
        result["source"] = replacement
        if observations is not None:
            observations.append(
                {
                    "index": len(observations),
                    "event_index": event_index,
                    "json_pointer": f"{pointer}/source",
                    "tool_call_id": local_tool_id,
                    **replacement,
                }
            )
        return result
    result: dict[str, Any] = {}
    for key, item in value.items():
        result[str(key)] = externalize_images(
            item,
            store,
            observations=observations,
            pointer=f"{pointer}/{key}",
            event_index=event_index,
            tool_call_id=local_tool_id,
        )
    return result


def stable_trajectory_id(run_id: str, task_id: str, attempt_index: int) -> str:
    source = f"{run_id}\n{task_id}\n{attempt_index}"
    return "ppttrj_" + sha256_text(source)[:24]


def load_attempt_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    counts: collections.Counter[str] = collections.Counter()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for raw_line in handle:
            digest.update(raw_line)
            line = raw_line.decode("utf-8", errors="replace")
            if not line.startswith("{"):
                if line.strip():
                    counts["non_json_preamble_lines"] += 1
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                counts["invalid_json_lines"] += 1
                continue
            if isinstance(value, dict):
                events.append(value)
            else:
                counts["non_object_json_lines"] += 1
    counts["events"] = len(events)
    return events, {**dict(counts), "source_sha256": digest.hexdigest()}


def content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def trajectory_facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    init = next(
        (
            event
            for event in events
            if event.get("type") == "system" and event.get("subtype") == "init"
        ),
        {},
    )
    result = next((event for event in reversed(events) if event.get("type") == "result"), {})
    calls: list[str] = []
    results: list[str] = []
    thinking_positions: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    stop_reason = None
    for event_index, event in enumerate(events):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if message.get("stop_reason"):
            stop_reason = message["stop_reason"]
        for content_index, block in enumerate(content_blocks(event)):
            if block.get("type") == "tool_use" and block.get("id"):
                calls.append(str(block["id"]))
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                results.append(str(block["tool_use_id"]))
            elif block.get("type") == "thinking" and block.get("signature"):
                digest = sha256_text(str(block["signature"]))
                thinking_positions[digest].append(
                    {"event_index": event_index, "content_index": content_index}
                )
    return {
        "init": init,
        "result": result,
        "stop_reason": stop_reason,
        "tool_call_ids": calls,
        "tool_result_ids": results,
        "thinking_positions": dict(thinking_positions),
    }


def backfill_trajectory_thinking(
    events: list[dict[str, Any]], decode_map: dict[str, dict[str, Any]]
) -> tuple[int, int]:
    success = unresolved = 0
    for event in events:
        for block in content_blocks(event):
            if block.get("type") != "thinking" or not block.get("signature"):
                continue
            digest = sha256_text(str(block["signature"]))
            decode = decode_map.get(digest) or {}
            text = decode.get("decoded_text") if decode.get("decode_status") == "success" else None
            if isinstance(text, str) and text.strip():
                block["thinking"] = text
                success += 1
            else:
                unresolved += 1
    return success, unresolved


def load_decode_state(
    run_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]], set[str]]:
    decode_dir = run_root / "postprocess" / "decode_opus47_valid_merged_final"
    inventory_dir = run_root / "postprocess" / "signature_inventory_final"
    attempt_counts: collections.Counter[str] = collections.Counter()
    for row in read_jsonl(decode_dir / "decode_attempts_all.jsonl"):
        if row.get("signature_sha256"):
            attempt_counts[str(row["signature_sha256"])] += 1
    eligible = {
        str(row["signature_sha256"])
        for row in read_jsonl(inventory_dir / "signature_unique_opus47_valid.jsonl")
    }
    decode_map: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(decode_dir / "decode_results_final.jsonl"):
        digest = str(row["signature_sha256"])
        final = row.get("decode") or {}
        response = final.get("response") or {}
        if final.get("ok") and isinstance(response.get("text"), str) and response["text"].strip():
            decode_map[digest] = {
                "decode_status": "success",
                "decoded_text": response["text"].strip(),
                "decoded_by": response.get("decoded_by"),
                "cache_hit": response.get("cache_hit"),
                "decoder_usage": (response.get("statistics") or {}).get("usage"),
                "decode_attempt_count": int(attempt_counts[digest]),
                "decoded_at": final.get("started_at_utc"),
                "http_status": final.get("http_status"),
                "error_code": None,
            }
        else:
            response_error = response.get("error") if isinstance(response, dict) else None
            code = response_error.get("code") if isinstance(response_error, dict) else None
            decode_map[digest] = {
                "decode_status": "failed",
                "decoded_text": None,
                "decoded_by": None,
                "cache_hit": None,
                "decoder_usage": None,
                "decode_attempt_count": int(attempt_counts[digest]),
                "decoded_at": final.get("started_at_utc"),
                "http_status": final.get("http_status"),
                "error_code": code or "decode_failed",
            }
    occurrences: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in read_jsonl(inventory_dir / "signature_occurrences.jsonl"):
        occurrences[(str(row.get("split")), str(row.get("task_id")))].append(row)
    return decode_map, dict(occurrences), eligible


def build_decode_occurrences(
    source_rows: list[dict[str, Any]],
    decode_map: dict[str, dict[str, Any]],
    eligible: set[str],
    trajectory_positions: dict[str, list[dict[str, Any]]],
    sidecar_path: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    result: list[dict[str, Any]] = []
    position_cursor: collections.Counter[str] = collections.Counter()
    counts: collections.Counter[str] = collections.Counter()
    for local_index, source in enumerate(source_rows, 1):
        digest = str(source.get("signature_sha256") or "")
        decode = decode_map.get(digest)
        if digest not in eligible:
            decode = {
                "decode_status": "not_attempted",
                "decoded_text": None,
                "decoded_by": None,
                "cache_hit": None,
                "decoder_usage": None,
                "decode_attempt_count": 0,
                "decoded_at": None,
                "http_status": None,
                "error_code": "ineligible_signature",
            }
        elif decode is None:
            decode = {
                "decode_status": "not_attempted",
                "decoded_text": None,
                "decoded_by": None,
                "cache_hit": None,
                "decoder_usage": None,
                "decode_attempt_count": 0,
                "decoded_at": None,
                "http_status": None,
                "error_code": "decode_result_missing",
            }
        positions = trajectory_positions.get(digest) or []
        cursor = position_cursor[digest]
        position = positions[cursor] if cursor < len(positions) else {}
        position_cursor[digest] += 1
        status = str(decode["decode_status"])
        counts[status] += 1
        item = {
            "occurrence_index": local_index,
            "source_occurrence_index": source.get("occurrence_index"),
            "source_stream": (
                "llm_trace" if source.get("source_kind") == "raw_ccrelay" else "trajectory"
            ),
            "source_kind": source.get("source_kind"),
            "request_index": source.get("request_index"),
            "request_id": source.get("request_id"),
            "request_ts": source.get("request_ts"),
            "message_index": position.get("event_index"),
            "content_index": source.get("content_index"),
            "trajectory_content_index": position.get("content_index"),
            "request_model": source.get("request_model"),
            "response_model": source.get("response_model"),
            "signature": source.get("signature"),
            "signature_sha256": digest,
            "signature_length": source.get("signature_length"),
            "base64_valid": source.get("base64_valid"),
            "decode_status": status,
            "decoded_text": decode.get("decoded_text"),
            "decoder_protocol_version": "thinking_signature_decode_final_v1",
            "decoder_mode": "sonnet5",
            "decoded_by": decode.get("decoded_by"),
            "cache_hit": decode.get("cache_hit"),
            "decode_attempt_count": decode.get("decode_attempt_count"),
            "decoded_at": decode.get("decoded_at"),
            "http_status": decode.get("http_status"),
            "error_code": decode.get("error_code"),
            "decoder_usage": decode.get("decoder_usage"),
            "decoder_error_sidecar_path": (str(sidecar_path) if status == "failed" else None),
        }
        result.append(item)
    total = len(result)
    if total == 0:
        status = "not_applicable"
    elif counts["success"] == total:
        status = "complete"
    elif counts["success"]:
        status = "partial"
    else:
        status = "unresolved"
    payload = {
        "version": "thinking_decode.v1",
        "status": status,
        "occurrences": result,
    }
    return payload, {"total": total, **dict(counts)}


def overlay_llm_trace(
    trace: list[dict[str, Any]],
    decode_occurrences: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for occurrence in decode_occurrences:
        request_index = occurrence.get("request_index")
        if occurrence.get("source_stream") != "llm_trace" or not request_index:
            continue
        index = int(request_index) - 1
        if index < 0 or index >= len(trace):
            errors.append(f"signature_request_index_out_of_range:{request_index}")
            continue
        row = trace[index]
        expected_request_id = occurrence.get("request_id")
        if expected_request_id and row.get("request_id") != expected_request_id:
            errors.append(f"signature_request_id_mismatch:{request_index}")
        response = row.get("response")
        blocks = response.get("content") if isinstance(response, dict) else None
        content_index = occurrence.get("content_index")
        if not isinstance(blocks, list) or not isinstance(content_index, int):
            errors.append(f"signature_response_content_missing:{request_index}")
            continue
        if content_index < 0 or content_index >= len(blocks):
            errors.append(f"signature_content_index_out_of_range:{request_index}:{content_index}")
            continue
        block = blocks[content_index]
        if not isinstance(block, dict) or block.get("type") != "thinking":
            errors.append(f"signature_target_not_thinking:{request_index}:{content_index}")
            continue
        block["signature"] = occurrence.get("signature")
        if occurrence.get("decode_status") == "success":
            block["thinking"] = occurrence.get("decoded_text")
    return errors


def backfill_nested_thinking(value: Any, decoded_by_signature: dict[str, str]) -> int:
    count = 0
    if isinstance(value, list):
        for item in value:
            count += backfill_nested_thinking(item, decoded_by_signature)
        return count
    if not isinstance(value, dict):
        return count
    if value.get("type") == "thinking" and value.get("signature"):
        digest = sha256_text(str(value["signature"]))
        decoded = decoded_by_signature.get(digest)
        if decoded:
            value["thinking"] = decoded
            count += 1
    for item in value.values():
        count += backfill_nested_thinking(item, decoded_by_signature)
    return count


def load_response_statuses(run_root: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    path = run_root / "postprocess" / "signature_inventory_final" / "response_manifest.jsonl"
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in read_jsonl(path):
        grouped[(str(row.get("split")), str(row.get("task_id")))].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: int(item.get("request_index") or 0))
    return dict(grouped)


def load_llm_trace(
    path: Path,
    statuses: list[dict[str, Any]],
    decode_payload: dict[str, Any],
    store: AssetStore,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    trace: list[dict[str, Any]] = []
    source_digest = hashlib.sha256()
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            source_digest.update(raw_line)
            if not raw_line.strip():
                continue
            value = json.loads(raw_line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected object")
            trace.append(value)
    errors = overlay_llm_trace(trace, decode_payload.get("occurrences") or [])
    decoded_by_signature = {
        str(item["signature_sha256"]): str(item["decoded_text"])
        for item in decode_payload.get("occurrences") or []
        if item.get("decode_status") == "success" and item.get("decoded_text")
    }
    history_backfill_count = backfill_nested_thinking(trace, decoded_by_signature)
    status_by_index = {int(row.get("request_index") or 0): row for row in statuses}
    request_models: collections.Counter[str] = collections.Counter()
    response_models: collections.Counter[str] = collections.Counter()
    usage: collections.Counter[str] = collections.Counter()
    stop_reason = None
    for request_index, row in enumerate(trace, 1):
        status = status_by_index.get(request_index) or {}
        row["raw_response_status_code"] = status.get("raw_response_status_code")
        row["raw_response_duration_ms"] = status.get("raw_response_duration_ms")
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        request_models[str(request.get("model") or "<missing>")] += 1
        response_models[str(response.get("model") or "<missing>")] += 1
        if response.get("stop_reason"):
            stop_reason = response["stop_reason"]
        response_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        usage["input_tokens"] += int(response_usage.get("input_tokens") or 0)
        usage["cache_creation_input_tokens"] += int(
            response_usage.get("cache_creation_input_tokens") or 0
        )
        usage["cache_read_input_tokens"] += int(response_usage.get("cache_read_input_tokens") or 0)
        usage["output_tokens"] += int(response_usage.get("output_tokens") or 0)
        output_detail = (
            response_usage.get("output_tokens_details")
            if isinstance(response_usage.get("output_tokens_details"), dict)
            else {}
        )
        usage["thinking_tokens"] += int(output_detail.get("thinking_tokens") or 0)
        trace[request_index - 1] = sanitize(externalize_images(row, store))
    facts = {
        "request_count": len(trace),
        "response_count": sum(bool(row.get("response")) for row in trace),
        "request_models": dict(request_models),
        "response_models": dict(response_models),
        "usage": dict(usage),
        "stop_reason": stop_reason,
        "history_thinking_backfill_count": history_backfill_count,
        "source_sha256": source_digest.hexdigest(),
    }
    return trace, facts, errors


def asset_metadata(path: Path, *, hash_cache: dict[str, str | None]) -> dict[str, Any]:
    exists = path.exists()
    is_file = path.is_file()
    key = str(path)
    digest = None
    if is_file:
        if key not in hash_cache:
            hash_cache[key] = file_sha256(path)
        digest = hash_cache[key]
    return {
        "path": str(path),
        "exists": exists,
        "kind": "file" if is_file else "directory" if path.is_dir() else "missing",
        "media_type": mimetypes.guess_type(path.name)[0],
        "size_bytes": path.stat().st_size if is_file else None,
        "sha256": digest,
    }


def rendered_slide_metadata(
    rendered_dir: Path, hash_cache: dict[str, str | None]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not rendered_dir.is_dir():
        return rows
    paths = sorted(
        list(rendered_dir.glob("slide-v*.jpg")) + list(rendered_dir.glob("slide-v*.png"))
    )
    for index, path in enumerate(paths, 1):
        meta = asset_metadata(path, hash_cache=hash_cache)
        try:
            width, height = image_dimensions(path.read_bytes())
        except OSError:
            width = height = None
        rows.append({"index": index, **meta, "width": width, "height": height})
    return rows


def load_costs(run_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in read_jsonl(run_root / "postprocess" / "cost_final" / "cost_records.jsonl"):
        result[(str(row.get("split")), str(row.get("task_id")))] = row
    fallback_path = run_root / "postprocess" / "cost_audit_final" / "missing_cost_records.jsonl"
    for row in read_jsonl(fallback_path):
        split = (
            "retry_missing_8" if "/retry_missing_8/" in str(row.get("source_trace")) else "rollout"
        )
        result[(split, str(row.get("task_id")))] = {
            "split": split,
            "task_id": row.get("task_id"),
            "total_cost_usd": row.get("total_cost_usd"),
            "usage": {},
            "model_usage": row.get("model_usage") or {},
            "cost_source": "raw_usage_fallback",
        }
    return result


def normalized_cost(cost: dict[str, Any] | None, thinking_tokens: int) -> dict[str, Any]:
    if not cost:
        return {
            "input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "output_tokens": None,
            "thinking_tokens": thinking_tokens,
            "usage_detail": {"status": "missing"},
            "cost_usd": None,
            "cost_cny": None,
            "cost_audit_status": "missing",
        }
    usage = cost.get("usage") if isinstance(cost.get("usage"), dict) else {}
    model_usage = cost.get("model_usage") if isinstance(cost.get("model_usage"), dict) else {}

    def total_usage(snake: str, camel: str) -> int:
        if usage.get(snake) is not None:
            return int(usage.get(snake) or 0)
        return sum(
            int((item or {}).get(snake) or (item or {}).get(camel) or 0)
            for item in model_usage.values()
            if isinstance(item, dict)
        )

    cost_usd = float(cost.get("total_cost_usd") or 0.0)
    return {
        "input_tokens": total_usage("input_tokens", "inputTokens"),
        "cache_creation_input_tokens": total_usage(
            "cache_creation_input_tokens", "cacheCreationInputTokens"
        ),
        "cache_read_input_tokens": total_usage("cache_read_input_tokens", "cacheReadInputTokens"),
        "output_tokens": total_usage("output_tokens", "outputTokens"),
        "thinking_tokens": thinking_tokens,
        "usage_detail": {
            "source": cost.get("cost_source") or "claude_result_log",
            "model_usage": model_usage,
            "cache_included": True,
        },
        "cost_usd": round(cost_usd, 9),
        "cost_cny": round(cost_usd * 6.7928, 6),
        "cost_audit_status": "complete",
    }


def load_rewards(run_root: Path) -> dict[str, dict[str, Any]]:
    reward_root = run_root / "postprocess" / "agentic_reward_legacy_scale_20260803"
    score_path = reward_root / "final_agentic_reward_vlm_opus47" / "agentic_reward_scores.jsonl"
    summary_path = reward_root / "final_agentic_reward_vlm_opus47" / "agentic_reward_summary.json"
    evaluation_dir = reward_root / "reward_c150_full497_vlm_opus47" / "evaluations"
    judge_models = sanitize(read_json(summary_path).get("judge_models") or {})
    rewards: dict[str, dict[str, Any]] = {}
    for source in read_jsonl(score_path):
        task_id = str(source["task_id"])
        detail_path = evaluation_dir / f"{task_id}.json"
        detail = sanitize(read_json(detail_path)) if detail_path.is_file() else None
        detail_json = canonical_json(detail) if detail is not None else None
        rewards[task_id] = {
            **sanitize(source),
            "_detail": detail,
            "_detail_path": str(detail_path) if detail_path.is_file() else None,
            "_detail_source_sha256": file_sha256(detail_path),
            "_detail_sha256": sha256_text(detail_json) if detail_json is not None else None,
            "_detail_size_bytes": len(detail_json.encode("utf-8"))
            if detail_json is not None
            else None,
            "_judge_models": judge_models,
        }
    return rewards


def empty_reward_fields(
    *, status: str, detail_status: str, version: str | None, batch_id: str | None
) -> dict[str, Any]:
    return {
        "reward_status": status,
        "reward_total": None,
        "reward_version": version,
        "quality_tier": None,
        "hard_filter_pass": None,
        "reward_record_id": None,
        "reward_wide_batch_id": batch_id,
        "reward_rubric_source": None,
        "reward_evaluation_valid": None,
        "reward_training_usable": None,
        "reward_system_error_items": None,
        "reward_cannot_determine_items": None,
        "reward_not_evaluated_items": None,
        "reward_total_items": None,
        "reward_requirement_compliance": None,
        "reward_substance_quality": None,
        "reward_presentation_design_quality": None,
        "reward_judge_models": None,
        "reward_detail_status": detail_status,
        "reward_detail_schema": None,
        "reward_detail_item_count": None,
        "reward_detail": None,
        "reward_detail_source_path": None,
        "reward_detail_source_sha256": None,
        "reward_detail_sha256": None,
        "reward_detail_size_bytes": None,
        "reward_detail_integrity_status": "not_applicable"
        if detail_status == "not_applicable"
        else "missing",
    }


def reward_fields(
    run_id: str,
    task_id: str,
    split: str,
    final_task: dict[str, Any],
    reward: dict[str, Any] | None,
) -> dict[str, Any]:
    selected_split = str(final_task.get("selected_source") or "rollout")
    if split != selected_split:
        return empty_reward_fields(
            status="not_selected_for_evaluation",
            detail_status="not_applicable",
            version=None,
            batch_id=None,
        )
    if reward is None:
        status = "preflight_failed" if final_task.get("usable") else "not_evaluated"
        return empty_reward_fields(
            status=status,
            detail_status="missing",
            version="task_specific_rubric_script_llm_vlm.v1",
            batch_id="agentic_reward_legacy_scale_20260803",
        )
    detail = reward.get("_detail") if isinstance(reward.get("_detail"), dict) else None
    detail_score = detail.get("reward_score") if detail else None
    aggregate_score = reward.get("reward_score")
    try:
        score_matches = abs(float(detail_score) - float(aggregate_score)) <= 1e-12
    except (TypeError, ValueError):
        score_matches = False
    domain_scores = detail.get("domain_scores") if detail else {}
    item_results = detail.get("item_results") if detail else []
    return {
        "reward_status": "complete",
        "reward_total": aggregate_score,
        "reward_version": "task_specific_rubric_script_llm_vlm.v1",
        "quality_tier": reward.get("quality_band"),
        "hard_filter_pass": None,
        "reward_record_id": f"reward_{sha256_text(f'{run_id}\n{task_id}')[:24]}",
        "reward_wide_batch_id": "agentic_reward_legacy_scale_20260803",
        "reward_rubric_source": reward.get("rubric_source"),
        "reward_evaluation_valid": reward.get("evaluation_valid"),
        "reward_training_usable": reward.get("training_usable"),
        "reward_system_error_items": reward.get("system_error_items"),
        "reward_cannot_determine_items": reward.get("cannot_determine_items"),
        "reward_not_evaluated_items": reward.get("not_evaluated_items"),
        "reward_total_items": reward.get("total_items"),
        "reward_requirement_compliance": domain_scores.get("Requirement Compliance"),
        "reward_substance_quality": domain_scores.get("Substance Quality"),
        "reward_presentation_design_quality": domain_scores.get("Presentation Design Quality"),
        "reward_judge_models": reward.get("_judge_models"),
        "reward_detail_status": "complete" if detail is not None else "missing",
        "reward_detail_schema": "ppt_agentic_reward_evaluation.v1" if detail is not None else None,
        "reward_detail_item_count": len(item_results) if isinstance(item_results, list) else None,
        "reward_detail": detail,
        "reward_detail_source_path": reward.get("_detail_path"),
        "reward_detail_source_sha256": reward.get("_detail_source_sha256"),
        "reward_detail_sha256": reward.get("_detail_sha256"),
        "reward_detail_size_bytes": reward.get("_detail_size_bytes"),
        "reward_detail_integrity_status": "pass"
        if detail is not None and score_matches
        else "score_mismatch"
        if detail is not None
        else "missing",
    }


def source_rows(run_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    selection = list(read_jsonl(run_root / "selection" / "selected_500.manifest.jsonl"))
    tasks = {
        str(row["task_id"]): row
        for row in read_jsonl(run_root / "selection" / "tasks.absolute_paths.jsonl")
    }
    return selection, tasks


def reference_assets(
    task: dict[str, Any], hash_cache: dict[str, str | None]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    references = [Path(str(path)) for path in task.get("reference_paths") or []]
    input_paths = list(references)
    for key in ("style_reference_pptx", "current_pptx"):
        if task.get(key):
            input_paths.append(Path(str(task[key])))
    reference_meta = [asset_metadata(path, hash_cache=hash_cache) for path in references]
    input_meta = [asset_metadata(path, hash_cache=hash_cache) for path in input_paths]
    return reference_meta, input_meta


def build_attempt(
    *,
    run_root: Path,
    logical_output_dir: Path,
    batch_id: str,
    built_at: str,
    run_metadata: dict[str, Any],
    selection: dict[str, Any],
    task: dict[str, Any],
    final_task: dict[str, Any],
    split: str,
    attempt_index: int,
    decode_map: dict[str, dict[str, Any]],
    occurrence_map: dict[tuple[str, str], list[dict[str, Any]]],
    eligible: set[str],
    response_statuses: dict[tuple[str, str], list[dict[str, Any]]],
    costs: dict[tuple[str, str], dict[str, Any]],
    rewards: dict[str, dict[str, Any]],
    store: AssetStore,
    hash_cache: dict[str, str | None],
    selection_manifest_sha: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = str(run_metadata["run_id"])
    task_id = str(task["task_id"])
    result_dir = run_root / split / "results" / task_id
    manifest_path = result_dir / "rollout_manifest.json"
    manifest = read_json(manifest_path)
    log_paths = sorted(result_dir.glob("claude_attempt*.log"))
    log_path = log_paths[-1] if log_paths else result_dir / "claude_attempt1.log"
    validation_errors: list[str] = []
    events, parse_counts = load_attempt_events(log_path)
    facts_before = trajectory_facts(events)
    decoded_in_trajectory, unresolved_in_trajectory = backfill_trajectory_thinking(
        events, decode_map
    )
    observations: list[dict[str, Any]] = []
    transformed_events: list[dict[str, Any]] = []
    for event_index, event in enumerate(events):
        transformed = externalize_images(
            event,
            store,
            observations=observations,
            pointer=f"/{event_index}",
            event_index=event_index,
        )
        transformed_events.append(sanitize(transformed))
    facts = trajectory_facts(transformed_events)
    decode_payload, decode_counts = build_decode_occurrences(
        occurrence_map.get((split, task_id), []),
        decode_map,
        eligible,
        facts_before["thinking_positions"],
        run_root
        / "postprocess"
        / "decode_opus47_valid_merged_final"
        / "decode_results_final.jsonl",
    )
    ccrelay = (manifest.get("rollout") or {}).get("ccrelay") or {}
    extracted_dir = Path(str(ccrelay.get("extracted_dir") or ""))
    trace_paths = sorted(extracted_dir.glob("*.extracted.jsonl"))
    trace_path = (
        trace_paths[0] if trace_paths else Path(str(ccrelay.get("raw_dir") or "")) / "cc-.jsonl"
    )
    trace, trace_facts, trace_errors = load_llm_trace(
        trace_path,
        response_statuses.get((split, task_id), []),
        decode_payload,
        store,
    )
    validation_errors.extend(trace_errors)
    if parse_counts.get("invalid_json_lines"):
        validation_errors.append("trajectory_invalid_json_lines")
    call_ids = set(facts["tool_call_ids"])
    result_ids = set(facts["tool_result_ids"])
    missing_results = sorted(call_ids - result_ids)
    unknown_results = sorted(result_ids - call_ids)
    if missing_results or unknown_results:
        validation_errors.append("tool_call_result_mismatch")
    trajectory_json = json_text(transformed_events)
    llm_trace_json = json_text(trace)
    raw_manifest = sanitize(manifest)
    query_path = Path(str(task["query_path"]))
    query_raw = query_path.read_text(encoding="utf-8", errors="replace").strip()
    query = redact_string(query_raw)
    reference_meta, input_meta = reference_assets(task, hash_cache)
    paths = manifest.get("paths") or {}
    pptx_path = Path(str(paths.get("slides_pptx") or result_dir / "slides.pptx"))
    pdf_path = Path(str(paths.get("slides_pdf") or result_dir / "rendered" / "slides.pdf"))
    rendered_dir = Path(str(paths.get("rendered_dir") or result_dir / "rendered"))
    slides = rendered_slide_metadata(rendered_dir, hash_cache)
    pptx_exists = pptx_path.is_file()
    pdf_exists = pdf_path.is_file()
    pptx_meta = asset_metadata(pptx_path, hash_cache=hash_cache) if pptx_exists else None
    pdf_meta = asset_metadata(pdf_path, hash_cache=hash_cache) if pdf_exists else None
    if pptx_exists and pdf_exists:
        artifact_status = "complete"
    elif pptx_exists:
        artifact_status = "pptx_only"
    else:
        artifact_status = "missing"
    status = str(manifest.get("status") or "failed")
    if status == "success" and not pptx_exists:
        validation_errors.append("success_without_pptx")
    init = facts.get("init") or {}
    result_event = facts.get("result") or {}
    trajectory_id = stable_trajectory_id(run_id, task_id, attempt_index)
    retry_of = stable_trajectory_id(run_id, task_id, 1) if attempt_index > 1 else None
    source_path_info = {
        "trajectory_log": str(log_path),
        "trajectory_log_sha256": parse_counts["source_sha256"],
        "llm_trace_extracted": str(trace_path),
        "llm_trace_extracted_sha256": trace_facts["source_sha256"],
        "rollout_manifest": str(manifest_path),
        "query_path": str(query_path),
        "rubric_input_path": task.get("rubric_input_path"),
    }
    first_request = (
        trace[0].get("request") if trace and isinstance(trace[0].get("request"), dict) else {}
    )
    request_system = first_request.get("system") if isinstance(first_request, dict) else None
    request_tools = first_request.get("tools") if isinstance(first_request, dict) else None
    observed_models = {
        key: value for key, value in trace_facts["response_models"].items() if key != "<missing>"
    }
    requested_model = str(
        (manifest.get("rollout") or {}).get("model_name") or run_metadata.get("model_requested")
    )
    cost = normalized_cost(
        costs.get((split, task_id)), int(trace_facts["usage"].get("thinking_tokens") or 0)
    )
    reward_data = reward_fields(run_id, task_id, split, final_task, rewards.get(task_id))
    if reward_data["reward_status"] == "complete":
        if reward_data["reward_detail_status"] != "complete":
            validation_errors.append("reward_detail_missing")
        elif reward_data["reward_detail_integrity_status"] != "pass":
            validation_errors.append("reward_detail_score_mismatch")
    selected_final = split == str(final_task.get("selected_source") or "rollout")
    error_value = manifest.get("error")
    if error_value is None and manifest.get("attempts"):
        error_value = (manifest.get("attempts") or [{}])[-1].get("error")
    error_detail = sanitize(error_value) if error_value is not None else None
    error_message = None
    if error_value is not None:
        error_message = redact_string(
            error_value if isinstance(error_value, str) else canonical_json(error_value)
        )[:2000]
    task_axes = task.get("task_axes") or {}
    task_type = "_".join(
        str(task_axes.get(key) or "unknown") for key in ("content_mode", "interaction_mode")
    )
    output_files = []
    if pptx_meta:
        output_files.append({"name": "pptx", **pptx_meta})
    if pdf_meta:
        output_files.append({"name": "pdf", **pdf_meta})
    task_metadata = {
        "selection": {
            key: value
            for key, value in selection.items()
            if key
            not in {
                "source_ppt",
            }
        },
        "source_paths": source_path_info,
        "final_task_selection": {
            "selected_source": final_task.get("selected_source"),
            "usable": final_task.get("usable"),
            "is_selected_attempt": selected_final,
        },
        "trajectory_parse": parse_counts,
        "tool_binding": {
            "status": "complete" if not missing_results and not unknown_results else "mismatch",
            "missing_result_ids": missing_results,
            "unknown_result_ids": unknown_results,
        },
        "thinking_backfill": {
            "decoded_agent_blocks": decoded_in_trajectory,
            "unresolved_agent_blocks": unresolved_in_trajectory,
        },
    }
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_revision": 1,
        "trajectory_id": trajectory_id,
        "domain": "ppt",
        "run_id": run_id,
        "task_id": task_id,
        "attempt_index": attempt_index,
        "attempt_id": str(
            (manifest.get("rollout") or {}).get("pod_name") or init.get("session_id") or ""
        ),
        "retry_of_trajectory_id": retry_of,
        "source_dataset_name": Path(str(run_metadata.get("source_run") or "unknown")).name,
        "source_batch_id": selection.get("batch"),
        "source_record_id": task_id,
        "source_line_no": selection.get("selection_index"),
        "source_manifest_path": str(run_root / "selection" / "selected_500.manifest.jsonl"),
        "source_manifest_sha256": selection_manifest_sha,
        "created_by": None,
        "batch_id": batch_id,
        "batch_ingested_at": built_at,
        "query": query,
        "query_sha256": sha256_text(query_raw),
        "task_type": task_type,
        "operation_type": task_axes.get("interaction_mode"),
        "task_axes": task_axes,
        "reference_files": reference_meta,
        "input_assets": input_meta,
        "is_multimodal": bool(observations),
        "input_image_count": len(observations),
        "task_metadata": task_metadata,
        "rollout_model_requested": requested_model,
        "rollout_model_observed": requested_model
        if requested_model in observed_models
        else next(iter(observed_models), None),
        "provider": "huayanapi",
        "agent_name": "claude-code",
        "agent_version": init.get("claude_code_version"),
        "runtime_image": (manifest.get("rollout") or {}).get("image"),
        "skill_refs": [
            {
                "name": name,
                "ref": (manifest.get("rollout") or {}).get("anthropic_skills_ref"),
            }
            for name in ("pptx", "pdf-converter")
        ],
        "tool_schema_sha256": sha256_text(canonical_json(request_tools))
        if request_tools is not None
        else None,
        "system_prompt_sha256": sha256_text(canonical_json(request_system))
        if request_system is not None
        else None,
        "thinking_enabled": bool(first_request.get("thinking"))
        if isinstance(first_request, dict)
        else bool(decode_counts["total"]),
        "max_output_tokens": max(
            (int((item.get("request") or {}).get("max_tokens") or 0) for item in trace),
            default=0,
        ),
        "runtime_config": {
            "concurrency": run_metadata.get("concurrency"),
            "one_task_per_pod": run_metadata.get("one_task_per_pod"),
            "observed_response_models": observed_models,
            "ccrelay_enabled": True,
        },
        "pod_name": (manifest.get("rollout") or {}).get("pod_name"),
        "session_id": init.get("session_id"),
        "trajectory_format": "claude_stream_json.v1",
        "trajectory": trajectory_json,
        "trajectory_path": f"{logical_output_dir / 'data.jsonl'}#trajectory_id={trajectory_id}",
        "trajectory_sha256": sha256_text(trajectory_json),
        "source_trajectory_sha256": source_path_info["trajectory_log_sha256"],
        "trajectory_transformations": [
            {"type": "credential_redaction", "version": "v1"},
            {"type": "image_externalization", "version": "v1"},
            {"type": "thinking_decode_backfill", "version": "v1"},
        ],
        "trajectory_size_bytes": len(trajectory_json.encode("utf-8")),
        "trajectory_event_count": len(transformed_events),
        "trajectory_parse_status": "ok"
        if not parse_counts.get("invalid_json_lines")
        else "partial",
        "llm_trace_format": "anthropic_ccrelay_request_response.v1+signature_backfill",
        "llm_trace": llm_trace_json,
        "llm_trace_path": f"{logical_output_dir / 'data.jsonl'}#trajectory_id={trajectory_id}",
        "llm_trace_sha256": sha256_text(llm_trace_json),
        "llm_trace_size_bytes": len(llm_trace_json.encode("utf-8")),
        "llm_request_count": trace_facts["request_count"],
        "llm_response_count": trace_facts["response_count"],
        "tool_call_count": len(facts["tool_call_ids"]),
        "tool_result_count": len(facts["tool_result_ids"]),
        "trajectory_image_count": len(observations),
        "trajectory_image_placeholder_count": len(observations),
        "trajectory_images": observations,
        "image_binding_status": "complete" if observations else "not_applicable",
        "raw_manifest": raw_manifest,
        "has_thinking": bool(decode_counts["total"] or facts["thinking_positions"]),
        "thinking_block_count": sum(len(items) for items in facts["thinking_positions"].values()),
        "signature_occurrence_count": decode_counts["total"],
        "unique_signature_count": len(
            {item.get("signature_sha256") for item in decode_payload["occurrences"]}
        ),
        "thinking_decode_status": decode_payload["status"],
        "decoded_thinking_count": int(decode_counts.get("success") or 0),
        "unresolved_signature_count": int(
            decode_counts.get("failed", 0) + decode_counts.get("not_attempted", 0)
        ),
        "thinking_decode": decode_payload,
        "status": status,
        "started_at": manifest.get("started_at"),
        "ended_at": manifest.get("ended_at"),
        "duration_ms": round(float(manifest.get("duration_sec") or 0) * 1000),
        "terminal_reason": result_event.get("terminal_reason"),
        "stop_reason": trace_facts.get("stop_reason") or facts.get("stop_reason"),
        "error_type": "rollout_failed" if status != "success" else None,
        "error_message": error_message,
        "error_detail": error_detail,
        "risk_flags": sorted(
            set(
                str(item)
                for item in (task.get("risk_flags") or []) + (manifest.get("risk_flags") or [])
            )
        ),
        "artifact_status": artifact_status,
        "pptx_path": str(pptx_path) if pptx_exists else None,
        "pptx_sha256": pptx_meta.get("sha256") if pptx_meta else None,
        "pptx_size_bytes": pptx_path.stat().st_size if pptx_exists else None,
        "slide_count": len(slides) or None,
        "pdf_path": str(pdf_path) if pdf_exists else None,
        "pdf_sha256": pdf_meta.get("sha256") if pdf_meta else None,
        "rendered_dir": str(rendered_dir) if rendered_dir.is_dir() else None,
        "rendered_slides": slides,
        "output_files": output_files,
        "source_pptx_path": task.get("current_pptx"),
        "source_pptx_sha256": (
            file_sha256(Path(str(task["current_pptx"]))) if task.get("current_pptx") else None
        ),
        "input_tokens": cost["input_tokens"],
        "cache_creation_input_tokens": cost["cache_creation_input_tokens"],
        "cache_read_input_tokens": cost["cache_read_input_tokens"],
        "output_tokens": cost["output_tokens"],
        "thinking_tokens": cost["thinking_tokens"],
        "usage_detail": cost["usage_detail"],
        "cost_usd": cost["cost_usd"],
        "cost_cny": cost["cost_cny"],
        "usd_cny_rate": 6.7928,
        "pricing_version": "logged_model_usage_20260803",
        "cache_cost_included": True,
        "cost_audit_status": cost["cost_audit_status"],
        **reward_data,
        "sanitization_version": "ppt_trajectory_redaction.v1",
        "sanitized_at": built_at,
        "secret_scan_status": "pending",
        "secret_scan_hit_count": 0,
        "integrity_status": "pending",
        "record_sha256": None,
        "validation_errors": validation_errors,
    }
    serialized: dict[str, Any] = {}
    for key, value in row.items():
        if key in PRE_SERIALIZED_JSON_COLUMNS and value is not None:
            serialized[key] = value
        elif key in JSON_COLUMNS and value is not None:
            serialized[key] = json_text(value)
        else:
            serialized[key] = value
    hits = sorted(
        {
            hit
            for value in serialized.values()
            if isinstance(value, str)
            for hit in secret_hits(value)
        }
    )
    serialized["secret_scan_status"] = "pass" if not hits else "fail"
    serialized["secret_scan_hit_count"] = len(hits)
    if hits:
        validation_errors.append("secret_scan_failed:" + ",".join(hits))
    fatal_errors = [
        error
        for error in validation_errors
        if error.startswith("secret_scan_failed")
        or error.startswith("signature_request")
        or error.startswith("signature_content")
        or error.startswith("signature_target")
        or error.startswith("reward_detail")
        or error in {"trajectory_invalid_json_lines", "success_without_pptx"}
    ]
    serialized["validation_errors"] = json_text(validation_errors)
    serialized["integrity_status"] = "pass" if not fatal_errors else "fail"
    record_for_hash = {
        key: value
        for key, value in serialized.items()
        if key not in {"batch_id", "batch_ingested_at", "record_sha256"}
    }
    serialized["record_sha256"] = stable_record_hash(record_for_hash)
    preview = {
        "trajectory_id": trajectory_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_index": attempt_index,
        "source_batch_id": serialized["source_batch_id"],
        "task_type": task_type,
        "query": query,
        "status": status,
        "artifact_status": artifact_status,
        "slide_count": serialized["slide_count"],
        "rollout_model": serialized["rollout_model_observed"],
        "is_multimodal": serialized["is_multimodal"],
        "trajectory_event_count": serialized["trajectory_event_count"],
        "llm_request_count": serialized["llm_request_count"],
        "tool_call_count": serialized["tool_call_count"],
        "image_count": serialized["trajectory_image_count"],
        "signature_count": serialized["signature_occurrence_count"],
        "decoded_thinking_count": serialized["decoded_thinking_count"],
        "thinking_decode_status": serialized["thinking_decode_status"],
        "reward_status": serialized["reward_status"],
        "reward_total": serialized["reward_total"],
        "reward_evaluation_valid": serialized["reward_evaluation_valid"],
        "reward_training_usable": serialized["reward_training_usable"],
        "reward_detail_status": serialized["reward_detail_status"],
        "reward_detail_item_count": serialized["reward_detail_item_count"],
        "reward_detail_source_sha256": serialized["reward_detail_source_sha256"],
        "reward_detail_sha256": serialized["reward_detail_sha256"],
        "reward_detail_integrity_status": serialized["reward_detail_integrity_status"],
        "cost_usd": serialized["cost_usd"],
        "duration_ms": serialized["duration_ms"],
        "integrity_status": serialized["integrity_status"],
        "secret_scan_status": serialized["secret_scan_status"],
        "secret_scan_hit_count": serialized["secret_scan_hit_count"],
        "trajectory_size_bytes": serialized["trajectory_size_bytes"],
        "llm_trace_size_bytes": serialized["llm_trace_size_bytes"],
        "decode_failed_count": int(decode_counts.get("failed") or 0),
        "decode_not_attempted_count": int(decode_counts.get("not_attempted") or 0),
        "validation_error_count": len(validation_errors),
    }
    return serialized, preview


def schema_registry(row: dict[str, Any]) -> dict[str, Any]:
    bigint = {
        "record_revision",
        "attempt_index",
        "source_line_no",
        "input_image_count",
        "max_output_tokens",
        "trajectory_size_bytes",
        "trajectory_event_count",
        "llm_trace_size_bytes",
        "llm_request_count",
        "llm_response_count",
        "tool_call_count",
        "tool_result_count",
        "trajectory_image_count",
        "trajectory_image_placeholder_count",
        "thinking_block_count",
        "signature_occurrence_count",
        "unique_signature_count",
        "decoded_thinking_count",
        "unresolved_signature_count",
        "duration_ms",
        "pptx_size_bytes",
        "slide_count",
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "thinking_tokens",
        "reward_system_error_items",
        "reward_cannot_determine_items",
        "reward_not_evaluated_items",
        "reward_total_items",
        "reward_detail_item_count",
        "reward_detail_size_bytes",
        "secret_scan_hit_count",
    }
    doubles = {
        "cost_usd",
        "cost_cny",
        "usd_cny_rate",
        "reward_total",
        "reward_requirement_compliance",
        "reward_substance_quality",
        "reward_presentation_design_quality",
    }
    booleans = {
        "is_multimodal",
        "thinking_enabled",
        "cache_cost_included",
        "hard_filter_pass",
        "reward_evaluation_valid",
        "reward_training_usable",
    }
    timestamps = {"batch_ingested_at", "started_at", "ended_at", "sanitized_at"}
    fields = []
    for name in row:
        if name in JSON_COLUMNS:
            semantic = "JSON(VARCHAR)"
        elif name in bigint:
            semantic = "BIGINT"
        elif name in doubles:
            semantic = "DOUBLE"
        elif name in booleans:
            semantic = "BOOLEAN"
        elif name in timestamps:
            semantic = "TIMESTAMP"
        else:
            semantic = "VARCHAR"
        fields.append(
            {
                "name": name,
                "semantic_type": semantic,
                "warehouse_view_type": "VARCHAR",
                "introduced_in": SCHEMA_VERSION,
                "deprecated_in": None,
                "replaced_by": None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "producer_version": PRODUCER_VERSION,
        "table_key": "ppt_trajectory",
        "display_name": "PPT trajectory wide table",
        "unique_key": ["run_id", "task_id", "attempt_index"],
        "dedup_order_by": "TRY_CAST(record_revision AS BIGINT) DESC, batch_ingested_at DESC",
        "fields": fields,
    }


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_parquet(jsonl_path: Path, parquet_path: Path, output_dir: Path) -> None:
    import duckdb

    connection = duckdb.connect()
    connection.execute("SET threads=8")
    connection.execute("SET memory_limit='64GB'")
    connection.execute(f"SET temp_directory={sql_quote(str(output_dir / 'duckdb_tmp'))}")
    query = (
        "COPY (SELECT * FROM read_json_auto("
        f"{sql_quote(str(jsonl_path))}, format='newline_delimited', sample_size=-1, "
        "maximum_object_size=1073741824, union_by_name=true)) "
        f"TO {sql_quote(str(parquet_path))} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1)"
    )
    connection.execute(query)
    connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--logical-output-dir", type=Path)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--built-at")
    parser.add_argument("--task-offset", type=int, default=0)
    parser.add_argument("--limit-tasks", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--skip-parquet", action="store_true")
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else run_root / "postprocess" / DEFAULT_OUTPUT_NAME
    )
    logical_output_dir = (
        args.logical_output_dir.resolve() if args.logical_output_dir else output_dir
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    built_at = args.built_at or utc_now()
    run_metadata = read_json(run_root / "run_metadata.json")
    selection, tasks = source_rows(run_root)
    selection = selection[max(args.task_offset, 0) :]
    if args.limit_tasks is not None:
        selection = selection[: max(args.limit_tasks, 0)]
    final_tasks = {
        str(row["task_id"]): row
        for row in read_jsonl(
            run_root / "postprocess" / "final_task_manifest" / "final_task_manifest.jsonl"
        )
    }
    decode_map, occurrence_map, eligible = load_decode_state(run_root)
    response_statuses = load_response_statuses(run_root)
    costs = load_costs(run_root)
    rewards = load_rewards(run_root)
    store = AssetStore(output_dir)
    selection_manifest_sha = str(
        file_sha256(run_root / "selection" / "selected_500.manifest.jsonl")
    )
    jobs: list[dict[str, Any]] = []
    for task_number, selected in enumerate(selection, 1):
        task_id = str(selected["task_id"])
        attempts = [("rollout", 1)]
        retry_manifest = (
            run_root / "retry_missing_8" / "results" / task_id / "rollout_manifest.json"
        )
        if retry_manifest.is_file():
            attempts.append(("retry_missing_8", 2))
        for split, attempt_index in attempts:
            jobs.append(
                {
                    "task_number": task_number,
                    "selected": selected,
                    "task_id": task_id,
                    "split": split,
                    "attempt_index": attempt_index,
                }
            )

    def process_job(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        task_id = str(job["task_id"])
        return build_attempt(
            run_root=run_root,
            logical_output_dir=logical_output_dir,
            batch_id=args.batch_id,
            built_at=built_at,
            run_metadata=run_metadata,
            selection=job["selected"],
            task=tasks[task_id],
            final_task=final_tasks[task_id],
            split=str(job["split"]),
            attempt_index=int(job["attempt_index"]),
            decode_map=decode_map,
            occurrence_map=occurrence_map,
            eligible=eligible,
            response_statuses=response_statuses,
            costs=costs,
            rewards=rewards,
            store=store,
            hash_cache={},
            selection_manifest_sha=selection_manifest_sha,
        )

    data_path = output_dir / "data.jsonl"
    preview_path = output_dir / "rows_preview.jsonl"
    index_path = output_dir / "row_index.jsonl"
    data_temp = data_path.with_suffix(".jsonl.tmp")
    preview_temp = preview_path.with_suffix(".jsonl.tmp")
    index_temp = index_path.with_suffix(".jsonl.tmp")
    summaries: list[dict[str, Any]] = []
    first_row: dict[str, Any] | None = None
    offset = 0
    rows_written = 0
    worker_count = max(1, int(args.workers))
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool,
        data_temp.open("wb") as data_out,
        preview_temp.open("w", encoding="utf-8") as preview_out,
        index_temp.open("w", encoding="utf-8") as index_out,
    ):
        window = max(worker_count * 2, 1)
        pending: dict[int, concurrent.futures.Future] = {}
        next_submit = 0
        while next_submit < min(window, len(jobs)):
            pending[next_submit] = pool.submit(process_job, jobs[next_submit])
            next_submit += 1
        for job_index, job in enumerate(jobs):
            future = pending.pop(job_index)
            row, preview = future.result()
            while next_submit < len(jobs) and len(pending) < window:
                pending[next_submit] = pool.submit(process_job, jobs[next_submit])
                next_submit += 1
            if first_row is None:
                first_row = row
            line = (json_text(row) + "\n").encode("utf-8")
            data_out.write(line)
            preview_out.write(json_text(preview) + "\n")
            index_out.write(
                json_text(
                    {
                        "row_number": rows_written + 1,
                        "trajectory_id": row["trajectory_id"],
                        "task_id": job["task_id"],
                        "attempt_index": job["attempt_index"],
                        "offset": offset,
                        "length": len(line),
                    }
                )
                + "\n"
            )
            offset += len(line)
            rows_written += 1
            summaries.append(preview)
            print(
                f"[{rows_written}/{len(jobs)}; task {job['task_number']}/{len(selection)}] "
                f"{job['task_id']} attempt={job['attempt_index']} "
                f"images={store.occurrence_count} unique_images={len(store.unique)}",
                flush=True,
            )
    os.replace(data_temp, data_path)
    os.replace(preview_temp, preview_path)
    os.replace(index_temp, index_path)
    if first_row is None:
        raise RuntimeError("no rows written")
    keys = [(row["run_id"], row["task_id"], row["attempt_index"]) for row in summaries]
    status_counts = collections.Counter(row["status"] for row in summaries)
    decode_status_counts = collections.Counter(row["thinking_decode_status"] for row in summaries)
    reward_status_counts = collections.Counter(row["reward_status"] for row in summaries)
    reward_complete_count = int(reward_status_counts.get("complete") or 0)
    reward_detail_inline_count = sum(row["reward_detail_status"] == "complete" for row in summaries)
    reward_detail_item_count = sum(int(row["reward_detail_item_count"] or 0) for row in summaries)
    reward_detail_source_hash_count = sum(
        bool(row["reward_detail_source_sha256"]) for row in summaries
    )
    reward_detail_hash_count = sum(bool(row["reward_detail_sha256"]) for row in summaries)
    reward_detail_integrity_pass_count = sum(
        row["reward_detail_integrity_status"] == "pass" for row in summaries
    )
    llm_request_count = sum(int(row["llm_request_count"] or 0) for row in summaries)
    signature_count = sum(int(row["signature_count"] or 0) for row in summaries)
    decoded_count = sum(int(row["decoded_thinking_count"] or 0) for row in summaries)
    decode_failed_count = sum(int(row["decode_failed_count"] or 0) for row in summaries)
    decode_not_attempted_count = sum(
        int(row["decode_not_attempted_count"] or 0) for row in summaries
    )
    integrity_fail_count = sum(row["integrity_status"] == "fail" for row in summaries)
    secret_scan_fail_count = sum(row["secret_scan_status"] == "fail" for row in summaries)
    validation = {
        "schema": "ppt_trajectory_canary_validation.v1",
        "producer_version": PRODUCER_VERSION,
        "created_at": built_at,
        "expected": {
            "task_count": 500 if args.limit_tasks is None else len(selection),
            "attempt_count": 508 if args.limit_tasks is None else None,
            "llm_response_count": 21146 if args.limit_tasks is None else None,
            "signature_occurrence_count": 2778 if args.limit_tasks is None else None,
            "eligible_signature_count": 2078 if args.limit_tasks is None else None,
            "decoded_signature_count": 2036 if args.limit_tasks is None else None,
            "reward_complete_count": 496 if args.limit_tasks is None else None,
            "reward_detail_item_count": 12213 if args.limit_tasks is None else None,
        },
        "actual": {
            "row_count": rows_written,
            "task_count": len({row["task_id"] for row in summaries}),
            "retry_row_count": sum(row["attempt_index"] == 2 for row in summaries),
            "unique_key_count": len(set(keys)),
            "status_counts": dict(status_counts),
            "artifact_complete_count": sum(
                row["artifact_status"] == "complete" for row in summaries
            ),
            "trajectory_inline_count": rows_written,
            "llm_trace_inline_count": rows_written,
            "llm_request_count": llm_request_count,
            "signature_occurrence_count": signature_count,
            "eligible_signature_count": decoded_count + decode_failed_count,
            "decoded_signature_count": decoded_count,
            "decode_failed_count": decode_failed_count,
            "decode_not_attempted_count": decode_not_attempted_count,
            "decode_status_counts": dict(decode_status_counts),
            "multimodal_row_count": sum(bool(row["is_multimodal"]) for row in summaries),
            "trajectory_image_occurrence_count": sum(
                int(row["image_count"] or 0) for row in summaries
            ),
            "unique_externalized_image_count": len(store.unique),
            "image_base64_decode_error_count": store.decode_errors,
            "reward_status_counts": dict(reward_status_counts),
            "reward_complete_count": reward_complete_count,
            "reward_detail_inline_count": reward_detail_inline_count,
            "reward_detail_item_count": reward_detail_item_count,
            "reward_detail_source_hash_count": reward_detail_source_hash_count,
            "reward_detail_hash_count": reward_detail_hash_count,
            "reward_detail_integrity_pass_count": reward_detail_integrity_pass_count,
            "secret_scan_fail_count": secret_scan_fail_count,
            "integrity_fail_count": integrity_fail_count,
        },
        "checks": {
            "unique_business_keys": len(set(keys)) == rows_written,
            "all_rows_inline": all(
                int(row["trajectory_size_bytes"] or 0) > 2
                and int(row["llm_trace_size_bytes"] or 0) > 2
                for row in summaries
            ),
            "all_secret_scans_pass": secret_scan_fail_count == 0,
            "all_integrity_checks_pass": integrity_fail_count == 0,
            "expected_attempt_count": args.limit_tasks is not None or rows_written == 508,
            "expected_llm_response_count": args.limit_tasks is not None
            or llm_request_count == 21146,
            "expected_signature_count": args.limit_tasks is not None or signature_count == 2778,
            "expected_eligible_signature_count": args.limit_tasks is not None
            or decoded_count + decode_failed_count == 2078,
            "expected_decoded_count": args.limit_tasks is not None or decoded_count == 2036,
            "expected_failed_decode_count": args.limit_tasks is not None
            or decode_failed_count == 42,
            "expected_not_attempted_count": args.limit_tasks is not None
            or decode_not_attempted_count == 700,
            "all_completed_rewards_have_inline_detail": reward_detail_inline_count
            == reward_complete_count,
            "all_reward_details_have_source_hash": reward_detail_source_hash_count
            == reward_complete_count,
            "all_reward_details_have_content_hash": reward_detail_hash_count
            == reward_complete_count,
            "all_reward_detail_scores_match": reward_detail_integrity_pass_count
            == reward_complete_count,
            "expected_reward_complete_count": args.limit_tasks is not None
            or reward_complete_count == 496,
            "expected_reward_detail_item_count": args.limit_tasks is not None
            or reward_detail_item_count == 12213,
        },
        "files": {
            "data_jsonl": str(data_path),
            "rows_preview": str(preview_path),
            "row_index": str(index_path),
            "image_assets": str(store.image_dir),
        },
    }
    write_json(output_dir / "schema_registry.json", schema_registry(first_row))
    write_json(output_dir / "validation_report.json", validation)
    write_json(
        output_dir / "batch.json",
        {
            "batch_id": args.batch_id,
            "table_key": "ppt_trajectory",
            "status": "mock_canary_not_registered",
            "created_at": built_at,
            "producer_version": PRODUCER_VERSION,
            "source_run": str(run_root),
            "row_count": rows_written,
            "task_count": len(selection),
            "notes": [
                "trajectory and llm_trace are both permanently inline",
                (
                    "complete Reward evaluations and all item_results are permanently "
                    "inline in reward_detail"
                ),
                "reward_record_id is retained only as a stable trace key",
                "raw judge API logs and prompts remain in the controlled source sidecar",
                "failed decoder raw responses remain in the controlled source sidecar",
                "this canary is not registered in the formal Warehouse",
            ],
            "files": validation["files"],
        },
    )
    if not args.skip_parquet:
        parquet_path = output_dir / "data.parquet"
        write_parquet(data_path, parquet_path, output_dir)
        validation["files"]["data_parquet"] = str(parquet_path)
        validation["actual"]["data_parquet_size_bytes"] = parquet_path.stat().st_size
        write_json(output_dir / "validation_report.json", validation)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if all(validation["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
