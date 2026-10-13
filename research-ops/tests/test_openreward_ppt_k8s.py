from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.deploy_openreward_ppt_k8s import render_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _deployment(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(item for item in documents if item["kind"] == "Deployment")


def test_reward_k8s_manifest_keeps_credentials_in_secret_refs() -> None:
    values = {
        "NAMESPACE": "rl-training",
        "PPT_IMAGE": "registry.test/ppt:fixed",
        "IMAGE_PULL_SECRET": "registry-auth",
        "AUTH_SECRET": "ors-auth",
        "MODEL_SECRET": "model-auth",
        "MODEL_SECRET_KEY": "api-key",
        "PROXY_SECRET": "proxy-auth",
        "MODEL_ENDPOINT": "https://model.test",
        "MODEL_NAME": "judge-model",
        "RESEARCH_OPS_ROOT": "/cfs/research-ops",
        "SLIDEFORGE_ROOT": "/cfs/slideforge",
        "BUNDLE_ROOT": "/cfs/bundles",
        "ARTIFACT_ROOT": "/cfs/artifacts",
        "WORK_ROOT": "/cfs/work",
    }
    documents = render_manifest(
        PROJECT_ROOT / "deploy/openreward-ppt-k8s/reward-adapter.yaml",
        values,
    )
    deployment = _deployment(documents)
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    environment = {item["name"]: item for item in container["env"]}

    assert environment["ORS_SLIDEFORGE_REWARD_TOKEN"]["valueFrom"][
        "secretKeyRef"
    ] == {"name": "ors-auth", "key": "rubric_token"}
    assert environment["MDRW_LLM_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "model-auth",
        "key": "api-key",
    }
    assert "value" not in environment["ORS_SLIDEFORGE_REWARD_TOKEN"]
    assert "value" not in environment["MDRW_LLM_API_KEY"]
    assert environment["HTTPS_PROXY"]["valueFrom"]["secretKeyRef"] == {
        "name": "proxy-auth",
        "key": "HTTPS_PROXY",
    }
    assert "value" not in environment["HTTPS_PROXY"]
    assert environment["ORS_CCRELAY_CLAUDE_COMPAT_SCRIPT"]["value"] == (
        "/opt/research-ops/scripts/run_ccrelay_claude_compat.py"
    )
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "registry-auth"}]


def test_reward_k8s_manifest_mounts_code_and_bundles_read_only() -> None:
    values = {
        "NAMESPACE": "rl-training",
        "PPT_IMAGE": "registry.test/ppt:fixed",
        "IMAGE_PULL_SECRET": "registry-auth",
        "AUTH_SECRET": "ors-auth",
        "MODEL_SECRET": "model-auth",
        "MODEL_SECRET_KEY": "api-key",
        "PROXY_SECRET": "proxy-auth",
        "MODEL_ENDPOINT": "https://model.test",
        "MODEL_NAME": "judge-model",
        "RESEARCH_OPS_ROOT": "/cfs/research-ops",
        "SLIDEFORGE_ROOT": "/cfs/slideforge",
        "BUNDLE_ROOT": "/cfs/bundles",
        "ARTIFACT_ROOT": "/cfs/artifacts",
        "WORK_ROOT": "/cfs/work",
    }
    deployment = _deployment(
        render_manifest(
            PROJECT_ROOT / "deploy/openreward-ppt-k8s/reward-adapter.yaml",
            values,
        )
    )
    pod = deployment["spec"]["template"]["spec"]
    mounts = {
        item["name"]: item for item in pod["containers"][0]["volumeMounts"]
    }
    volumes = {item["name"]: item for item in pod["volumes"]}

    assert mounts["research-ops-code"]["readOnly"] is True
    assert mounts["slideforge-code"]["readOnly"] is True
    assert mounts["task-bundles"]["readOnly"] is True
    assert volumes["artifact-root"]["hostPath"]["type"] == "DirectoryOrCreate"
    assert volumes["reward-work"]["hostPath"]["type"] == "DirectoryOrCreate"
