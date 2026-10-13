#!/usr/bin/env python3
"""Deploy the Kubernetes-native OpenReward PPT runtime and its least-privilege RBAC."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.deploy_openreward_ppt_k8s import (
        DEFAULT_ARTIFACT_ROOT,
        DEFAULT_BUNDLE_ROOT,
        DEFAULT_IMAGE,
        PROJECT_ROOT,
        _absolute_directory,
        _status,
        ensure_reference_secret,
        render_documents,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ rather than the repo on sys.path.
    from deploy_openreward_ppt_k8s import (  # type: ignore[no-redef]
        DEFAULT_ARTIFACT_ROOT,
        DEFAULT_BUNDLE_ROOT,
        DEFAULT_IMAGE,
        PROJECT_ROOT,
        _absolute_directory,
        _status,
        ensure_reference_secret,
        render_documents,
    )

DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime.yaml"
DEFAULT_HOST_ROOT = PROJECT_ROOT / "data"
DEFAULT_WORK_ROOT = PROJECT_ROOT / "data/openreward_ppt_k8s/runtime-work"
DEFAULT_WHEELHOUSE_ROOT = PROJECT_ROOT / "data/openreward_ppt_k8s/runtime-wheelhouse"
DEFAULT_NODE_BINARY = PROJECT_ROOT / "data/openreward_ppt_k8s/runtime-tools/node"
_WHEEL_REQUIREMENTS = {
    "aiohappyeyeballs": "2.7.1",
    "aiohttp": "3.14.2",
    "aiosignal": "1.4.0",
    "attrs": "26.1.0",
    "certifi": "2026.7.22",
    "charset-normalizer": "3.4.9",
    "durationpy": "0.10",
    "frozenlist": "1.8.0",
    "idna": "3.18",
    "kubernetes": "36.0.3",
    "multidict": "6.7.1",
    "oauthlib": "3.3.1",
    "propcache": "0.5.2",
    "python-dateutil": "2.9.0.post0",
    "pyyaml": "6.0.3",
    "requests": "2.34.2",
    "requests-oauthlib": "2.0.0",
    "six": "1.17.0",
    "typing-extensions": "4.15.0",
    "urllib3": "2.7.0",
    "websocket-client": "1.9.0",
    "yarl": "1.24.5",
}


def _absolute_executable(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError("node binary path must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("node binary must be an executable file")
    return resolved


def validate_wheelhouse(path: Path) -> Path:
    root = _absolute_directory(path)
    names = {item.name.casefold() for item in root.glob("*.whl") if item.is_file()}
    missing = []
    for distribution, version in _WHEEL_REQUIREMENTS.items():
        prefix = f"{distribution.replace('-', '_')}-{version}-".casefold()
        if not any(name.startswith(prefix) for name in names):
            missing.append(f"{distribution}=={version}")
    if missing:
        raise ValueError(f"runtime wheelhouse is incomplete: {', '.join(missing)}")
    return root


def _ensure_below(root: Path, paths: dict[str, Path]) -> None:
    outside = [name for name, path in paths.items() if not path.is_relative_to(root)]
    if outside:
        raise ValueError(f"runtime paths outside host root: {', '.join(sorted(outside))}")


def _asset_sha256(wheelhouse: Path, node_binary: Path) -> str:
    digest = hashlib.sha256()
    paths = [*sorted(wheelhouse.glob("*.whl")), node_binary]
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_replacements(
    args: argparse.Namespace,
    *,
    kube_api_cidr: str,
    reward_service_cidr: str,
) -> dict[str, str]:
    host_root = _absolute_directory(args.host_root)
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
    wheelhouse_root = validate_wheelhouse(args.wheelhouse_root)
    node_binary = _absolute_executable(args.node_binary)
    _ensure_below(
        host_root,
        {
            "artifact_root": artifact_root,
            "bundle_root": bundle_root,
            "node_binary": node_binary,
            "wheelhouse_root": wheelhouse_root,
            "work_root": work_root,
        },
    )
    # Parse these before placing them into a NetworkPolicy. This prevents a typo
    # from silently broadening runtime egress.
    kube_api_cidr = str(ipaddress.ip_network(kube_api_cidr, strict=True))
    reward_service_cidr = str(ipaddress.ip_network(reward_service_cidr, strict=True))
    return {
        "NAMESPACE": args.namespace,
        "PPT_IMAGE": args.image,
        "IMAGE_PULL_SECRET": args.image_pull_secret,
        "AUTH_SECRET": args.auth_secret,
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT.resolve(strict=True)),
        "HOST_ROOT": str(host_root),
        "BUNDLE_ROOT": str(bundle_root),
        "ARTIFACT_ROOT": str(artifact_root),
        "WORK_ROOT": str(work_root),
        "WHEELHOUSE_ROOT": str(wheelhouse_root),
        "NODE_BINARY": str(node_binary),
        "NODE_TOOLS_ROOT": str(node_binary.parent),
        "RUNTIME_ASSET_SHA256": _asset_sha256(wheelhouse_root, node_binary),
        "REWARD_URL": args.reward_url
        or f"http://openreward-ppt-reward.{args.namespace}.svc.cluster.local:9104",
        "KUBE_API_CIDR": kube_api_cidr,
        "REWARD_SERVICE_CIDR": reward_service_cidr,
    }


def render_manifest(template: Path, replacements: dict[str, str]) -> list[dict[str, Any]]:
    documents = render_documents(template, replacements)
    expected = Counter(
        {
            "ServiceAccount": 1,
            "Role": 1,
            "RoleBinding": 1,
            "NetworkPolicy": 2,
            "Deployment": 1,
            "Service": 1,
        }
    )
    kinds = Counter(str(item.get("kind")) for item in documents)
    if kinds != expected:
        raise ValueError(f"runtime manifest resources are invalid: {dict(kinds)}")
    return documents


def _clients(kubeconfig: Path) -> tuple[Any, Any, Any, Any]:
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
    return (
        client.CoreV1Api(api_client),
        client.AppsV1Api(api_client),
        client.RbacAuthorizationV1Api(api_client),
        client.NetworkingV1Api(api_client),
    )


def _service_cidr(core: Any, *, namespace: str, name: str) -> str:
    service = core.read_namespaced_service(name, namespace, _request_timeout=30)
    raw = str(service.spec.cluster_ip or "").strip()
    if not raw or raw.casefold() == "none":
        raise RuntimeError(f"Kubernetes Service {namespace}/{name} has no ClusterIP")
    address = ipaddress.ip_address(raw)
    return f"{address}/{address.max_prefixlen}"


def _incluster_api_cidr(core: Any, *, namespace: str) -> str:
    """Read the API Service IP injected into an existing namespace-local Pod."""

    try:
        from kubernetes.stream import stream
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("install the ppt-k8s extra before deploying") from exc
    pods = core.list_namespaced_pod(
        namespace,
        label_selector="app.kubernetes.io/name=openreward-ppt-reward",
        _request_timeout=30,
    ).items
    pod = next((item for item in pods if item.status.phase == "Running"), None)
    if pod is None or not pod.spec.containers:
        raise RuntimeError(
            "cannot discover the in-cluster API IP; pass --kube-api-cidr explicitly"
        )
    value = stream(
        core.connect_get_namespaced_pod_exec,
        pod.metadata.name,
        namespace,
        container=pod.spec.containers[0].name,
        command=["/bin/sh", "-c", 'printf %s "$KUBERNETES_SERVICE_HOST"'],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _request_timeout=30,
    )
    address = ipaddress.ip_address(str(value).strip())
    return f"{address}/{address.max_prefixlen}"


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
        kind = body["kind"]
        name = body["metadata"]["name"]
        namespace = body["metadata"]["namespace"]
        read, create, patch = methods[kind]
        try:
            read(name, namespace, _request_timeout=30)
        except Exception as exc:
            if _status(exc) != 404:
                raise
            create(namespace, body, _request_timeout=30)
            action = "created"
        else:
            patch(name, namespace, body, _request_timeout=30)
            action = "patched"
        actions.append(f"{kind}/{name}={action}")
    return actions


def _container_state(status: Any) -> dict[str, Any]:
    state = getattr(status, "state", None)
    waiting = getattr(state, "waiting", None) if state is not None else None
    terminated = getattr(state, "terminated", None) if state is not None else None
    return {
        "name": getattr(status, "name", None),
        "ready": bool(getattr(status, "ready", False)),
        "waiting": getattr(waiting, "reason", None),
        "exit_code": getattr(terminated, "exit_code", None),
        "image_id": getattr(status, "image_id", None),
    }


def wait_for_runtime(
    core: Any,
    apps: Any,
    *,
    namespace: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(
            "openreward-ppt-runtime",
            namespace,
            _request_timeout=30,
        )
        pods = core.list_namespaced_pod(
            namespace,
            label_selector="app.kubernetes.io/name=openreward-ppt-runtime",
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
                    "init": [
                        _container_state(item)
                        for item in (pod.status.init_container_statuses or [])
                    ],
                    "containers": [
                        _container_state(item)
                        for item in (pod.status.container_statuses or [])
                    ],
                }
                for pod in pods
            ],
        }
        for pod in pods:
            statuses = [
                *(pod.status.init_container_statuses or []),
                *(pod.status.container_statuses or []),
            ]
            failed = next(
                (
                    item
                    for item in statuses
                    if getattr(getattr(item.state, "terminated", None), "exit_code", 0)
                    not in {None, 0}
                ),
                None,
            )
            if failed is not None:
                logs = core.read_namespaced_pod_log(
                    pod.metadata.name,
                    namespace,
                    container=failed.name,
                    tail_lines=120,
                    _request_timeout=30,
                )
                raise RuntimeError(f"PPT runtime container {failed.name} failed: {logs}")
        if (
            deployment.status.observed_generation == deployment.metadata.generation
            and (deployment.status.ready_replicas or 0) >= 1
        ):
            return latest
        time.sleep(3)
    raise RuntimeError(f"PPT runtime did not become ready: {json.dumps(latest)}")


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
    parser.add_argument("--host-root", type=Path, default=DEFAULT_HOST_ROOT)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--wheelhouse-root", type=Path, default=DEFAULT_WHEELHOUSE_ROOT)
    parser.add_argument("--node-binary", type=Path, default=DEFAULT_NODE_BINARY)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-ppt-auth")
    parser.add_argument("--reward-url")
    parser.add_argument("--kube-api-cidr")
    parser.add_argument("--reward-service-cidr")
    parser.add_argument("--wait-seconds", type=int, default=1200)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        replacements = build_replacements(
            args,
            kube_api_cidr=args.kube_api_cidr or "127.0.0.1/32",
            reward_service_cidr=args.reward_service_cidr or "127.0.0.2/32",
        )
        documents = render_manifest(args.template, replacements)
        print(json.dumps({"ready": True, "resources": [item["kind"] for item in documents]}))
        return 0

    kubeconfig = args.kubeconfig.expanduser().resolve(strict=True)
    core, apps, rbac, networking = _clients(kubeconfig)
    ensure_reference_secret(
        core,
        namespace=args.namespace,
        name=args.auth_secret,
        key="runtime_token",
    )
    ensure_reference_secret(
        core,
        namespace=args.namespace,
        name=args.auth_secret,
        key="rubric_token",
    )
    core.read_namespaced_secret(args.image_pull_secret, args.namespace, _request_timeout=30)
    if args.kube_api_cidr:
        kube_api_cidr = args.kube_api_cidr
    else:
        try:
            kube_api_cidr = _service_cidr(
                core,
                namespace="default",
                name="kubernetes",
            )
        except Exception as exc:
            if _status(exc) != 403:
                raise
            kube_api_cidr = _incluster_api_cidr(core, namespace=args.namespace)
    reward_service_cidr = args.reward_service_cidr or _service_cidr(
        core,
        namespace=args.namespace,
        name="openreward-ppt-reward",
    )
    replacements = build_replacements(
        args,
        kube_api_cidr=kube_api_cidr,
        reward_service_cidr=reward_service_cidr,
    )
    documents = render_manifest(args.template, replacements)
    actions = _apply(core, apps, rbac, networking, documents)
    result = wait_for_runtime(
        core,
        apps,
        namespace=args.namespace,
        timeout_seconds=args.wait_seconds,
    )
    print(
        json.dumps(
            {
                "ready": True,
                "actions": actions,
                "service_url": (
                    f"http://openreward-ppt-runtime.{args.namespace}.svc.cluster.local:9103"
                ),
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
