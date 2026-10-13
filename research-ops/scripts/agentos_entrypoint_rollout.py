#!/usr/bin/env python3
"""Run one direct AgentOS application rollout without miaoda-agent-sdk."""

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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if str(key).lower() in SENSITIVE_KEYS
                else _redact_url(item)
                if "url" in str(key).lower() and isinstance(item, str)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.query and not parsed.fragment:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))


def _write_artifact(path: Path, artifact: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    query_group = parser.add_mutually_exclusive_group(required=True)
    query_group.add_argument("--query")
    query_group.add_argument("--query-file", type=Path)
    parser.add_argument("--prd-file", type=Path)
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
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--jwt-secret-env", default="AGENTOS_WORKSPACE_JWT_KEY")
    parser.add_argument("--replay-url-env", default="AGENTOS_REPLAY_URL")
    parser.add_argument("--skill-id-env", default="AGENTOS_SKILL_ID")
    parser.add_argument("--model-api-key-env", default="AGENTOS_MODEL_API_KEY")
    args = parser.parse_args()
    query = (
        args.query_file.read_text(encoding="utf-8").strip()
        if args.query_file is not None
        else str(args.query or "").strip()
    )
    if not query:
        parser.error("query must not be empty")
    prd = (
        args.prd_file.read_text(encoding="utf-8").strip()
        if args.prd_file is not None
        else query
    )
    if not prd:
        parser.error("prd must not be empty")

    skill_id = os.environ.get(args.skill_id_env, "").strip()
    replay_url = os.environ.get(args.replay_url_env, "").strip()
    model_api_key = os.environ.get(args.model_api_key_env, "demo").strip() or "demo"
    model_config = {
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
    }
    if args.top_k is not None:
        model_config["top_k"] = args.top_k
    coding_config = {
        "codingPromptiVariant": args.coding_prompti_variant,
        "codingPromptiModelConfig": model_config,
    }
    client = AgentOSEntrypointClient(
        workspace_base_url=args.workspace_url,
        entrypoint_base_url=args.entrypoint_url,
        user_id=args.user_id,
        jwt_secret_env=args.jwt_secret_env,
        timeout_seconds=args.timeout,
    )
    try:
        rollout = client.first_coding_rollout(
            query,
            coding_config=coding_config,
            prd=prd,
            replay_url=replay_url or None,
            skill_ids=[skill_id] if skill_id else [],
        )
    except AgentOSRolloutPhaseError as exc:
        artifact = _redact(
            {
                "application": {
                    "user_id": exc.application.user_id,
                    "app_id": exc.application.app_id,
                    "conversation_id": exc.application.conversation_id,
                },
                "inputs": {"query": query, "prd": prd, "uses_replay_url": bool(replay_url)},
                "model": {
                    "provider": args.provider,
                    "model_name": args.model_name,
                    "model_api_url": args.model_api_url,
                },
                "skill_count": 1 if skill_id else 0,
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
        )
        _write_artifact(args.output, artifact)
        print(
            json.dumps(
                {
                    "app_id": exc.application.app_id,
                    "phase": exc.phase,
                    "terminal_state": exc.result.terminal_state,
                    "output": str(args.output),
                },
                ensure_ascii=True,
            )
        )
        return 1
    artifact = _redact(
        {
            "application": {
                "user_id": rollout.application.user_id,
                "app_id": rollout.application.app_id,
                "conversation_id": rollout.application.conversation_id,
            },
            "inputs": {"query": query, "prd": prd, "uses_replay_url": bool(replay_url)},
            "model": {
                "provider": args.provider,
                "model_name": args.model_name,
                "model_api_url": args.model_api_url,
            },
            "skill_count": 1 if skill_id else 0,
            "prd": {
                "terminal_state": rollout.prd.terminal_state,
                "events": rollout.prd.events,
            },
            "coding": {
                "terminal_state": rollout.coding.terminal_state,
                "events": rollout.coding.events,
            },
            "trajectory": {
                "terminal_state": "pending",
                "events": [],
            },
        }
    )
    _write_artifact(args.output, artifact)
    trajectory_error = None
    try:
        trajectory = client.get_conversation_trajectory(
            rollout.application.app_id,
            max_wait_seconds=120.0,
        )
    except Exception as exc:  # keep completed rollout evidence if the query lane is unavailable
        trajectory = None
        trajectory_error = type(exc).__name__
        artifact["trajectory"] = {
            "terminal_state": "unavailable",
            "events": [],
            "error_type": trajectory_error,
        }
    else:
        artifact["trajectory"] = {
            "terminal_state": trajectory.terminal_state,
            "events": trajectory.events,
        }
    _write_artifact(args.output, _redact(artifact))
    print(
        json.dumps(
            {
                "app_id": rollout.application.app_id,
                "conversation_id": rollout.application.conversation_id,
                "prd_state": rollout.prd.terminal_state,
                "coding_state": rollout.coding.terminal_state,
                "trajectory_state": (
                    trajectory.terminal_state if trajectory is not None else "unavailable"
                ),
                "trajectory_error_type": trajectory_error,
                "output": str(args.output),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
