#!/usr/bin/env python3
"""Deploy the Kubernetes-native SWE-bench runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-swe-k8s/deployment.yaml"
DEFAULT_KUBECONFIG = Path("/srv/research-ops/tools/cluster-config")
DEFAULT_HARNESS = Path("/srv/research-ops/tools/SWE-bench")
DEFAULT_WORK_ROOT = PROJECT_ROOT / "data/openreward_swe_k8s/work"
DEFAULT_IMAGE = "registry.example.com/acg-agi/openreward-swe-runtime:20260722-k8s-slim"
DEFAULT_IMAGE_PREFIX = "registry.example.com/acg-agi/swebench"
DEFAULT_SWEGYM_IMAGE_PREFIX = "registry.example.com/acg-agi/swegym"
DEFAULT_HARNESS_FAMILY = "swebench"
DEFAULT_HARNESS_REVISION = "4b6773064a070c689874d30171e78a3f429a3fbf"


def _code_revision() -> str:
    """Hash mounted SWE runtime code so source edits restart the Deployment."""

    files = (
        PROJECT_ROOT / "backend/research_ops/openreward/swe_runtime.py",
        PROJECT_ROOT / "backend/research_ops/openreward/swe_k8s_runtime.py",
        PROJECT_ROOT / "scripts/run_openreward_swe_runtime.py",
        PROJECT_ROOT / "deploy/openreward-swe-k8s/deployment.yaml",
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _directory(
    path: Path,
    *,
    create: bool = False,
    allow_missing: bool = False,
) -> Path:
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if allow_missing and not path.exists():
        return path.resolve(strict=False)
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"path is not a directory: {resolved}")
    return resolved


def render_manifest(template: Path, args: argparse.Namespace) -> list[dict[str, Any]]:
    host_root = _directory(args.host_root)
    work_root = _directory(
        args.work_root,
        create=not args.dry_run,
        allow_missing=args.dry_run,
    )
    harness = _directory(args.harness_root)
    if not work_root.is_relative_to(host_root):
        raise ValueError("work root must be below host root")
    text = template.read_text(encoding="utf-8")
    replacements = {
        "NAMESPACE": args.namespace,
        "IMAGE": args.image,
        "IMAGE_PULL_SECRET": args.image_pull_secret,
        "AUTH_SECRET": args.auth_secret,
        "HOST_ROOT": str(host_root),
        "WORK_ROOT": str(work_root),
        "HARNESS_HOST_ROOT": str(harness),
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT.resolve(strict=True)),
        "IMAGE_PREFIX": args.image_prefix,
        "SWEGYM_IMAGE_PREFIX": getattr(
            args, "swegym_image_prefix", DEFAULT_SWEGYM_IMAGE_PREFIX
        ),
        "HARNESS_FAMILY": getattr(args, "harness_family", DEFAULT_HARNESS_FAMILY),
        "HARNESS_REVISION": getattr(
            args, "harness_revision", DEFAULT_HARNESS_REVISION
        ),
        "CODE_SHA256": _code_revision(),
        "IMAGE_MAP_PATH": getattr(args, "image_map_path", ""),
    }
    for key, value in replacements.items():
        text = text.replace(f"@@{key}@@", json.dumps(str(value), ensure_ascii=False))
    documents = [item for item in yaml.safe_load_all(text) if item]
    expected = {
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "NetworkPolicy",
        "Deployment",
        "Service",
    }
    if {item.get("kind") for item in documents} != expected or len(documents) != 6:
        raise ValueError("unexpected SWE K8s manifest resources")
    return documents


def _clients(kubeconfig: Path) -> tuple[Any, Any, Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install the Kubernetes Python client") from exc
    configuration = client.Configuration()
    config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
    configuration.proxy = None
    api = client.ApiClient(configuration)
    return (
        client.CoreV1Api(api),
        client.AppsV1Api(api),
        client.RbacAuthorizationV1Api(api),
        client.NetworkingV1Api(api),
    )


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def _ensure_auth_secret(core: Any, client: Any, *, namespace: str, name: str) -> None:
    try:
        existing = core.read_namespaced_secret(name, namespace, _request_timeout=30)
    except Exception as exc:
        if _status(exc) != 404:
            raise
        token = secrets.token_urlsafe(48)
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/name": "openreward-swe-runtime",
                    "app.kubernetes.io/part-of": "research-ops",
                    "research-ops.work-id": "harn-0001",
                },
            ),
            type="Opaque",
            data={"runtime_token": base64.b64encode(token.encode()).decode()},
        )
        core.create_namespaced_secret(namespace, body, _request_timeout=30)
        return
    if "runtime_token" not in (existing.data or {}):
        core.patch_namespaced_secret(
            name,
            namespace,
            {
                "data": {
                    "runtime_token": base64.b64encode(secrets.token_urlsafe(48).encode()).decode()
                }
            },
            _request_timeout=30,
        )


def _apply(
    core: Any,
    apps: Any,
    rbac: Any,
    networking: Any,
    documents: list[dict[str, Any]],
) -> list[str]:
    methods = {
        "ServiceAccount": (
            core.read_namespaced_service_account,
            core.create_namespaced_service_account,
            core.patch_namespaced_service_account,
        ),
        "Role": (
            rbac.read_namespaced_role,
            rbac.create_namespaced_role,
            rbac.patch_namespaced_role,
        ),
        "RoleBinding": (
            rbac.read_namespaced_role_binding,
            rbac.create_namespaced_role_binding,
            rbac.patch_namespaced_role_binding,
        ),
        "NetworkPolicy": (
            networking.read_namespaced_network_policy,
            networking.create_namespaced_network_policy,
            networking.patch_namespaced_network_policy,
        ),
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
    }
    actions: list[str] = []
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
            actions.append(f"{kind}/{name}=created")
        else:
            patch(name, namespace, body, _request_timeout=30)
            actions.append(f"{kind}/{name}=patched")
    return actions


def _wait_ready(apps: Any, core: Any, *, namespace: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(
            "openreward-swe-runtime", namespace, _request_timeout=30
        )
        pods = core.list_namespaced_pod(
            namespace,
            label_selector="app.kubernetes.io/name=openreward-swe-runtime",
            _request_timeout=30,
        ).items
        latest = {
            "generation": deployment.metadata.generation or 0,
            "observed_generation": deployment.status.observed_generation or 0,
            "updated_replicas": deployment.status.updated_replicas or 0,
            "ready_replicas": deployment.status.ready_replicas or 0,
            "replicas": deployment.spec.replicas or 0,
            "code_sha256": (
                deployment.spec.template.metadata.annotations or {}
            ).get("research-ops.code-sha256", ""),
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": all(
                        item.ready for item in (pod.status.container_statuses or [])
                    ),
                    "code_matches": (
                        (pod.metadata.annotations or {}).get("research-ops.code-sha256")
                        == (deployment.spec.template.metadata.annotations or {}).get(
                            "research-ops.code-sha256"
                        )
                    ),
                    "waiting": [
                        item.state.waiting.reason
                        for item in (pod.status.container_statuses or [])
                        if item.state and item.state.waiting
                    ],
                }
                for pod in pods
            ],
        }
        if (
            (deployment.status.observed_generation or 0)
            >= (deployment.metadata.generation or 0)
            and (deployment.status.updated_replicas or 0) >= 1
            and (deployment.status.ready_replicas or 0) >= 1
            and any(item["ready"] and item["code_matches"] for item in latest["pods"])
        ):
            return latest
        time.sleep(3)
    raise RuntimeError(f"SWE runtime did not become ready: {json.dumps(latest)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--host-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--harness-root", type=Path, default=DEFAULT_HARNESS)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-prefix", default=DEFAULT_IMAGE_PREFIX)
    parser.add_argument("--swegym-image-prefix", default=DEFAULT_SWEGYM_IMAGE_PREFIX)
    parser.add_argument(
        "--harness-family",
        choices=("swebench", "swegym", "r2e"),
        default=DEFAULT_HARNESS_FAMILY,
    )
    parser.add_argument("--harness-revision", default=DEFAULT_HARNESS_REVISION)
    parser.add_argument(
        "--image-map-path",
        default="",
        help="optional JSON canonical-image to internal-image map",
    )
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-swe-auth")
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    documents = render_manifest(args.template, args)
    if args.dry_run:
        print(json.dumps({"ready": True, "resources": [item["kind"] for item in documents]}))
        return 0
    core, apps, rbac, networking = _clients(args.kubeconfig.expanduser().resolve(strict=True))
    from kubernetes import client as kubernetes_client

    core.read_namespaced_secret(args.image_pull_secret, args.namespace, _request_timeout=30)
    _ensure_auth_secret(
        core,
        kubernetes_client,
        namespace=args.namespace,
        name=args.auth_secret,
    )
    actions = _apply(core, apps, rbac, networking, documents)
    result = _wait_ready(apps, core, namespace=args.namespace, timeout=args.wait_seconds)
    print(
        json.dumps(
            {
                "ready": True,
                "actions": actions,
                "service_url": (
                    f"http://openreward-swe-runtime.{args.namespace}.svc.cluster.local:9101"
                ),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
