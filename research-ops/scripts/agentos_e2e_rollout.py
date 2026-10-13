#!/usr/bin/env python3
"""Run one raw-query Miaoda E2E rollout through AgentOS HTTP APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from research_ops.agentos import (  # noqa: E402
    AgentOSClientError,
    AgentOSEntrypointClient,
    AgentOSRolloutPhaseError,
)

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "jwt",
    "secret",
    "token",
    "x-authorization",
}


def _sensitive_key(value: str) -> bool:
    lowered = value.casefold()
    return lowered in SENSITIVE_KEYS or lowered.endswith("_key")


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.query and not parsed.fragment:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _sensitive_key(str(key))
                else _redact_url(item)
                if "url" in str(key).casefold() and isinstance(item, str)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _write_artifact(path: Path, artifact: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_jsonl_record(path: Path, index: int) -> dict[str, Any]:
    if index < 0:
        raise ValueError("record index must be non-negative")
    with path.open(encoding="utf-8") as stream:
        current = -1
        for line in stream:
            if not line.strip():
                continue
            current += 1
            if current == index:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("selected JSONL record is not an object")
                return record
    raise ValueError(f"record index {index} is outside {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    query_group = parser.add_mutually_exclusive_group(required=True)
    query_group.add_argument("--query")
    query_group.add_argument("--query-file", type=Path)
    query_group.add_argument("--record-jsonl", type=Path)
    parser.add_argument("--record-index", type=int, default=0)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-api-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace-url", required=True)
    parser.add_argument("--entrypoint-url", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--provider", default="qianfancoder_adapter_v4")
    parser.add_argument("--coding-prompti-variant", default="qianfan-coder")
    parser.add_argument("--max-tokens", type=int, default=32000)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--query-mode", default="deep_mode")
    parser.add_argument("--default-agent", default="AdaProAgent")
    parser.add_argument("--jwt-secret-env", default="AGENTOS_WORKSPACE_JWT_KEY")
    parser.add_argument("--skill-id-env", default="AGENTOS_SKILL_ID")
    parser.add_argument("--model-api-key-env", default="AGENTOS_MODEL_API_KEY")
    args = parser.parse_args()

    source_record: dict[str, Any] = {}
    if args.record_jsonl is not None:
        source_record = _read_jsonl_record(args.record_jsonl, args.record_index)
        query = str(source_record.get("query") or "").strip()
    elif args.query_file is not None:
        query = args.query_file.read_text(encoding="utf-8").strip()
    else:
        query = str(args.query or "").strip()
    if not query:
        parser.error("query must not be empty")

    skill_id = os.environ.get(args.skill_id_env, "").strip()
    model_api_key = os.environ.get(args.model_api_key_env, "demo").strip() or "demo"
    coding_config = {
        "codingPromptiVariant": args.coding_prompti_variant,
        "codingPromptiModelConfig": {
            "provider": args.provider,
            "api_url": args.model_api_url,
            "model_value": args.model_name,
            "model": args.model_name,
            "max_tokens": args.max_tokens,
            "api_key": model_api_key,
            "timeout": int(args.timeout),
            "temperature": args.temperature,
            "top_p": args.top_p,
            "thinking_config": {"thinking": {"type": "disabled"}},
        },
    }
    client = AgentOSEntrypointClient(
        workspace_base_url=args.workspace_url,
        entrypoint_base_url=args.entrypoint_url,
        user_id=args.user_id,
        jwt_secret_env=args.jwt_secret_env,
        timeout_seconds=args.timeout,
    )
    source = {
        "record_id": source_record.get("id"),
        "metadata": source_record.get("metadata") or {},
        "record_index": args.record_index if args.record_jsonl is not None else None,
    }
    try:
        rollout = client.end_to_end_rollout(
            query,
            coding_config=coding_config,
            skill_ids=[skill_id] if skill_id else [],
            query_mode=args.query_mode,
            default_agent=args.default_agent,
        )
    except AgentOSRolloutPhaseError as exc:
        artifact = {
            "application": {
                "user_id": exc.application.user_id,
                "app_id": exc.application.app_id,
                "conversation_id": exc.application.conversation_id,
            },
            "input": {"query": query, "source": source},
            "model": {
                "provider": args.provider,
                "model_name": args.model_name,
                "model_api_url": args.model_api_url,
            },
            "error": {
                "phase": exc.phase,
                "message": str(exc),
                "terminal_state": exc.result.terminal_state,
            },
            exc.phase: {
                "terminal_state": exc.result.terminal_state,
                "events": exc.result.events,
            },
        }
        _write_artifact(args.output, _redact(artifact))
        print(
            json.dumps(
                {
                    "app_id": exc.application.app_id,
                    "phase": exc.phase,
                    "terminal_state": exc.result.terminal_state,
                    "output": str(args.output),
                }
            )
        )
        return 1
    except AgentOSClientError as exc:
        artifact = {
            "input": {"query": query, "source": source},
            "model": {
                "provider": args.provider,
                "model_name": args.model_name,
                "model_api_url": args.model_api_url,
            },
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        _write_artifact(args.output, _redact(artifact))
        print(json.dumps({"error_type": type(exc).__name__, "output": str(args.output)}))
        return 1

    artifact = {
        "application": {
            "user_id": rollout.application.user_id,
            "app_id": rollout.application.app_id,
            "conversation_id": rollout.application.conversation_id,
        },
        "input": {"query": query, "source": source},
        "model": {
            "provider": args.provider,
            "model_name": args.model_name,
            "model_api_url": args.model_api_url,
        },
        "requirement": {
            "terminal_state": rollout.requirement.terminal_state,
            "events": rollout.requirement.events,
        },
        "coding": {
            "terminal_state": rollout.coding.terminal_state,
            "events": rollout.coding.events,
        },
    }
    _write_artifact(args.output, _redact(artifact))
    print(
        json.dumps(
            {
                "app_id": rollout.application.app_id,
                "conversation_id": rollout.application.conversation_id,
                "requirement_state": rollout.requirement.terminal_state,
                "coding_state": rollout.coding.terminal_state,
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
