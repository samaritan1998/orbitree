#!/usr/bin/env python3
"""Deploy the SlideForge Reward adapter to Kubernetes without exposing secrets."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-ppt-k8s/reward-adapter.yaml"
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "data/example-ppt-bundles/bundles"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "data/openreward_ppt_k8s/artifacts"
DEFAULT_WORK_ROOT = PROJECT_ROOT / "data/openreward_ppt_k8s/reward-work"
DEFAULT_IMAGE = "registry.example.com/acg-agi/ppt-rollout:0610-deps"
DEFAULT_MODEL_ENDPOINT = "https://www.huayanapi.com"
DEFAULT_MODEL_NAME = "ccop-4-6-agent"
_PLACEHOLDER = re.compile(r"@@[A-Z0-9_]+@@")
_SECRET_KEYS = {"runtime_token", "rubric_token"}


def _absolute_directory(
    path: Path,
    *,
    create: bool = False,
    allow_missing: bool = False,
) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"path must be absolute: {candidate}")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if allow_missing and not candidate.exists():
        return candidate.resolve(strict=False)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"path is not a directory: {resolved}")
    return resolved


def build_replacements(args: argparse.Namespace) -> dict[str, str]:
    research_ops_root = _absolute_directory(PROJECT_ROOT)
    slideforge_root = _absolute_directory(args.slideforge_root)
    bundle_root = _absolute_directory(args.bundle_root)
    artifact_root = _absolute_directory(
        args.artifact_root,
        create=not args.dry_run,
        allow_missing=args.dry_run,
    )
    work_root = _absolute_directory(
        args.work_root,
        create=not args.dry_run,
        allow_missing=args.dry_run,
    )
    required = (
        slideforge_root / "pipeline/stages/reward/build_candidate_bundle.py",
        slideforge_root / "pipeline/stages/reward/run_reward_with_claude_skill.sh",
        slideforge_root / "skills/ppt-reward-evaluator/scripts/validate_evaluation.py",
    )
    if any(not item.is_file() for item in required):
        raise ValueError("SlideForge root is missing required Reward scripts")
    return {
        "NAMESPACE": args.namespace,
        "PPT_IMAGE": args.image,
        "IMAGE_PULL_SECRET": args.image_pull_secret,
        "AUTH_SECRET": args.auth_secret,
        "MODEL_SECRET": args.model_secret,
        "MODEL_SECRET_KEY": args.model_secret_key,
        "PROXY_SECRET": args.proxy_secret,
        "MODEL_ENDPOINT": args.model_endpoint,
        "MODEL_NAME": args.model_name,
        "RESEARCH_OPS_ROOT": str(research_ops_root),
        "SLIDEFORGE_ROOT": str(slideforge_root),
        "BUNDLE_ROOT": str(bundle_root),
        "ARTIFACT_ROOT": str(artifact_root),
        "WORK_ROOT": str(work_root),
    }


def render_documents(template: Path, replacements: dict[str, str]) -> list[dict[str, Any]]:
    rendered = template.read_text(encoding="utf-8")
    for name, value in replacements.items():
        rendered = rendered.replace(f"@@{name}@@", json.dumps(value, ensure_ascii=False))
    unresolved = sorted(set(_PLACEHOLDER.findall(rendered)))
    if unresolved:
        raise ValueError(f"manifest has unresolved placeholders: {', '.join(unresolved)}")
    return [item for item in yaml.safe_load_all(rendered) if item]


def render_manifest(template: Path, replacements: dict[str, str]) -> list[dict[str, Any]]:
    documents = render_documents(template, replacements)
    if len(documents) != 2 or {item.get("kind") for item in documents} != {
        "Deployment",
        "Service",
    }:
        raise ValueError("manifest must contain exactly the Reward Deployment and Service")
    return documents


def _kubernetes_client(kubeconfig: Path) -> tuple[Any, Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("install the ppt-k8s extra before deploying") from exc
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig),
        client_configuration=configuration,
    )
    configuration.proxy = None
    api_client = client.ApiClient(configuration)
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client), client


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def ensure_reference_secret(
    core: Any,
    *,
    namespace: str,
    name: str,
    key: str,
) -> None:
    secret = core.read_namespaced_secret(name, namespace, _request_timeout=30)
    if key not in (secret.data or {}):
        raise RuntimeError(f"Kubernetes Secret {name!r} is missing key {key!r}")


def ensure_auth_secret(core: Any, client: Any, *, namespace: str, name: str) -> str:
    try:
        existing = core.read_namespaced_secret(name, namespace, _request_timeout=30)
    except Exception as exc:
        if _status(exc) != 404:
            raise
        values = {key: secrets.token_urlsafe(48) for key in sorted(_SECRET_KEYS)}
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/part-of": "research-ops",
                    "research-ops.work-id": "harn-0001",
                },
            ),
            type="Opaque",
            data={
                key: base64.b64encode(value.encode()).decode()
                for key, value in values.items()
            },
        )
        core.create_namespaced_secret(namespace, body, _request_timeout=30)
        return "created"
    missing = _SECRET_KEYS.difference((existing.data or {}).keys())
    if missing:
        patch = {
            "data": {
                key: base64.b64encode(secrets.token_urlsafe(48).encode()).decode()
                for key in sorted(missing)
            }
        }
        core.patch_namespaced_secret(name, namespace, patch, _request_timeout=30)
        return "completed"
    return "preserved"


def apply_documents(core: Any, apps: Any, documents: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for body in documents:
        kind = body["kind"]
        metadata = body["metadata"]
        name = metadata["name"]
        namespace = metadata["namespace"]
        if kind == "Deployment":
            try:
                apps.read_namespaced_deployment(name, namespace, _request_timeout=30)
            except Exception as exc:
                if _status(exc) != 404:
                    raise
                apps.create_namespaced_deployment(namespace, body, _request_timeout=30)
                actions.append(f"Deployment/{name}=created")
            else:
                apps.patch_namespaced_deployment(name, namespace, body, _request_timeout=30)
                actions.append(f"Deployment/{name}=patched")
        elif kind == "Service":
            try:
                core.read_namespaced_service(name, namespace, _request_timeout=30)
            except Exception as exc:
                if _status(exc) != 404:
                    raise
                core.create_namespaced_service(namespace, body, _request_timeout=30)
                actions.append(f"Service/{name}=created")
            else:
                core.patch_namespaced_service(name, namespace, body, _request_timeout=30)
                actions.append(f"Service/{name}=patched")
        else:  # pragma: no cover - render_manifest rejects this
            raise ValueError(f"unsupported Kubernetes kind: {kind}")
    return actions


def wait_for_reward_pod(
    core: Any,
    apps: Any,
    *,
    namespace: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    selector = "app.kubernetes.io/name=openreward-ppt-reward"
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(
            "openreward-ppt-reward",
            namespace,
            _request_timeout=30,
        )
        pods = core.list_namespaced_pod(
            namespace,
            label_selector=selector,
            _request_timeout=30,
        ).items
        latest = {
            "deployment_generation": deployment.metadata.generation,
            "observed_generation": deployment.status.observed_generation,
            "ready_replicas": deployment.status.ready_replicas or 0,
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": all(item.ready for item in (pod.status.container_statuses or [])),
                    "image_id": next(
                        (
                            item.image_id
                            for item in (pod.status.container_statuses or [])
                            if item.image_id
                        ),
                        None,
                    ),
                }
                for pod in pods
            ],
        }
        if (
            deployment.status.observed_generation == deployment.metadata.generation
            and (deployment.status.ready_replicas or 0) >= 1
        ):
            return latest
        time.sleep(3)
    raise RuntimeError(f"Reward Deployment did not become ready: {json.dumps(latest)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=os.environ.get("ORS_PPT_KUBECONFIG"),
        required=not bool(os.environ.get("ORS_PPT_KUBECONFIG")),
    )
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--slideforge-root", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-ppt-auth")
    parser.add_argument("--model-secret", default="openreward-ppt-model")
    parser.add_argument("--model-secret-key", default="api_key")
    parser.add_argument("--proxy-secret", default="openreward-ppt-proxy")
    parser.add_argument("--model-endpoint", default=DEFAULT_MODEL_ENDPOINT)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    replacements = build_replacements(args)
    documents = render_manifest(args.template, replacements)
    if args.dry_run:
        print(json.dumps({"ready": True, "resources": [item["kind"] for item in documents]}))
        return 0
    kubeconfig = args.kubeconfig.expanduser().resolve(strict=True)
    core, apps, client = _kubernetes_client(kubeconfig)
    ensure_reference_secret(
        core,
        namespace=args.namespace,
        name=args.model_secret,
        key=args.model_secret_key,
    )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"):
        ensure_reference_secret(
            core,
            namespace=args.namespace,
            name=args.proxy_secret,
            key=key,
        )
    auth_action = ensure_auth_secret(
        core,
        client,
        namespace=args.namespace,
        name=args.auth_secret,
    )
    actions = apply_documents(core, apps, documents)
    result = wait_for_reward_pod(
        core,
        apps,
        namespace=args.namespace,
        timeout_seconds=args.wait_seconds,
    )
    print(
        json.dumps(
            {
                "ready": True,
                "auth_secret": {"name": args.auth_secret, "action": auth_action},
                "actions": actions,
                "service_url": (
                    f"http://openreward-ppt-reward.{args.namespace}.svc.cluster.local:9104"
                ),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
