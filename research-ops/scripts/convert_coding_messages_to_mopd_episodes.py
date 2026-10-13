#!/usr/bin/env python3
"""Convert canonical ``coding_messages_all`` files into compact turn episodes.

Each output line is one application episode.  ``initial_messages`` plus the
ordered ``turns`` reconstructs the complete multi-turn context without
copying an ever-growing prompt into every turn.  The converter records the
remaining MOPD requirements explicitly: canonical messages contain the
sampled actions, but not token ids, behavior-policy log-probabilities, or a
reward.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
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
BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,\"']+")
SIGNED_URL_RE = re.compile(
    r"(?i)(https?://[^\s\"']*[?&](?:authorization|signature|token)=[^\s\"']+)"
)


def _redact(value: Any, *, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret")
    ):
        return "<redacted>"
    if isinstance(value, str):
        value = BEARER_RE.sub("Bearer <redacted>", value)
        return SIGNED_URL_RE.sub("<signed-url-redacted>", value)
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _history_flag(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("isHistoryMessage") is True:
            return True
        return any(_history_flag(item) for item in value.values())
    if isinstance(value, list):
        return any(_history_flag(item) for item in value)
    return False


def _messages(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    request = record.get("llm_request_body")
    request = request if isinstance(request, Mapping) else {}
    params = request.get("call_llm_params")
    params = params if isinstance(params, Mapping) else {}
    raw = params.get("messages")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _response(record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None, Any]:
    body = record.get("llm_response_body")
    body = body if isinstance(body, Mapping) else {}
    responses = body.get("responses")
    if not isinstance(responses, list) or not responses:
        responses = body.get("final_responses")
    if not isinstance(responses, list) or not responses:
        return None, None, None
    response = responses[-1]
    if not isinstance(response, Mapping):
        return None, None, None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None, None, response.get("usage")
    choice = choices[0]
    message = choice.get("message")
    return (
        dict(message) if isinstance(message, Mapping) else None,
        str(choice.get("finish_reason") or "") or None,
        response.get("usage"),
    )


def _trajectory_record(raw: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("trajectory must be a non-empty list")
    candidates = [
        (index, item)
        for index, item in enumerate(raw)
        if isinstance(item, Mapping) and _messages(item)
    ]
    if not candidates:
        raise ValueError("trajectory has no LLM request messages")
    index, record = max(candidates, key=lambda pair: len(_messages(pair[1])))
    return dict(record), index


def _result_rows(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        import pandas as pd
    except ImportError as exc:  # optional dependency, only needed for xlsx enrichment
        raise RuntimeError("pandas is required when --result-xlsx is used") from exc
    frame = pd.read_excel(path)
    if "app_id" not in frame.columns:
        raise ValueError(f"result xlsx has no app_id column: {path}")
    rows: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        app_id = row.get("app_id")
        if pd.isna(app_id) or not str(app_id).strip():
            continue
        rows[str(app_id).strip()] = {
            "case_id": _row_value(row, "id"),
            "query": _row_value(row, "query"),
            "status": _row_value(row, "状态"),
            "app_type": _row_value(row, "应用类型"),
            "duration_seconds": _row_value(row, "端到端用时(秒)"),
        }
    return rows


def _row_value(row: Any, name: str) -> Any:
    import pandas as pd

    item = row.get(name)
    return None if pd.isna(item) else item


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _turns(
    messages: list[dict[str, Any]],
    final_action: dict[str, Any] | None,
    *,
    finish_reason: str | None,
    usage: Any,
) -> tuple[list[dict[str, Any]], int]:
    assistant_indices = [
        index for index, message in enumerate(messages) if message.get("role") == "assistant"
    ]
    first_assistant = assistant_indices[0] if assistant_indices else len(messages)
    turns: list[dict[str, Any]] = []
    for position, message_index in enumerate(assistant_indices):
        next_index = (
            assistant_indices[position + 1]
            if position + 1 < len(assistant_indices)
            else len(messages)
        )
        observations = messages[message_index + 1 : next_index]
        turns.append(
            {
                "turn_index": len(turns),
                "prompt_message_count": message_index,
                "prompt_source": "reconstructed_prefix_of_captured_request",
                "action": _redact(messages[message_index]),
                "observations": _redact(observations),
                "terminal": False,
                "finish_reason": None,
                "usage": None,
            }
        )
    if final_action is not None:
        turns.append(
            {
                "turn_index": len(turns),
                "prompt_message_count": len(messages),
                "prompt_source": "captured_request_response",
                "action": _redact(final_action),
                "observations": [],
                "terminal": True,
                "finish_reason": finish_reason,
                "usage": _redact(usage),
            }
        )
    return turns, first_assistant


def convert_one(
    path: Path,
    *,
    batch_task_id: str | None,
    result: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    record, record_index = _trajectory_record(raw)
    raw_messages = _messages(record)
    history_removed = sum(_history_flag(message) for message in raw_messages)
    messages = [message for message in raw_messages if not _history_flag(message)]
    final_action, finish_reason, usage = _response(record)
    turns, first_assistant = _turns(
        messages,
        final_action,
        finish_reason=finish_reason,
        usage=usage,
    )
    request = record.get("llm_request_body")
    request = request if isinstance(request, Mapping) else {}
    params = request.get("call_llm_params")
    params = params if isinstance(params, Mapping) else {}
    app_id = path.stem
    result = result or {}
    query = str(result.get("query") or "")
    status = str(result.get("status") or "")
    episode = {
        "schema_version": "agentos-mopd-episode-v1",
        "episode_id": f"{batch_task_id or 'unbound'}:{app_id}",
        "batch_task_id": batch_task_id,
        "domain": "miaoda",
        "phase": "coding",
        "app_id": app_id,
        "case_id": result.get("case_id"),
        "query": query or None,
        "query_sha256": _sha256(query) if query else None,
        "app_type": result.get("app_type") or (record.get("ext") or {}).get("app_type"),
        "rollout_status": status or None,
        "duration_seconds": result.get("duration_seconds"),
        "model": record.get("model") or params.get("model"),
        "sampling": _redact(
            {
                key: params.get(key)
                for key in ("max_tokens", "temperature", "top_p", "thinking", "stream")
                if key in params
            }
        ),
        "initial_messages": _redact(messages[:first_assistant]),
        "turns": turns,
        "training": {
            "context_reconstructable": True,
            "captured_final_request_response": final_action is not None,
            "history_messages_removed": history_removed,
            "behavior_token_ids_available": False,
            "behavior_logprobs_available": False,
            "teacher_logprobs_available": False,
            "reward_available": False,
            "training_ready": False,
            "status": "needs_tokenization_logprob_replay_and_reward",
        },
        "source": {
            "trajectory_file": str(path.resolve()),
            "trajectory_records": len(raw) if isinstance(raw, list) else None,
            "selected_record_index": record_index,
        },
    }
    report = {
        "app_id": app_id,
        "rollout_status": status or None,
        "trajectory_records": len(raw) if isinstance(raw, list) else None,
        "selected_record_index": record_index,
        "raw_messages": len(raw_messages),
        "history_messages_removed": history_removed,
        "current_messages": len(messages),
        "turns": len(turns),
        "final_response_present": final_action is not None,
        "behavior_logprobs_available": False,
        "reward_available": False,
    }
    return episode, report


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--result-xlsx", type=Path)
    parser.add_argument("--batch-task-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    paths = sorted(Path(item) for item in glob.glob(str(args.input_dir / "*.json")))
    if not paths:
        parser.error(f"no JSON trajectories found in {args.input_dir}")
    result_rows = _result_rows(args.result_xlsx)
    episodes: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for path in paths:
        try:
            episode, report = convert_one(
                path,
                batch_task_id=args.batch_task_id,
                result=result_rows.get(path.stem),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            reports.append(
                {"app_id": path.stem, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        episodes.append(episode)
        reports.append(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        for episode in episodes:
            stream.write(json.dumps(episode, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temporary, args.output)
    summary = {
        "schema_version": "agentos-mopd-episode-v1",
        "batch_task_id": args.batch_task_id,
        "trajectory_files": len(paths),
        "episodes": len(episodes),
        "failed_conversions": sum("error" in item for item in reports),
        "turns": sum(len(item["turns"]) for item in episodes),
        "history_messages_removed": sum(
            item["training"]["history_messages_removed"] for item in episodes
        ),
        "training_ready_episodes": sum(
            bool(item["training"]["training_ready"]) for item in episodes
        ),
        "rollout_status_counts": dict(
            Counter(str(item.get("rollout_status") or "unknown") for item in episodes)
        ),
        "duration_seconds": _duration_summary(episodes),
        "status": "needs_tokenization_logprob_replay_and_reward",
        "output": str(args.output.resolve()),
        "reports": reports,
    }
    _write_json(args.report, summary)
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "episodes",
                    "turns",
                    "history_messages_removed",
                    "training_ready_episodes",
                    "status",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if not summary["failed_conversions"] else 1


def _duration_summary(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    for episode in episodes:
        try:
            values.append(float(episode["duration_seconds"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "mean": sum(values) / len(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values) if values else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
