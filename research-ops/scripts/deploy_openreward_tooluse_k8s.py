#!/usr/bin/env python3
"""Deploy the Toolathlon-Gym OpenReward service to a Kubernetes namespace.

The script only manages public deployment metadata.  Registry credentials stay
in the existing image-pull Secret and are never read or printed.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-tooluse-k8s/deployment.yaml"
DEFAULT_IMAGE = "registry.example.com/acg-agi/openreward-toolathlon:9c82859d-canvasfix3"
DEFAULT_KUBECONFIG = Path("/srv/research-ops/tools/cluster-config")
_PLACEHOLDER = re.compile(r"@@[A-Z0-9_]+@@")


def render_manifest(
    template: Path,
    *,
    namespace: str,
    image: str,
    image_pull_secret: str,
) -> list[dict[str, Any]]:
    text = template.read_text(encoding="utf-8")
    replacements = {
        "NAMESPACE": namespace,
        "IMAGE": image,
        "IMAGE_PULL_SECRET": image_pull_secret,
    }
    for key, value in replacements.items():
        text = text.replace(f"@@{key}@@", json.dumps(str(value)))
    unresolved = sorted(set(_PLACEHOLDER.findall(text)))
    if unresolved:
        raise ValueError(f"manifest has unresolved placeholders: {', '.join(unresolved)}")
    documents = [item for item in yaml.safe_load_all(text) if item]
    kinds = {str(item.get("kind")) for item in documents}
    if kinds != {"Deployment", "Service", "NetworkPolicy"}:
        raise ValueError(f"unexpected ToolUse manifest resources: {sorted(kinds)}")
    return documents


def _clients(kubeconfig: Path) -> tuple[Any, Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("install the Kubernetes Python client") from exc
    configuration = client.Configuration()
    config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    configuration.proxy = None
    api = client.ApiClient(configuration)
    return client.CoreV1Api(api), client.AppsV1Api(api), client.NetworkingV1Api(api)


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def _apply(core: Any, apps: Any, networking: Any, documents: list[dict[str, Any]]) -> list[str]:
    methods = {
        "Deployment": (
            apps.read_namespaced_deployment,
            apps.create_namespaced_deployment,
            apps.patch_namespaced_deployment,
        ),
        "Service": (
            core.read_namespaced_service,
            core.create_namespaced_service,
            core.patch_namespaced_service,
        ),
        "NetworkPolicy": (
            networking.read_namespaced_network_policy,
            networking.create_namespaced_network_policy,
            networking.patch_namespaced_network_policy,
        ),
    }
    result: list[str] = []
    for body in documents:
        kind = str(body["kind"])
        name = str(body["metadata"]["name"])
        namespace = str(body["metadata"]["namespace"])
        read, create, patch = methods[kind]
        try:
            read(name, namespace, _request_timeout=30)
        except Exception as exc:
            if _status(exc) != 404:
                raise
            create(namespace, body, _request_timeout=30)
            result.append(f"{kind}/{name}=created")
        else:
            patch(name, namespace, body, _request_timeout=30)
            result.append(f"{kind}/{name}=patched")
    return result


def wait_ready(apps: Any, core: Any, *, namespace: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(
            "openreward-tooluse", namespace, _request_timeout=30
        )
        pods = core.list_namespaced_pod(
            namespace,
            label_selector="app.kubernetes.io/name=openreward-tooluse",
            _request_timeout=30,
        ).items
        latest = {
            "ready_replicas": deployment.status.ready_replicas or 0,
            "replicas": deployment.spec.replicas or 0,
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": all(item.ready for item in (pod.status.container_statuses or [])),
                    "waiting": [
                        item.state.waiting.reason
                        for item in (pod.status.container_statuses or [])
                        if item.state and item.state.waiting
                    ],
                }
                for pod in pods
            ],
        }
        if (deployment.status.ready_replicas or 0) >= 1:
            return latest
        time.sleep(3)
    raise RuntimeError(f"ToolUse deployment did not become ready: {json.dumps(latest)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    documents = render_manifest(
        args.template,
        namespace=args.namespace,
        image=args.image,
        image_pull_secret=args.image_pull_secret,
    )
    if args.dry_run:
        print(json.dumps({"ready": True, "resources": [item["kind"] for item in documents]}))
        return 0
    core, apps, networking = _clients(args.kubeconfig.expanduser().resolve(strict=True))
    core.read_namespaced_secret(args.image_pull_secret, args.namespace, _request_timeout=30)
    actions = _apply(core, apps, networking, documents)
    result = wait_ready(apps, core, namespace=args.namespace, timeout_seconds=args.wait_seconds)
    print(
        json.dumps(
            {
                "ready": True,
                "actions": actions,
                "service_url": (
                    f"http://openreward-tooluse.{args.namespace}.svc.cluster.local:8080"
                ),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
