#!/usr/bin/env python3
"""Deploy the unified ORS HTTP server to Kubernetes.

The generated API key is stored directly in a Kubernetes Secret and is never
printed.  Runtime endpoint values are non-secret service DNS names; external
credentials remain Secret references in the pod spec.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml"
DEFAULT_KUBECONFIG = Path("/srv/research-ops/tools/cluster-config")
_MIAODA_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


def _canonical_miaoda_model_api_url(value: str) -> str:
    """Ensure AgentOS receives the policy proxy's complete OpenAI endpoint."""

    raw = str(value).strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        raise ValueError("Miaoda model API URL must be an absolute HTTP URL")
    path = parts.path.rstrip("/") or _MIAODA_CHAT_COMPLETIONS_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _code_revision() -> str:
    """Hash mounted ORS code/config so hostPath edits restart the pod."""

    digest = hashlib.sha256()
    roots = [
        PROJECT_ROOT / "backend/research_ops",
        PROJECT_ROOT / "config",
    ]
    files = [PROJECT_ROOT / "scripts/run_openreward_server.py"]
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".py", ".json", ".yaml", ".yml"}
        )
    for path in sorted(files):
        relative = path.relative_to(PROJECT_ROOT)
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def render_manifest(
    template: Path,
    *,
    namespace: str,
    image: str,
    image_pull_secret: str,
    auth_secret: str,
    api_key: str,
    enable_miaoda: bool = False,
    miaoda_auth_secret: str = "openreward-miaoda-auth",
    miaoda_model_name: str = "",
    miaoda_model_api_url: str = "",
    miaoda_upstream_url: str = "http://198.51.100.51:8080",
    policy_upstream_route_file: str = "",
    policy_tokenizer_path: str = "",
    policy_chat_template_path: str = "",
    policy_capture_root: str = "",
    policy_capture_join_timeout_seconds: int = 900,
    miaoda_datawheel_concurrency: int = 80,
    miaoda_model_max_tokens: int = 32_000,
    miaoda_model_timeout_seconds: int = 1_200,
    miaoda_message_interval_timeout_seconds: int = 1_500,
    host_network: bool = False,
) -> list[dict[str, Any]]:
    if enable_miaoda and (
        not str(miaoda_model_name).strip()
        or not str(miaoda_model_api_url).strip()
    ):
        raise ValueError(
            "Miaoda deployment requires --miaoda-model-name and "
            "--miaoda-model-api-url"
        )
    if enable_miaoda:
        miaoda_model_api_url = _canonical_miaoda_model_api_url(miaoda_model_api_url)
    text = template.read_text(encoding="utf-8")
    replacements = {
        "NAMESPACE": namespace,
        "IMAGE": image,
        "IMAGE_PULL_SECRET": image_pull_secret,
        "AUTH_SECRET": auth_secret,
        "API_KEY": api_key,
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT.resolve(strict=True)),
        "TOOLUSE_URL": f"http://openreward-tooluse.{namespace}.svc.cluster.local:8080",
        "PPT_URL": f"http://openreward-ppt-runtime.{namespace}.svc.cluster.local:9103",
        "SWE_URL": f"http://openreward-swe-runtime.{namespace}.svc.cluster.local:9101",
        "MIAODA_REAL_DOMAIN": "--real-domain miaoda" if enable_miaoda else "",
        "HOST_NETWORK": bool(host_network),
        "CODE_SHA256": _code_revision(),
    }
    for key, value in replacements.items():
        # This one replacement is embedded in a shell command rather than a
        # YAML scalar; keep the two CLI words separate when enabled.
        encoded = (
            str(value)
            if key in {"MIAODA_REAL_DOMAIN", "HOST_NETWORK"}
            else json.dumps(str(value), ensure_ascii=False)
        )
        text = text.replace(f"@@{key}@@", encoded)
    documents = [item for item in yaml.safe_load_all(text) if item]
    if len(documents) != 4 or {item.get("kind") for item in documents} != {
        "Secret",
        "Deployment",
        "Service",
        "NetworkPolicy",
    }:
        raise ValueError("unexpected ORS manifest resources")
    if enable_miaoda:
        deployment = next(item for item in documents if item.get("kind") == "Deployment")
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        capture_root = policy_capture_root or str(
            PROJECT_ROOT / "data" / "miaoda_online_capture"
        )
        if not Path(capture_root).is_absolute():
            raise ValueError("policy capture root must be absolute")
        route_file = str(policy_upstream_route_file).strip()
        if route_file:
            route_path = Path(route_file)
            if not route_path.is_absolute():
                raise ValueError("policy upstream route file must be absolute")
            try:
                route_path.relative_to(Path(capture_root))
            except ValueError as exc:
                raise ValueError(
                    "policy upstream route file must be inside policy capture root"
                ) from exc
        tokenizer_path = str(policy_tokenizer_path).strip()
        if not tokenizer_path:
            raise ValueError(
                "Miaoda batched policy capture requires an exact policy tokenizer path"
            )
        if not Path(tokenizer_path).is_absolute():
            raise ValueError("policy tokenizer path must be absolute")
        chat_template_path = str(policy_chat_template_path).strip()
        if chat_template_path:
            template_path = Path(chat_template_path)
            if not template_path.is_absolute():
                raise ValueError("policy chat template path must be absolute")
            try:
                template_path.relative_to(Path(tokenizer_path))
            except ValueError as exc:
                raise ValueError(
                    "policy chat template path must be inside policy tokenizer path"
                ) from exc
        if policy_capture_join_timeout_seconds < 0:
            raise ValueError("policy capture join timeout must be non-negative")
        if miaoda_datawheel_concurrency < 1 or miaoda_datawheel_concurrency > 80:
            raise ValueError(
                "Miaoda Datawheel concurrency must be between 1 and 80"
            )
        if miaoda_model_max_tokens < 1:
            raise ValueError("Miaoda model max tokens must be positive")
        if miaoda_model_timeout_seconds < 1:
            raise ValueError("Miaoda model timeout must be positive")
        if miaoda_message_interval_timeout_seconds <= miaoda_model_timeout_seconds:
            raise ValueError(
                "Miaoda message interval timeout must exceed the model timeout"
            )
        container["env"].extend(
            [
                {
                    "name": "ROPS_AGENTOS_DATAWHEEL_BASE_URL",
                    "value": "http://198.51.100.250:80/api/v1/datawheel",
                },
                {
                    "name": "DATAWHEEL_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": miaoda_auth_secret,
                            "key": "datawheel_token",
                        }
                    },
                },
                {
                    "name": "DATAWHEEL_GENERATED_USER_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": miaoda_auth_secret,
                            "key": "generated_user_id",
                        }
                    },
                },
                {"name": "MOPD_POLICY_MODEL_NAME", "value": str(miaoda_model_name).strip()},
                {"name": "MOPD_POLICY_API_URL", "value": str(miaoda_model_api_url).strip()},
                {
                    "name": "AGENTOS_MODEL_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {"name": auth_secret, "key": "api_key"}
                    },
                },
                {"name": "ROPS_GOBLIN_BASE_URL", "value": "http://198.51.100.139:8118"},
                {"name": "GOBLIN_BENCHMARK_DATASET_NAME", "value": "example-app-300"},
                {
                    "name": "GOBLIN_REWARD_API_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": miaoda_auth_secret,
                            "key": "goblin_token",
                        }
                    },
                },
                {"name": "GOBLIN_REWARD_RESULT_ROOT", "value": f"{capture_root}/goblin"},
                {"name": "MIAODA_ARTIFACT_DIR", "value": f"{capture_root}/artifacts"},
                {"name": "MOPD_POLICY_CAPTURE_DIR", "value": capture_root},
                {"name": "MOPD_POLICY_PROXY_PREFIX", "value": "/policy"},
                {"name": "MIAODA_SUBMISSION_BATCH_SIZE", "value": "256"},
                {"name": "MIAODA_SUBMISSION_BATCH_WINDOW_SECONDS", "value": "2"},
                {
                    "name": "MIAODA_EXPLICIT_BATCH_WAIT_TIMEOUT_SECONDS",
                    "value": "120",
                },
                {
                    "name": "MIAODA_DATAWHEEL_CONCURRENCY",
                    "value": str(miaoda_datawheel_concurrency),
                },
                {
                    "name": "MIAODA_MODEL_MAX_TOKENS",
                    "value": str(miaoda_model_max_tokens),
                },
                {
                    "name": "MIAODA_MODEL_TIMEOUT_SECONDS",
                    "value": str(miaoda_model_timeout_seconds),
                },
                {
                    "name": "MIAODA_MESSAGE_INTERVAL_TIMEOUT_SECONDS",
                    "value": str(miaoda_message_interval_timeout_seconds),
                },
                {"name": "MIAODA_DATAWHEEL_POLL_CACHE_SECONDS", "value": "2"},
                {
                    "name": "MIAODA_POLICY_CAPTURE_JOIN_TIMEOUT_SECONDS",
                    "value": str(policy_capture_join_timeout_seconds),
                },
                {"name": "MIAODA_TRAJECTORY_JOIN_CONCURRENCY", "value": "32"},
            ]
        )
        container["env"].append(
            {
                "name": (
                    "MOPD_POLICY_UPSTREAM_ROUTE_FILE"
                    if route_file
                    else "MOPD_POLICY_UPSTREAM_URL"
                ),
                "value": route_file or str(miaoda_upstream_url).strip(),
            }
        )
        if tokenizer_path:
            container["env"].append(
                {"name": "MOPD_POLICY_TOKENIZER_PATH", "value": tokenizer_path}
            )
            if chat_template_path:
                container["env"].append(
                    {
                        "name": "MOPD_POLICY_CHAT_TEMPLATE_PATH",
                        "value": chat_template_path,
                    }
                )
            container["volumeMounts"].append(
                {
                    "name": "policy-tokenizer",
                    "mountPath": tokenizer_path,
                    "readOnly": True,
                }
            )
            pod_spec["volumes"].append(
                {
                    "name": "policy-tokenizer",
                    "hostPath": {"path": tokenizer_path, "type": "Directory"},
                }
            )
        container["volumeMounts"].append(
            {"name": "policy-capture", "mountPath": capture_root}
        )
        pod_spec["volumes"].append(
            {
                "name": "policy-capture",
                "hostPath": {"path": capture_root, "type": "DirectoryOrCreate"},
            }
        )
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
        "Secret": (
            core.read_namespaced_secret,
            core.create_namespaced_secret,
            core.patch_namespaced_secret,
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
        "NetworkPolicy": (
            networking.read_namespaced_network_policy,
            networking.create_namespaced_network_policy,
            networking.patch_namespaced_network_policy,
        ),
    }
    actions: list[str] = []
    for body in documents:
        kind = str(body["kind"])
        name = str(body["metadata"]["name"])
        namespace = str(body["metadata"]["namespace"])
        read, create, patch = methods[kind]
        try:
            existing = read(name, namespace, _request_timeout=30)
        except Exception as exc:
            if _status(exc) != 404:
                raise
            create(namespace, body, _request_timeout=30)
            actions.append(f"{kind}/{name}=created")
            continue
        if kind == "Secret" and existing.data and existing.data.get("api_key"):
            # Never overwrite a live API key on a repeated deployment.
            body = {"metadata": {"labels": body["metadata"].get("labels", {})}}
        patch_kwargs: dict[str, Any] = {"_request_timeout": 30}
        if kind == "Deployment":
            # Strategic merge keys volumeMounts by mountPath. Repeated ORS
            # deployments intentionally change the tokenizer/capture paths,
            # so strategic patching retains every historical mount. JSON
            # merge patch replaces those arrays with the rendered manifest.
            patch_kwargs["_content_type"] = "application/merge-patch+json"
        patch(name, namespace, body, **patch_kwargs)
        actions.append(f"{kind}/{name}=patched")
    return actions


def wait_ready(apps: Any, core: Any, *, namespace: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        deployment = apps.read_namespaced_deployment(
            "openreward-ors", namespace, _request_timeout=30
        )
        pods = core.list_namespaced_pod(
            namespace,
            label_selector="app.kubernetes.io/name=openreward-ors",
            _request_timeout=30,
        ).items
        generation = deployment.metadata.generation or 0
        observed_generation = deployment.status.observed_generation or 0
        desired_revision = (
            deployment.spec.template.metadata.annotations or {}
        ).get("research-ops.code-sha256", "")
        latest = {
            "generation": generation,
            "observed_generation": observed_generation,
            "updated_replicas": deployment.status.updated_replicas or 0,
            "ready_replicas": deployment.status.ready_replicas or 0,
            "replicas": deployment.spec.replicas or 0,
            "pods": [
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": all(item.ready for item in (pod.status.container_statuses or [])),
                    "revision_matches": (
                        not desired_revision
                        or (pod.metadata.annotations or {}).get(
                            "research-ops.code-sha256"
                        )
                        == desired_revision
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
        matching_ready = any(
            item["ready"] and item["revision_matches"] for item in latest["pods"]
        )
        if (
            observed_generation >= generation
            and (deployment.status.updated_replicas or 0) >= 1
            and (deployment.status.ready_replicas or 0) >= 1
            and matching_ready
        ):
            return latest
        time.sleep(3)
    raise RuntimeError(f"ORS deployment did not become ready: {json.dumps(latest)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--image", default="registry.example.com/acg-agi/ppt-rollout:0610-deps")
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-ors-auth")
    parser.add_argument("--enable-miaoda", action="store_true")
    parser.add_argument("--miaoda-auth-secret", default="openreward-miaoda-auth")
    parser.add_argument("--miaoda-model-name", default="")
    parser.add_argument("--miaoda-model-api-url", default="")
    parser.add_argument("--miaoda-upstream-url", default="http://198.51.100.51:8080")
    parser.add_argument("--policy-upstream-route-file", default="")
    parser.add_argument("--policy-tokenizer-path", default="")
    parser.add_argument("--policy-chat-template-path", default="")
    parser.add_argument("--policy-capture-root", default="")
    parser.add_argument(
        "--miaoda-policy-capture-join-timeout-seconds",
        type=int,
        default=900,
    )
    parser.add_argument("--miaoda-datawheel-concurrency", type=int, default=80)
    parser.add_argument("--miaoda-model-max-tokens", type=int, default=32_000)
    parser.add_argument("--miaoda-model-timeout-seconds", type=int, default=1_200)
    parser.add_argument(
        "--miaoda-message-interval-timeout-seconds",
        type=int,
        default=1_500,
    )
    parser.add_argument(
        "--host-network",
        action="store_true",
        help="run ORS with host networking so it can reach a cluster-d policy endpoint",
    )
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    api_key = secrets.token_urlsafe(48)
    documents = render_manifest(
        args.template,
        namespace=args.namespace,
        image=args.image,
        image_pull_secret=args.image_pull_secret,
        auth_secret=args.auth_secret,
        api_key=api_key,
        enable_miaoda=args.enable_miaoda,
        miaoda_auth_secret=args.miaoda_auth_secret,
        miaoda_model_name=args.miaoda_model_name,
        miaoda_model_api_url=args.miaoda_model_api_url,
        miaoda_upstream_url=args.miaoda_upstream_url,
        policy_upstream_route_file=args.policy_upstream_route_file,
        policy_tokenizer_path=args.policy_tokenizer_path,
        policy_chat_template_path=args.policy_chat_template_path,
        policy_capture_root=args.policy_capture_root,
        policy_capture_join_timeout_seconds=(
            args.miaoda_policy_capture_join_timeout_seconds
        ),
        miaoda_datawheel_concurrency=args.miaoda_datawheel_concurrency,
        miaoda_model_max_tokens=args.miaoda_model_max_tokens,
        miaoda_model_timeout_seconds=args.miaoda_model_timeout_seconds,
        miaoda_message_interval_timeout_seconds=(
            args.miaoda_message_interval_timeout_seconds
        ),
        host_network=args.host_network,
    )
    if args.dry_run:
        print(json.dumps({"ready": True, "resources": [item["kind"] for item in documents]}))
        return 0
    core, apps, networking = _clients(args.kubeconfig.expanduser().resolve(strict=True))
    core.read_namespaced_secret(args.image_pull_secret, args.namespace, _request_timeout=30)
    if args.enable_miaoda:
        core.read_namespaced_secret(args.miaoda_auth_secret, args.namespace, _request_timeout=30)
    actions = _apply(core, apps, networking, documents)
    result = wait_ready(apps, core, namespace=args.namespace, timeout_seconds=args.wait_seconds)
    print(
        json.dumps(
            {
                "ready": True,
                "actions": actions,
                "service_url": (
                    f"http://openreward-ors.{args.namespace}.svc.cluster.local:8022"
                ),
                "auth_secret": args.auth_secret,
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
