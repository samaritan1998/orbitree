from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.deploy_openreward_ppt_k8s import render_documents
from scripts.deploy_openreward_ppt_k8s_runtime import (
    _WHEEL_REQUIREMENTS,
    build_replacements,
    render_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(
        item
        for item in documents
        if item["kind"] == kind and item["metadata"]["name"] == name
    )


def _arguments(tmp_path: Path) -> SimpleNamespace:
    host_root = tmp_path / "data"
    bundle_root = host_root / "bundles"
    artifact_root = host_root / "artifacts"
    work_root = host_root / "work"
    wheelhouse_root = host_root / "wheelhouse"
    node_binary = host_root / "tools" / "node"
    for path in (bundle_root, artifact_root, work_root, wheelhouse_root):
        path.mkdir(parents=True)
    node_binary.parent.mkdir(parents=True)
    node_binary.write_bytes(b"node")
    node_binary.chmod(0o755)
    for distribution, version in _WHEEL_REQUIREMENTS.items():
        name = distribution.replace("-", "_")
        (wheelhouse_root / f"{name}-{version}-py3-none-any.whl").touch()
    return SimpleNamespace(
        namespace="rl-training",
        image="registry.test/ppt:fixed",
        image_pull_secret="registry-auth",
        auth_secret="ors-auth",
        host_root=host_root,
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        work_root=work_root,
        wheelhouse_root=wheelhouse_root,
        node_binary=node_binary,
        reward_url=None,
        dry_run=False,
    )


def _documents(tmp_path: Path) -> list[dict[str, Any]]:
    args = _arguments(tmp_path)
    replacements = build_replacements(
        args,
        kube_api_cidr="192.0.2.1/32",
        reward_service_cidr="192.0.2.42/32",
    )
    return render_manifest(
        PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime.yaml",
        replacements,
    )


def test_runtime_manifest_uses_offline_dependency_bootstrap_and_secret_refs(
    tmp_path: Path,
) -> None:
    documents = _documents(tmp_path)
    deployment = _resource(documents, "Deployment", "openreward-ppt-runtime")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "openreward-ppt-runtime"
    assert pod["automountServiceAccountToken"] is True
    init = pod["initContainers"][0]
    install = " ".join(init["args"])
    assert "--no-index" in install
    assert "kubernetes==36.0.3" in install
    assert "pip.baidu" not in install

    container = pod["containers"][0]
    environment = {item["name"]: item for item in container["env"]}
    assert environment["ORS_PPT_RUNTIME_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "ors-auth",
        "key": "runtime_token",
    }
    assert environment["ORS_PPT_RUBRIC_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "ors-auth",
        "key": "rubric_token",
    }
    assert "value" not in environment["ORS_PPT_RUNTIME_TOKEN"]
    assert "value" not in environment["ORS_PPT_RUBRIC_TOKEN"]
    assert environment["ORS_PPT_ENGINE"]["value"] == "kubernetes"
    assert environment["ORS_PPT_MAX_RENDER_IMAGES"]["value"] == "64"
    assert environment["ORS_PPT_K8S_NODE_BINARY"]["value"].endswith("/tools/node")
    assert not any("docker.sock" in str(item) for item in pod["volumes"])


def test_runtime_manifest_has_least_privilege_rbac_and_sandbox_network_isolation(
    tmp_path: Path,
) -> None:
    documents = _documents(tmp_path)
    role = _resource(documents, "Role", "openreward-ppt-runtime")
    rules = {tuple(item["resources"]): set(item["verbs"]) for item in role["rules"]}
    assert rules[("pods",)] == {"create", "get", "list", "delete"}
    assert rules[("pods/log",)] == {"get"}
    assert rules[("pods/exec",)] == {"create", "get"}

    sandbox = _resource(
        documents,
        "NetworkPolicy",
        "openreward-ppt-sandbox-deny-all",
    )
    assert sandbox["spec"]["podSelector"]["matchLabels"] == {
        "research-ops.runtime": "openreward-ppt-sandbox"
    }
    assert sandbox["spec"]["ingress"] == []
    assert sandbox["spec"]["egress"] == []
    runtime = _resource(documents, "NetworkPolicy", "openreward-ppt-runtime")
    cidrs = {
        peer["ipBlock"]["cidr"]
        for rule in runtime["spec"]["egress"]
        for peer in rule.get("to", [])
        if "ipBlock" in peer
    }
    assert cidrs == {"192.0.2.1/32", "192.0.2.42/32"}


def test_runtime_control_job_only_reads_runtime_token_from_secret(tmp_path: Path) -> None:
    args = _arguments(tmp_path)
    replacements = {
        "NAMESPACE": args.namespace,
        "PPT_IMAGE": args.image,
        "IMAGE_PULL_SECRET": args.image_pull_secret,
        "AUTH_SECRET": args.auth_secret,
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT),
        "BUNDLE_ROOT": str(args.bundle_root),
        "ARTIFACT_ROOT": str(args.artifact_root),
        "TASK_ID": "ppt-control-task",
    }
    documents = render_documents(
        PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime-control-job.yaml",
        replacements,
    )
    assert len(documents) == 1
    pod = documents[0]["spec"]["template"]["spec"]
    environment = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert environment["ORS_PPT_RUNTIME_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "ors-auth",
        "key": "runtime_token",
    }
    assert pod["automountServiceAccountToken"] is False


def test_runtime_wheelhouse_requirements_match_deployer_lock() -> None:
    path = PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime-wheelhouse-requirements.txt"
    locked = {
        name: version
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, version in [line.split("==", maxsplit=1)]
    }
    assert locked == _WHEEL_REQUIREMENTS
