#!/usr/bin/env python3
"""Run a deterministic Slime adapter episode against the real SWE ORS runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from research_ops.openreward import (
    RemoteServiceConfig,
    SweSandboxBackend,
    build_default_environments,
    create_openreward_app,
    load_task_catalogs,
)
from research_ops.openreward.swe_runtime import (
    DockerSweEngine,
    OfficialSwebenchHarness,
    SweRuntimeConfig,
    SweRuntimeService,
    create_swe_runtime_app,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-swe-canary.json"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "data"
    / "openreward_swe_canary_20260720"
    / "slime_adapter_integration.json"
)
DEFAULT_HARNESS = Path("/srv/research-ops/tools/SWE-bench")


class _Status(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    ABORTED = "aborted"
    FAILED = "failed"


class _Sample:
    Status = _Status

    def __init__(self, *, index: int, task_id: str) -> None:
        self.index = index
        self.prompt = "public query is resolved by ORS"
        self.tokens: list[int] = []
        self.response = ""
        self.response_length = 0
        self.loss_mask: list[int] | None = None
        self.rollout_log_probs: list[float] | None = None
        self.reward: float | None = None
        self.status = _Status.PENDING
        self.remove_sample = False
        self.metadata = {
            "openreward_env": "swe",
            "openreward_mode": "interactive",
            "openreward_split": "train",
            "openreward_task_index": index,
            "openreward_task_id": task_id,
        }
        self.multimodal_inputs = None


class _Tokenizer:
    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        tokenize: bool = False,
        add_generation_prompt: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        del tokenize, tools
        rendered = "|".join(
            f"{item['role']}:{item.get('content', '')}" for item in messages
        )
        return rendered + ("|assistant:" if add_generation_prompt else "")

    def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]:
        del add_special_tokens
        return [ord(character) % 251 for character in text]


class _GenerateState:
    def __init__(self, _args: Any) -> None:
        self.tokenizer = _Tokenizer()


def _install_deterministic_sglang_module() -> None:
    module = types.ModuleType("slime.rollout.sglang_rollout")
    module.GenerateState = _GenerateState
    module.get_model_url = lambda _args, _model, endpoint: f"http://policy.local{endpoint}"
    sys.modules[module.__name__] = module


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    slime_project_root = args.slime_project_root.resolve()
    if not (slime_project_root / "slime" / "rollout" / "openreward_rollout.py").is_file():
        raise RuntimeError("--slime-project-root does not contain the Slime Python package")
    sys.path.insert(0, str(slime_project_root))
    _install_deterministic_sglang_module()

    from slime.openreward.client import OpenRewardClient
    from slime.rollout import openreward_rollout as rollout

    tasks = list(load_task_catalogs(args.catalog)["swe"]["train"])
    task = tasks[args.index]
    task_id = str(task["task_id"])
    harness = OfficialSwebenchHarness(harness_root=args.harness_root)
    runtime_config = SweRuntimeConfig.from_environ()
    service = SweRuntimeService(
        DockerSweEngine(runtime_config),
        harness,
        ttl_seconds=runtime_config.session_ttl_seconds,
    )
    sidecar = create_swe_runtime_app(service)
    backend = SweSandboxBackend(
        RemoteServiceConfig(
            environment="swe",
            base_url_env="SWE_INTEGRATION_URL",
            create_operation="POST /v1/sandboxes",
            action_operations={
                "bash": "POST /v1/sandboxes/{sandbox_id}/exec",
                "read_file": "POST /v1/sandboxes/{sandbox_id}/files/read",
                "write_file": "POST /v1/sandboxes/{sandbox_id}/files/write",
                "submit": "POST /v1/sandboxes/{sandbox_id}/verify",
            },
            destroy_operation="DELETE /v1/sandboxes/{sandbox_id}",
            terminal_actions=frozenset({"submit"}),
            timeout_seconds=runtime_config.verify_seconds + 300,
        ),
        environ={"SWE_INTEGRATION_URL": "http://swe-runtime.local"},
        transport=httpx.ASGITransport(app=sidecar),
    )
    ors = create_openreward_app(
        build_default_environments(
            backends={"swe": backend},
            task_catalogs={"swe": {"train": tasks}},
        )
    )

    policy_requests: list[dict[str, Any]] = []

    async def deterministic_policy(_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        policy_requests.append(payload)
        return {
            "text": '<tool_call>{"name":"submit","arguments":{}}</tool_call>',
            "meta_info": {
                "output_token_logprobs": [[-0.125, 31], [-0.25, 32]],
                "finish_reason": {"type": "stop"},
            },
        }

    rollout.post = deterministic_policy
    sample = _Sample(index=args.index, task_id=task_id)
    client = OpenRewardClient(
        "http://ors.local",
        transport=httpx.ASGITransport(app=ors),
    )
    try:
        result = await rollout._run_interactive(
            SimpleNamespace(openreward_policy_model="deterministic-submit"),
            client,
            sample,
            rollout.OpenRewardRolloutConfig(
                env_name="swe",
                mode="interactive",
                max_turns=1,
                require_logprobs=True,
            ),
            {"max_new_tokens": 32},
        )
    finally:
        await client.close()
        await service.shutdown()

    status = getattr(result.status, "value", str(result.status))
    if status != "completed":
        raise RuntimeError(f"Slime adapter canary did not complete: {status}")
    if result.response_length != len(result.rollout_log_probs or []):
        raise RuntimeError("Slime response/logprob lengths diverged")
    if result.response_length != len(result.loss_mask or []):
        raise RuntimeError("Slime response/loss-mask lengths diverged")
    if not policy_requests or policy_requests[0].get("return_logprob") is not True:
        raise RuntimeError("Slime policy request did not require behavior logprobs")

    response_tokens = result.tokens[-result.response_length :]
    return {
        "version": 1,
        "environment": "swe",
        "policy": "deterministic-submit-control",
        "selector": {"split": "train", "index": args.index},
        "task_id": task_id,
        "status": status,
        "reward": result.reward,
        "prompt_token_count": len(result.tokens) - result.response_length,
        "response_token_ids": response_tokens,
        "rollout_log_probs": result.rollout_log_probs,
        "loss_mask": result.loss_mask,
        "policy_request_count": len(policy_requests),
        "openreward_ors_session_id": result.metadata["openreward_ors_session_id"],
        "openreward_task_id": result.metadata["openreward_task_id"],
        "openreward_env": result.metadata["openreward_env"],
        "terminal_metadata": result.metadata.get("openreward_terminal_metadata", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slime-project-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--harness-root", type=Path, default=DEFAULT_HARNESS)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    evidence = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
