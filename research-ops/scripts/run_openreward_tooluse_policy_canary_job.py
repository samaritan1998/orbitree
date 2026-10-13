#!/usr/bin/env python3
"""Submit one real ToolUse policy canary as a Kubernetes Job."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KUBECONFIG = Path("/srv/research-ops/tools/cluster-config")
DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-tooluse-k8s/policy-canary-job.yaml"
DEFAULT_SLIME_ROOT = Path(
    "/srv/research-ops/workspaces/alice/qianfan_coder/glm_post_train/0527_relax/baidu/acg-agi/slime"
)
DEFAULT_EVIDENCE_ROOT = PROJECT_ROOT / "data/real_four_domain_20260722"
DEFAULT_CHECKPOINT = Path(
    "/data/models/experiments/qianfan_coder_glm/glm_post_train/"
    "hf_checkpoints/train-example-qw36-online-mopd-smoke-0717-iter0-hf"
)


def _clients(kubeconfig: Path) -> tuple[Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install the Kubernetes Python client") from exc
    configuration = client.Configuration()
    config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    configuration.proxy = None
    api = client.ApiClient(configuration)
    return client.CoreV1Api(api), client.BatchV1Api(api)


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def _render(template: Path, replacements: dict[str, str]) -> dict[str, Any]:
    text = template.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(f"@@{key}@@", json.dumps(value, ensure_ascii=False))
    document = yaml.safe_load(text)
    if not isinstance(document, dict) or document.get("kind") != "Job":
        raise ValueError("policy canary manifest must contain one Job")
    return document


def _state(core: Any, batch: Any, namespace: str, job_name: str) -> dict[str, Any]:
    job = batch.read_namespaced_job(job_name, namespace, _request_timeout=30)
    pods = core.list_namespaced_pod(
        namespace,
        label_selector=f"job-name={job_name}",
        _request_timeout=30,
    ).items
    pod = pods[0] if pods else None
    waiting = None
    if pod:
        waiting = next(
            (
                item.state.waiting.reason
                for item in (pod.status.container_statuses or [])
                if item.state and item.state.waiting
            ),
            None,
        )
    return {
        "job": job_name,
        "pod": pod.metadata.name if pod else None,
        "phase": pod.status.phase if pod else "Pending",
        "waiting_reason": waiting,
        "active": job.status.active or 0,
        "succeeded": job.status.succeeded or 0,
        "failed": job.status.failed or 0,
    }


def _wait(core: Any, batch: Any, namespace: str, job_name: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        current = _state(core, batch, namespace, job_name)
        if current != last:
            print(json.dumps(current, sort_keys=True), flush=True)
            last = current
        if current["succeeded"]:
            return current
        if current["failed"]:
            pod = current.get("pod")
            logs = core.read_namespaced_pod_log(
                pod,
                namespace,
                container="canary",
                tail_lines=200,
                _request_timeout=30,
            ) if pod else ""
            raise RuntimeError(f"ToolUse policy canary failed:\n{logs}")
        time.sleep(10)
    raise TimeoutError(f"ToolUse policy canary timed out: {json.dumps(last)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--slime-root", type=Path, default=DEFAULT_SLIME_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--policy-url", default="http://198.51.100.51:8080")
    parser.add_argument("--policy-model", default="ors-qw36-online-mopd-iter0")
    parser.add_argument("--task-index", type=int, default=0)
    parser.add_argument("--image", default="registry.example.com/acg-agi/slime:v0.3.0")
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--ors-auth-secret", default="openreward-ors-auth")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.slime_root.is_dir():
        raise RuntimeError(f"Slime repository is unavailable: {args.slime_root}")
    if not args.checkpoint.is_dir():
        raise RuntimeError(f"policy checkpoint is unavailable: {args.checkpoint}")
    if args.task_index < 0:
        raise RuntimeError("task index must be non-negative")
    args.evidence_root.mkdir(parents=True, exist_ok=True)
    document = _render(
        args.template,
        {
            "NAMESPACE": args.namespace,
            "SLIME_IMAGE": args.image,
            "IMAGE_PULL_SECRET": args.image_pull_secret,
            "ORS_AUTH_SECRET": args.ors_auth_secret,
            "POLICY_URL": args.policy_url,
            "POLICY_MODEL": args.policy_model,
            "CHECKPOINT": str(args.checkpoint),
            "TASK_INDEX": str(args.task_index),
            "RESEARCH_OPS_ROOT": str(PROJECT_ROOT.resolve(strict=True)),
            "SLIME_ROOT": str(args.slime_root.resolve(strict=True)),
            "EVIDENCE_ROOT": str(args.evidence_root.resolve()),
        },
    )
    if args.dry_run:
        print(json.dumps({"ready": True, "kind": document["kind"]}))
        return 0
    core, batch = _clients(args.kubeconfig.expanduser().resolve(strict=True))
    try:
        secret = core.read_namespaced_secret(
            args.ors_auth_secret,
            args.namespace,
            _request_timeout=30,
        )
    except Exception as exc:
        if _status(exc) == 404:
            raise RuntimeError("ORS auth Secret is missing") from exc
        raise
    if "api_key" not in (secret.data or {}):
        raise RuntimeError("ORS auth Secret is missing api_key")
    created = batch.create_namespaced_job(args.namespace, document, _request_timeout=30)
    job_name = created.metadata.name
    print(json.dumps({"submitted": True, "job": job_name}), flush=True)
    result = _wait(core, batch, args.namespace, job_name, args.timeout_seconds)
    pod_name = result.get("pod")
    logs = core.read_namespaced_pod_log(
        pod_name,
        args.namespace,
        container="canary",
        tail_lines=200,
        _request_timeout=30,
    )
    if isinstance(logs, bytes):
        logs = logs.decode("utf-8", errors="replace")
    print(logs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
