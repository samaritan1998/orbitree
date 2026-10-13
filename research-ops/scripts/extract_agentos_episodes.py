#!/usr/bin/env python3
"""Convert AgentOS rollout artifacts into history-free environment episodes.

The A2A trajectory contains tool actions and observations, but usually not the
exact model request/token log-probabilities.  The output records that fact
explicitly instead of pretending the artifacts are token-level MOPD samples.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "jwt",
    "secret",
    "token",
    "x-authorization",
}
SIGNED_URL_RE = re.compile(r"(?i)(https?://[^\s\"']*(?:authorization|signature|token)=[^\s\"']+)")
BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,\"']+")


def _redact_text(value: str) -> str:
    value = BEARER_RE.sub("Bearer <redacted>", value)
    return SIGNED_URL_RE.sub("<signed-url-redacted>", value)


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold()
    if lowered in SENSITIVE_KEYS or lowered.endswith("_key"):
        return "<redacted>"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _events(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"rollout artifact is not an object: {path}")
    return payload


def _result(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("result")
    return value if isinstance(value, dict) else {}


def _artifact(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("artifact")
    return value if isinstance(value, dict) else {}


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("metadata")
    return value if isinstance(value, dict) else {}


def _event_id(result: dict[str, Any]) -> str | None:
    value = _metadata(result).get("eventId")
    return str(value) if value is not None else None


def _category(result: dict[str, Any]) -> str | None:
    metadata = _metadata(result)
    action = str(metadata.get("action") or "").strip()
    if action in {"edit", "read", "run", "skill", "message", "finish", "write"}:
        return action
    name = str(_artifact(result).get("name") or "")
    if name.startswith(("file_add_", "file_edit_")):
        return "edit"
    if name.startswith("file_read_"):
        return "read"
    if name.startswith("cmd_run_"):
        return "run"
    if name.startswith("skill_"):
        return "skill"
    for candidate in ("edit", "read", "run", "skill", "message", "finish", "write"):
        if name.startswith(candidate) or f"_{candidate}_" in name:
            return candidate
    return None


def _is_history(result: dict[str, Any]) -> bool:
    return bool(_metadata(result).get("isHistoryMessage"))


def _is_action(result: dict[str, Any]) -> bool:
    name = str(_artifact(result).get("name") or "")
    return name.endswith("_action") or _metadata(result).get("action") in {
        "message",
        "finish",
        "write",
    }


def _is_observation(result: dict[str, Any]) -> bool:
    name = str(_artifact(result).get("name") or "")
    return name.endswith("_observation") or bool(_metadata(result).get("observation"))


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    artifact = _artifact(result)
    parts = artifact.get("parts")
    if not isinstance(parts, list):
        return {}
    values: list[Any] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            values.append(part["data"])
        elif isinstance(part, dict) and part.get("text") is not None:
            values.append({"text": part.get("text")})
    if len(values) == 1 and isinstance(values[0], dict):
        return values[0]
    return {"parts": values}


def _iter_paths(value: Any, path: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}/{key}"
            if str(key).casefold() in {"messages", "prompt", "request", "llm_request"}:
                yield current
            yield from _iter_paths(item, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_paths(item, f"{path}/{index}")


def _extract_rollout(
    path: Path,
    *,
    include_requirement: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = _events(path)
    application = payload.get("application") if isinstance(payload.get("application"), dict) else {}
    query = str((payload.get("input") or {}).get("query") or "")
    coding = payload.get("coding") if isinstance(payload.get("coding"), dict) else {}
    raw_events = coding.get("events") if isinstance(coding.get("events"), list) else []
    history_count = 0
    current: list[dict[str, Any]] = []
    request_paths: list[str] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        result = _result(event)
        if _is_history(result):
            history_count += 1
            continue
        current.append(result)
        request_paths.extend(_iter_paths(result))

    episodes: list[dict[str, Any]] = []
    pending: dict[str, list[int]] = {}
    orphan_observations = 0
    for result in current:
        category = _category(result)
        if _is_observation(result):
            if category is None or not pending.get(category):
                orphan_observations += 1
                continue
            episode_index = pending[category].pop(0)
            episodes[episode_index]["observation"] = _redact(_payload(result))
            episodes[episode_index]["observation_event_id"] = _event_id(result)
            continue
        if not _is_action(result):
            continue
        if category is None:
            category = "unknown"
        episode = {
            "episode_id": None,
            "app_id": application.get("app_id"),
            "conversation_id": application.get("conversation_id"),
            "domain": "miaoda",
            "phase": "coding",
            "query": query,
            "action_type": category,
            "action_event_id": _event_id(result),
            "action": _redact(_payload(result)),
            "observation": None,
            "observation_event_id": None,
            "training": {
                "format": "agentos_environment_episode_v1",
                "history_events_removed": history_count,
                "model_prompt_available": False,
                "student_tokens_available": False,
                "student_logprobs_available": False,
                "teacher_logprobs_available": False,
                "status": "needs_model_request_capture",
            },
        }
        episode["episode_id"] = f"{application.get('app_id')}:{len(episodes):04d}"
        episodes.append(episode)
        pending.setdefault(category, []).append(len(episodes) - 1)

    # A later action of the same category supersedes the pending slot; any
    # unmatched observations are counted above, while unmatched actions remain
    # valid partial episodes.
    report = {
        "artifact": str(path.resolve()),
        "app_id": application.get("app_id"),
        "conversation_id": application.get("conversation_id"),
        "query_sha256": _digest(query),
        "raw_coding_events": len(raw_events),
        "history_events_removed": history_count,
        "current_coding_events": len(current),
        "episodes": len(episodes),
        "paired_episodes": sum(item["observation"] is not None for item in episodes),
        "orphan_observations": orphan_observations,
        "model_request_paths_seen": sorted(set(request_paths)),
    }
    if include_requirement:
        report["requirement_events"] = len((payload.get("requirement") or {}).get("events") or [])
    return episodes, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    paths: list[Path] = list(args.input or [])
    if args.input_dir:
        paths.extend(
            Path(item)
            for item in glob.glob(str(args.input_dir / "miaoda-e2e-canary-*.json"))
        )
    paths = sorted(set(paths))
    if not paths:
        parser.error("at least one --input or --input-dir is required")

    all_episodes: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for path in paths:
        try:
            episodes, report = _extract_rollout(path)
        except (OSError, ValueError, TypeError) as exc:
            reports.append({"artifact": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        all_episodes.extend(episodes)
        reports.append(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        for episode in all_episodes:
            stream.write(json.dumps(episode, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(temporary, args.output)
    summary = {
        "schema_version": "agentos-environment-episodes-v1",
        "artifacts": len(paths),
        "successful_artifacts": sum("error" not in item for item in reports),
        "episodes": len(all_episodes),
        "paired_episodes": sum(item["observation"] is not None for item in all_episodes),
        "training_ready_episodes": 0,
        "status": "needs_model_request_capture",
        "reports": reports,
        "output": str(args.output.resolve()),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report_tmp = args.report.with_suffix(args.report.suffix + ".part")
    report_tmp.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(report_tmp, args.report)
    print(
        json.dumps(
            {k: summary[k] for k in ("artifacts", "episodes", "paired_episodes", "status")},
            ensure_ascii=False,
        )
    )
    return 0 if summary["successful_artifacts"] == summary["artifacts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
