from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.deploy_openreward_ors_k8s import _apply as apply_ors
from scripts.deploy_openreward_ors_k8s import render_manifest as render_ors
from scripts.deploy_openreward_tooluse_k8s import render_manifest as render_tooluse

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in documents
        if item["kind"] == kind and item["metadata"]["name"] == name
    )


def test_ors_deployment_uses_json_merge_patch_to_replace_runtime_mounts() -> None:
    deployment_patches: list[dict[str, Any]] = []

    class Core:
        read_namespaced_secret = staticmethod(
            lambda *_args, **_kwargs: SimpleNamespace(data={})
        )
        read_namespaced_service = staticmethod(lambda *_args, **_kwargs: object())
        create_namespaced_secret = staticmethod(lambda *_args, **_kwargs: None)
        create_namespaced_service = staticmethod(lambda *_args, **_kwargs: None)
        patch_namespaced_secret = staticmethod(lambda *_args, **_kwargs: None)
        patch_namespaced_service = staticmethod(lambda *_args, **_kwargs: None)

    class Apps:
        read_namespaced_deployment = staticmethod(lambda *_args, **_kwargs: object())
        create_namespaced_deployment = staticmethod(lambda *_args, **_kwargs: None)

        @staticmethod
        def patch_namespaced_deployment(*_args, **kwargs):
            deployment_patches.append(kwargs)

    class Networking:
        read_namespaced_network_policy = staticmethod(
            lambda *_args, **_kwargs: object()
        )
        create_namespaced_network_policy = staticmethod(lambda *_args, **_kwargs: None)
        patch_namespaced_network_policy = staticmethod(lambda *_args, **_kwargs: None)

    documents = [
        {
            "kind": kind,
            "metadata": {"name": name, "namespace": "rl-training", "labels": {}},
        }
        for kind, name in (
            ("Secret", "ors-auth"),
            ("Deployment", "openreward-ors"),
            ("Service", "openreward-ors"),
            ("NetworkPolicy", "openreward-ors"),
        )
    ]
    apply_ors(Core(), Apps(), Networking(), documents)

    assert deployment_patches == [
        {"_request_timeout": 30, "_content_type": "application/merge-patch+json"}
    ]


def test_tooluse_manifest_is_cluster_only_and_has_no_host_runtime_mounts() -> None:
    documents = render_tooluse(
        PROJECT_ROOT / "deploy/openreward-tooluse-k8s/deployment.yaml",
        namespace="rl-training",
        image="registry.test/toolathlon:fixed",
        image_pull_secret="registry-auth",
    )
    deployment = _resource(documents, "Deployment", "openreward-tooluse")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "registry.test/toolathlon:fixed"
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "registry-auth"}]
    assert not any("hostPath" in volume for volume in pod["volumes"])
    assert "docker.sock" not in str(pod)

    service = _resource(documents, "Service", "openreward-tooluse")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8080


def test_ors_manifest_uses_secret_refs_and_internal_runtime_urls() -> None:
    documents = render_ors(
        PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
        namespace="rl-training",
        image="registry.test/ors:fixed",
        image_pull_secret="registry-auth",
        auth_secret="ors-auth",
        api_key="not-a-production-key",
    )
    secret = _resource(documents, "Secret", "ors-auth")
    assert secret["stringData"] == {"api_key": "not-a-production-key"}

    deployment = _resource(documents, "Deployment", "openreward-ors")
    revision = deployment["spec"]["template"]["metadata"]["annotations"][
        "research-ops.code-sha256"
    ]
    assert len(revision) == 64
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item for item in container["env"]}
    assert environment["ORS_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "ors-auth",
        "key": "api_key",
    }
    assert "value" not in environment["ORS_API_KEY"]
    assert environment["ORS_PPT_RUNTIME_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "openreward-ppt-auth",
        "key": "runtime_token",
    }
    assert environment["ORS_TOOLUSE_SERVICE_URL"]["value"] == (
        "http://openreward-tooluse.rl-training.svc.cluster.local:8080"
    )
    assert environment["ORS_PPT_RUNTIME_URL"]["value"] == (
        "http://openreward-ppt-runtime.rl-training.svc.cluster.local:9103"
    )
    assert environment["ORS_SWE_SANDBOX_URL"]["value"] == (
        "http://openreward-swe-runtime.rl-training.svc.cluster.local:9101"
    )
    assert environment["ORS_SWE_SANDBOX_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "openreward-swe-auth",
        "key": "runtime_token",
    }
    command = " ".join(container["args"])
    assert "--real-domain tooluse" in command
    assert "--real-domain ppt" in command
    assert "--real-domain swe" in command
    assert command.count("--task-catalog") == 3
    mounts = {item["name"]: item for item in container["volumeMounts"]}
    assert mounts["research-ops-code"]["readOnly"] is True


def test_ors_miaoda_manifest_canonicalizes_a_bare_policy_gateway() -> None:
    documents = render_ors(
        PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
        namespace="rl-training",
        image="registry.test/ors:fixed",
        image_pull_secret="registry-auth",
        auth_secret="ors-auth",
        api_key="not-a-production-key",
        enable_miaoda=True,
        miaoda_model_name="policy-r1",
        miaoda_model_api_url="http://policy.test:14255",
        policy_capture_root="/data/example/capture",
        policy_tokenizer_path="/data/example/models/qwen36",
    )
    deployment = _resource(documents, "Deployment", "openreward-ors")
    environment = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["MOPD_POLICY_API_URL"]["value"] == (
        "http://policy.test:14255/v1/chat/completions"
    )
    assert environment["MIAODA_POLICY_CAPTURE_JOIN_TIMEOUT_SECONDS"]["value"] == "900"
    assert environment["MIAODA_DATAWHEEL_CONCURRENCY"]["value"] == "80"


def test_ors_miaoda_manifest_requires_tokenizer_for_batched_capture() -> None:
    with pytest.raises(ValueError, match="exact policy tokenizer path"):
        render_ors(
            PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
            namespace="rl-training",
            image="registry.test/ors:fixed",
            image_pull_secret="registry-auth",
            auth_secret="ors-auth",
            api_key="not-a-production-key",
            enable_miaoda=True,
            miaoda_model_name="policy-r1",
            miaoda_model_api_url="http://policy.test:14255",
            policy_capture_root="/data/example/capture",
        )


def test_ors_miaoda_manifest_can_follow_trainer_policy_route() -> None:
    capture_root = "/data/example/capture"
    route_file = f"{capture_root}/persistent-policy-route.json"
    documents = render_ors(
        PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
        namespace="rl-training",
        image="registry.test/ors:fixed",
        image_pull_secret="registry-auth",
        auth_secret="ors-auth",
        api_key="not-a-production-key",
        enable_miaoda=True,
        miaoda_model_name="policy-r1",
        miaoda_model_api_url="http://policy.test:14255",
        policy_capture_root=capture_root,
        policy_upstream_route_file=route_file,
        policy_tokenizer_path="/data/example/models/qwen36",
        policy_chat_template_path="/data/example/models/qwen36/chat_template_force_nothink.jinja",
        miaoda_datawheel_concurrency=2,
    )
    deployment = _resource(documents, "Deployment", "openreward-ors")
    environment = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert environment["MOPD_POLICY_UPSTREAM_ROUTE_FILE"]["value"] == route_file
    assert environment["MOPD_POLICY_TOKENIZER_PATH"]["value"] == (
        "/data/example/models/qwen36"
    )
    assert environment["MOPD_POLICY_CHAT_TEMPLATE_PATH"]["value"] == (
        "/data/example/models/qwen36/chat_template_force_nothink.jinja"
    )
    assert "MOPD_POLICY_UPSTREAM_URL" not in environment
    assert environment["MIAODA_SUBMISSION_BATCH_SIZE"]["value"] == "256"
    assert environment["MIAODA_EXPLICIT_BATCH_WAIT_TIMEOUT_SECONDS"]["value"] == "120"
    assert environment["MIAODA_DATAWHEEL_CONCURRENCY"]["value"] == "2"
    assert environment["MIAODA_MODEL_MAX_TOKENS"]["value"] == "32000"
    assert environment["MIAODA_MODEL_TIMEOUT_SECONDS"]["value"] == "1200"
    assert environment["MIAODA_MESSAGE_INTERVAL_TIMEOUT_SECONDS"]["value"] == "1500"
    assert environment["MIAODA_TRAJECTORY_JOIN_CONCURRENCY"]["value"] == "32"


def test_ors_miaoda_manifest_rejects_shorter_message_watchdog() -> None:
    with pytest.raises(
        ValueError,
        match="message interval timeout must exceed the model timeout",
    ):
        render_ors(
            PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
            namespace="rl-training",
            image="registry.test/ors:fixed",
            image_pull_secret="registry-auth",
            auth_secret="ors-auth",
            api_key="not-a-production-key",
            enable_miaoda=True,
            miaoda_model_name="policy-r1",
            miaoda_model_api_url="http://policy.test:14255",
            policy_capture_root="/data/example/capture",
            policy_tokenizer_path="/data/example/models/qwen36",
            miaoda_model_timeout_seconds=1200,
            miaoda_message_interval_timeout_seconds=600,
        )


def test_ors_miaoda_manifest_rejects_concurrency_above_single_instance_limit() -> None:
    with pytest.raises(
        ValueError,
        match="Datawheel concurrency must be between 1 and 80",
    ):
        render_ors(
            PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
            namespace="rl-training",
            image="registry.test/ors:fixed",
            image_pull_secret="registry-auth",
            auth_secret="ors-auth",
            api_key="not-a-production-key",
            enable_miaoda=True,
            miaoda_model_name="policy-r1",
            miaoda_model_api_url="http://policy.test:14255",
            policy_capture_root="/data/example/capture",
            policy_tokenizer_path="/data/example/models/qwen36",
            miaoda_datawheel_concurrency=81,
        )


def test_ors_miaoda_policy_route_must_share_capture_mount() -> None:
    with pytest.raises(ValueError, match="inside policy capture root"):
        render_ors(
            PROJECT_ROOT / "deploy/openreward-ors-k8s/deployment.yaml",
            namespace="rl-training",
            image="registry.test/ors:fixed",
            image_pull_secret="registry-auth",
            auth_secret="ors-auth",
            api_key="not-a-production-key",
            enable_miaoda=True,
            miaoda_model_name="policy-r1",
            miaoda_model_api_url="http://policy.test:14255",
            policy_capture_root="/data/example/capture",
            policy_upstream_route_file="/mnt/other/route.json",
            policy_tokenizer_path="/data/example/models/qwen36",
        )
