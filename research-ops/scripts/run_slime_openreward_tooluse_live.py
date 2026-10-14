#!/usr/bin/env python3
"""Run one real interactive pure-MOPD episode through the OpenReward adapter.

The script is intended for a short-lived Kubernetes Job.  It writes bounded
sample evidence to CFS and never serializes credentials or the full response
text.  The policy endpoint must expose Slime's native ``/generate`` contract.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from argparse import Namespace
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_teacher_route_validator() -> tuple[type[Exception], Any]:
    """Load the lightweight resolver without importing Orbitree server deps."""

    path = PROJECT_ROOT / "backend" / "research_ops" / "openreward" / "teacher_routing.py"
    spec = importlib.util.spec_from_file_location("_openreward_teacher_routing", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OpenReward teacher routing module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.TeacherRoutingError, module.validate_runtime_teacher_route


TeacherRoutingError, validate_runtime_teacher_route = _load_teacher_route_validator()

from slime.rollout.openreward_rollout import build_debug_rollout_dump, generate  # noqa: E402
from slime.utils.http_utils import init_http_client  # noqa: E402
from slime.utils.types import Sample  # noqa: E402

_PUBLIC_TASK_FIELDS = (
    "task_id",
    "id",
    "episode_id",
    "case_id",
    "query",
    "benchmark_family",
    "task_name",
)
_PUBLIC_SOURCE_FIELDS = {
    "dataset",
    "split",
    "revision",
    "repository",
    "source_revision",
    "source_row",
}


def _public_task_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    """Keep joinable task identity without exporting verifier or sandbox data."""

    public: dict[str, Any] = {}
    for key in _PUBLIC_TASK_FIELDS:
        value = task.get(key)
        if isinstance(value, (str, int, float, bool)):
            if key == "query" and len(str(value)) > 16_000:
                value = str(value)[:16_000] + "<truncated>"
            public[key] = value
    source = task.get("source")
    if isinstance(source, Mapping):
        public_source = {
            str(key): value
            for key, value in source.items()
            if str(key) in _PUBLIC_SOURCE_FIELDS
            and isinstance(value, (str, int, float, bool))
        }
        if public_source:
            public["source"] = public_source
    return public


def _sanitize_sample_task_metadata(sample: Sample) -> None:
    """Remove hidden verifier/task runtime fields before evidence or .pt export."""

    metadata = sample.metadata if isinstance(sample.metadata, dict) else {}
    task = metadata.get("openreward_task")
    if isinstance(task, Mapping):
        metadata["openreward_task"] = _public_task_metadata(task)
        metadata["openreward_task_metadata_sanitized"] = True
    # The trace is useful for bounded human evidence, but is not a training
    # input and may contain provider/tool output.  Keep it out of .pt metadata.
    metadata.pop("openreward_trace", None)
    sample.metadata = metadata


def _required(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"missing runtime reference: {name}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, str(default))).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _router(url: str) -> tuple[str, int]:
    parsed = urlsplit(url if "://" in url else f"http://{url}")
    if not parsed.hostname:
        raise RuntimeError("policy URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port


def _args(
    *,
    checkpoint: str,
    policy_url: str,
    model: str,
    environment: str,
    max_new_tokens: int,
    max_turns: int,
    max_length_continuations: int,
    finalize_action_on_limit: str | None,
    pure_opd: bool,
    complete_on_turn_limit: bool,
) -> Namespace:
    host, port = _router(policy_url)
    return Namespace(
        partial_rollout=False,
        hf_checkpoint=checkpoint,
        sglang_server_concurrency=1,
        rollout_num_gpus=1,
        rollout_num_gpus_per_engine=1,
        rollout_temperature=0.0,
        rollout_top_p=1.0,
        rollout_top_k=-1,
        rollout_max_response_len=max_new_tokens,
        rollout_stop=[],
        rollout_stop_token_ids=[],
        rollout_skip_special_tokens=False,
        sglang_enable_deterministic_inference=True,
        rollout_seed=0,
        n_samples_per_prompt=1,
        sglang_dp_size=1,
        sglang_model_routers={model: (host, port)},
        sglang_router_ip=host,
        sglang_router_port=port,
        use_distributed_post=False,
        num_gpus_per_node=1,
        openreward_policy_model=model,
        openreward_env=environment,
        openreward_mode="interactive",
        openreward_base_url=_required("ORS_URL"),
        openreward_api_key_env="ORS_API_KEY",
        openreward_require_logprobs=True,
        openreward_max_turns=max_turns,
        openreward_max_length_continuations=max_length_continuations,
        openreward_finalize_action_on_limit=finalize_action_on_limit or "",
        openreward_poll_interval=0.0,
        openreward_lightweight_tokenizer=True,
        openreward_pure_opd=pure_opd,
        openreward_complete_on_turn_limit=complete_on_turn_limit,
    )


def _status_name(sample: Sample) -> str:
    return getattr(sample.status, "name", str(sample.status)).casefold()


def _evidence(
    sample: Sample,
    *,
    started: datetime,
    policy_model: str,
    environment: str,
    task_index: int,
    max_turns: int,
) -> dict[str, Any]:
    tokens = [int(item) for item in sample.tokens]
    mask = [int(item) for item in (sample.loss_mask or [])]
    logprobs = [float(item) for item in (sample.rollout_log_probs or [])]
    teacher_logprobs = [float(item) for item in (sample.teacher_log_probs or [])]
    metadata = sample.metadata if isinstance(sample.metadata, dict) else {}
    raw_trace = metadata.get("openreward_trace")
    safe_metadata = {
        key: value
        for key, value in metadata.items()
        if key.startswith("openreward_") and key not in {"openreward_trace"}
    }
    task = safe_metadata.get("openreward_task")
    if isinstance(task, Mapping):
        safe_metadata["openreward_task"] = _public_task_metadata(task)
        safe_metadata["openreward_task_metadata_sanitized"] = True
    action_trace: list[dict[str, Any]] = []
    if isinstance(raw_trace, list):
        for item in raw_trace:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip()
            if not action:
                continue
            reward = item.get("reward")
            action_trace.append(
                {
                    "turn": int(item.get("turn") or 0),
                    "action": action,
                    "finished": bool(item.get("finished")),
                    "reward": reward if isinstance(reward, (int, float)) else None,
                    "source": str(item.get("source") or "policy"),
                }
            )
    return {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
        "environment": environment,
        "policy_model": policy_model,
        "task_index": task_index,
        "max_turns": max_turns,
        "finalized_on_limit": bool(metadata.get("openreward_finalized_on_limit")),
        "status": _status_name(sample),
        "finished": bool(metadata.get("openreward_finished")),
        "training_objective": metadata.get("openreward_training_objective"),
        "task_reward_used": metadata.get("openreward_task_reward_used"),
        "observed_task_reward": metadata.get("openreward_observed_task_reward"),
        "reward": sample.reward,
        "token_count": len(tokens),
        "response_length": int(sample.response_length),
        "loss_mask_count": len(mask),
        "loss_mask_ones": sum(mask),
        "logprob_count": len(logprobs),
        "finite_logprob_count": sum(math.isfinite(value) for value in logprobs),
        "token_logprob_lengths_match": len(mask) == len(logprobs) == int(sample.response_length),
        "teacher_logprob_count": len(teacher_logprobs),
        "finite_teacher_logprob_count": sum(
            math.isfinite(value) for value in teacher_logprobs
        ),
        "teacher_logprob_lengths_match": len(teacher_logprobs) == int(sample.response_length),
        "tokens_sha256": hashlib.sha256(
            json.dumps(tokens, separators=(",", ":")).encode()
        ).hexdigest(),
        "logprobs_sha256": hashlib.sha256(
            json.dumps(logprobs, separators=(",", ":")).encode()
        ).hexdigest(),
        "weight_versions": list(sample.weight_versions),
        "action_trace": action_trace,
        "metadata": safe_metadata,
    }


def _write_training_sample(
    path: Path,
    sample: Sample,
    *,
    include_text: bool,
) -> str:
    import torch

    payload = build_debug_rollout_dump(
        [sample],
        rollout_id=0,
        include_text=include_text,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        torch.save(payload, temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", default=os.environ.get("MOPD_POLICY_CHECKPOINT", ""))
    parser.add_argument("--policy-model", default=os.environ.get("MOPD_POLICY_MODEL_NAME", ""))
    parser.add_argument("--policy-url", default=os.environ.get("MOPD_POLICY_API_URL", ""))
    parser.add_argument(
        "--environment",
        choices=("tooluse", "swe", "ppt", "miaoda"),
        default=os.environ.get("OPENREWARD_ENV", "tooluse"),
    )
    teacher_routes_env = str(
        os.environ.get(
            "SLIME_OPENREWARD_TEACHER_ROUTES_FILE",
            os.environ.get("TEACHER_ROUTES_FILE", ""),
        )
    ).strip()
    parser.add_argument(
        "--teacher-routes",
        type=Path,
        default=Path(teacher_routes_env) if teacher_routes_env else None,
        help="Runtime route JSON materialized by the four-domain MOPD launcher.",
    )
    parser.add_argument(
        "--task-index",
        type=int,
        default=int(
            os.environ.get("OPENREWARD_TASK_INDEX", os.environ.get("TOOLUSE_TASK_INDEX", "0"))
        ),
    )
    task_spec_env = str(
        os.environ.get(
            "OPENREWARD_TASK_SPEC_PATH",
            os.environ.get("TOOLUSE_TASK_SPEC_PATH", ""),
        )
    ).strip()
    parser.add_argument(
        "--task-spec",
        type=Path,
        default=Path(task_spec_env) if task_spec_env else None,
        help="Optional inline official task-spec JSON; overrides split/index selection.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            os.environ.get(
                "OPENREWARD_EVIDENCE_PATH",
                os.environ.get("TOOLUSE_EVIDENCE_PATH", "/tmp/openreward-policy-evidence.json"),
            )
        ),
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=int(
            os.environ.get(
                "OPENREWARD_MAX_NEW_TOKENS",
                os.environ.get("TOOLUSE_MAX_NEW_TOKENS", "1024"),
            )
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=int(
            os.environ.get(
                "OPENREWARD_MAX_TURNS",
                os.environ.get("TOOLUSE_MAX_TURNS", "48"),
            )
        ),
    )
    parser.add_argument(
        "--max-length-continuations",
        type=int,
        default=int(os.environ.get("SLIME_OPENREWARD_MAX_LENGTH_CONTINUATIONS", "2")),
    )
    parser.add_argument(
        "--finalize-action-on-limit",
        default=os.environ.get(
            "OPENREWARD_FINALIZE_ACTION_ON_LIMIT",
            os.environ.get("TOOLUSE_FINALIZE_ACTION_ON_LIMIT", ""),
        ),
    )
    sample_output_env = str(
        os.environ.get(
            "OPENREWARD_SAMPLE_OUTPUT",
            os.environ.get("TOOLUSE_SAMPLE_OUTPUT", ""),
        )
    ).strip()
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=Path(sample_output_env) if sample_output_env else None,
        help="Optional Slime --load-debug-rollout-data compatible .pt output.",
    )
    parser.add_argument(
        "--sample-include-text",
        action="store_true",
        help="Include policy prompt/response text in the training dump.",
    )
    parser.add_argument(
        "--pure-opd",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("SLIME_OPENREWARD_PURE_OPD", True),
        help=(
            "Use a neutral training reward and learn only from teacher token "
            "logprobs; task reward remains optional metadata."
        ),
    )
    parser.add_argument(
        "--complete-on-turn-limit",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("SLIME_OPENREWARD_COMPLETE_ON_TURN_LIMIT", False),
        help=(
            "Close a pure-OPD episode at its horizon without invoking a task "
            "reward/verifier finalizer."
        ),
    )
    args = parser.parse_args()
    checkpoint = str(args.checkpoint).strip() or _required("MOPD_POLICY_CHECKPOINT")
    policy_model = str(args.policy_model).strip() or _required("MOPD_POLICY_MODEL_NAME")
    policy_url = str(args.policy_url).strip() or _required("MOPD_POLICY_API_URL")
    if args.task_index < 0:
        raise RuntimeError("task index must be non-negative")
    if args.max_new_tokens < 1:
        raise RuntimeError("max new tokens must be positive")
    if args.max_turns < 1:
        raise RuntimeError("max turns must be positive")
    if args.max_length_continuations < 0:
        raise RuntimeError("max length continuations must be non-negative")
    if args.teacher_routes is not None:
        try:
            validate_runtime_teacher_route(
                args.teacher_routes,
                domain=args.environment,
                check_health=True,
            )
        except TeacherRoutingError as exc:
            raise RuntimeError(f"teacher route is not ready: {exc}") from exc
        # The Slime adapter selects the correct endpoint for each sample's
        # domain and records only checkpoint lineage in its evidence.
        os.environ["SLIME_OPENREWARD_TEACHER_ROUTES_FILE"] = str(args.teacher_routes)
        os.environ["SLIME_OPENREWARD_REQUIRE_TEACHER_LOGPROBS"] = "true"

    metadata: dict[str, Any] = {
        "openreward_env": args.environment,
        "openreward_mode": "interactive",
        "openreward_split": "train",
        "openreward_task_index": args.task_index,
        "openreward_task_id": f"{args.environment}-{args.task_index}",
        "openreward_pure_opd": bool(args.pure_opd),
        "openreward_complete_on_turn_limit": bool(args.complete_on_turn_limit),
    }
    if args.task_spec is not None:
        task_spec = json.loads(args.task_spec.read_text(encoding="utf-8"))
        if not isinstance(task_spec, dict):
            raise RuntimeError("--task-spec must contain a JSON object")
        environments = task_spec.get("environments")
        if isinstance(environments, dict):
            environment_catalog = environments.get(args.environment)
            train = (
                environment_catalog.get("train")
                if isinstance(environment_catalog, dict)
                else None
            )
            if not isinstance(train, list) or args.task_index >= len(train):
                raise RuntimeError("task catalog does not contain the selected task")
            selected = train[args.task_index]
            if not isinstance(selected, dict):
                raise RuntimeError("selected catalog task is not an object")
            task_spec = selected
        metadata.pop("openreward_split", None)
        metadata.pop("openreward_task_index", None)
        metadata["openreward_task"] = task_spec
        task_id = task_spec.get("task_id") or task_spec.get("id")
        if task_id is not None and str(task_id).strip():
            metadata["openreward_task_id"] = str(task_id).strip()

    sample = Sample(
        index=args.task_index,
        prompt="",
        metadata=metadata,
    )
    started = datetime.now(UTC)
    rollout_args = _args(
        checkpoint=checkpoint,
        policy_url=policy_url,
        model=policy_model,
        environment=args.environment,
        max_new_tokens=args.max_new_tokens,
        max_turns=args.max_turns,
        max_length_continuations=args.max_length_continuations,
        finalize_action_on_limit=str(args.finalize_action_on_limit).strip() or None,
        pure_opd=bool(args.pure_opd),
        complete_on_turn_limit=bool(args.complete_on_turn_limit),
    )
    init_http_client(rollout_args)
    result = asyncio.run(
        generate(
            rollout_args,
            sample,
            {
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": -1,
                "max_new_tokens": args.max_new_tokens,
                "return_logprob": True,
            },
        )
    )
    if not isinstance(result, Sample):
        raise RuntimeError("OpenReward adapter returned an unexpected sample type")
    evidence = _evidence(
        result,
        started=started,
        policy_model=policy_model,
        environment=args.environment,
        task_index=args.task_index,
        max_turns=args.max_turns,
    )
    _sanitize_sample_task_metadata(result)
    sample_export_error = False
    if args.sample_output is not None:
        try:
            sample_sha256 = _write_training_sample(
                args.sample_output,
                result,
                include_text=bool(args.sample_include_text),
            )
        except Exception as exc:
            sample_export_error = True
            detail = re.sub(r"https?://\S+", "<url>", str(exc).strip())
            evidence["sample_export_error"] = {
                "type": type(exc).__name__,
                "detail": detail[:240] or type(exc).__name__,
            }
        else:
            evidence.update(
                {
                    "sample_artifact_format": "slime.debug_rollout.v1",
                    "sample_artifact_path": str(args.sample_output),
                    "sample_artifact_sha256": sample_sha256,
                    "sample_includes_text": bool(args.sample_include_text),
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    if sample_export_error:
        return 2
    if evidence["status"] != "completed" or not evidence["finished"]:
        return 2
    if evidence["reward"] is None or not evidence["token_logprob_lengths_match"]:
        return 2
    if evidence["finite_logprob_count"] != evidence["logprob_count"]:
        return 2
    require_teacher = str(
        os.environ.get("SLIME_OPENREWARD_REQUIRE_TEACHER_LOGPROBS", "false")
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if require_teacher and (
        not evidence["teacher_logprob_lengths_match"]
        or evidence["finite_teacher_logprob_count"] != evidence["teacher_logprob_count"]
        or evidence["teacher_logprob_count"] != evidence["response_length"]
    ):
        return 2
    if not evidence["weight_versions"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
