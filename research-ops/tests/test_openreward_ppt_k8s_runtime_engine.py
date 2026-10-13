from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from research_ops.openreward.ppt_k8s_runtime import (
    KubernetesPptEngine,
    KubernetesPptRuntimeConfig,
)
from research_ops.openreward.ppt_runtime import PptRuntimeConfig, PptTask

_PNG = b"\x89PNG\r\n\x1a\nfixture"


class FakeApiError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(str(status))
        self.status = status


def _container_status(name: str, *, running: bool, image: str) -> Any:
    state = SimpleNamespace(
        running=SimpleNamespace() if running else None,
        waiting=None,
        terminated=None if running else SimpleNamespace(exit_code=0),
    )
    return SimpleNamespace(name=name, state=state, image_id=image, ready=running)


class FakeCoreApi:
    def __init__(self) -> None:
        self.pods: dict[str, Any] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def create_namespaced_pod(self, namespace: str, body: dict[str, Any], **_: Any) -> Any:
        assert namespace == "rl-training"
        self.created.append(body)
        metadata = body["metadata"]
        container = body["spec"]["containers"][0]
        name = metadata["name"]
        role = container["name"]
        image_id = f"registry.test/ppt@sha256:{'a' * 64}"
        running = role == "policy"
        if role == "render":
            workspace = next(
                Path(item["hostPath"]["path"])
                for item in body["spec"]["volumes"]
                if item["name"] == "workspace"
            )
            (workspace / "render" / "slide-1.png").write_bytes(_PNG)
        pod = SimpleNamespace(
            metadata=SimpleNamespace(name=name),
            status=SimpleNamespace(
                phase="Running" if running else "Succeeded",
                container_statuses=[
                    _container_status(role, running=running, image=image_id)
                ],
            ),
        )
        self.pods[name] = pod
        return pod

    def read_namespaced_pod(self, name: str, namespace: str, **_: Any) -> Any:
        assert namespace == "rl-training"
        if name not in self.pods:
            raise FakeApiError(404)
        return self.pods[name]

    def delete_namespaced_pod(self, name: str, namespace: str, **_: Any) -> None:
        assert namespace == "rl-training"
        if name not in self.pods:
            raise FakeApiError(404)
        self.deleted.append(name)
        self.pods.pop(name)

    def list_namespaced_pod(self, namespace: str, **_: Any) -> Any:
        assert namespace == "rl-training"
        return SimpleNamespace(items=list(self.pods.values()))

    def read_namespaced_pod_log(self, *_: Any, **__: Any) -> str:
        return "fixture logs"

    def connect_get_namespaced_pod_exec(self, *_: Any, **__: Any) -> None:
        raise AssertionError("the injected stream function should intercept exec")


def fake_stream(_: Any, *__: Any, **kwargs: Any) -> str:
    command = kwargs["command"]
    marker = next(item for item in command if re.fullmatch(r"__ORS_EXIT_[0-9a-f]+__", item))
    return f"command output\n{marker}0\n"


def _task() -> PptTask:
    return PptTask.from_spec(
        {
            "task_id": "ppt-k8s-fixture",
            "query": "Create one editable slide.",
            "benchmark_family": "ppt_online",
            "output_path": "/workspace/slides.pptx",
            "assets": [{"path": "/inputs/reference.md"}],
            "verifier": {
                "min_slides": 1,
                "max_slides": 1,
                "editable_pptx_required": True,
                "render_required": True,
            },
        }
    )


def _config(tmp_path: Path) -> KubernetesPptRuntimeConfig:
    bundle_root = tmp_path / "bundles"
    artifact_root = tmp_path / "artifacts"
    work_root = tmp_path / "work"
    for path in (bundle_root, artifact_root, work_root):
        path.mkdir()
    node_binary = tmp_path / "node"
    node_binary.write_bytes(b"fixture node")
    node_binary.chmod(0o755)
    runtime = PptRuntimeConfig(
        policy_image="registry.test/ppt:fixed",
        verifier_image="registry.test/ppt:fixed",
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        skill_in_image=True,
        command_seconds=10,
        render_seconds=10,
    )
    return KubernetesPptRuntimeConfig(
        runtime=runtime,
        namespace="rl-training",
        work_root=work_root,
        host_root=tmp_path,
        image_pull_secret="registry-auth",
        pod_start_seconds=2,
        delete_seconds=2,
        node_binary=node_binary,
    )


def test_kubernetes_engine_uses_isolated_pods_and_shared_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inputs = config.runtime.bundle_root / "ppt-k8s-fixture" / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "reference.md").write_text("ground truth", encoding="utf-8")
    core = FakeCoreApi()
    engine = KubernetesPptEngine(config, core_api=core, stream_fn=fake_stream)

    handle = engine.start(_task(), inputs, session_id="ors-ppt-fixture")
    policy = core.created[0]
    assert policy["spec"]["automountServiceAccountToken"] is False
    assert policy["spec"]["enableServiceLinks"] is False
    assert policy["metadata"]["labels"]["research-ops.role"] == "policy"
    mounts = policy["spec"]["containers"][0]["volumeMounts"]
    assert {"name": "inputs", "mountPath": "/inputs", "readOnly": True} in mounts
    assert {
        "name": "node-runtime",
        "mountPath": "/usr/local/bin/node",
        "readOnly": True,
    } in mounts
    assert engine.image_id("policy").endswith("a" * 64)

    assert engine.write_file(handle, PurePosixPath("/workspace/source.js"), b"source") == 6
    assert engine.read_file(handle, PurePosixPath("/workspace/source.js")) == b"source"
    assert engine.read_file(handle, PurePosixPath("/inputs/reference.md")) == b"ground truth"
    result = engine.exec_shell(handle, "printf ok")
    assert result.exit_code == 0
    assert result.text == "command output"

    assert engine.render(b"fixture-pptx", session_id=handle.session_id) == [_PNG]
    renderer = core.created[1]
    assert renderer["metadata"]["labels"]["research-ops.role"] == "render"
    assert renderer["spec"]["automountServiceAccountToken"] is False
    assert not any(
        item["name"] == "inputs" for item in renderer["spec"]["volumes"]
    )
    assert not any(
        item["name"] == "node-runtime" for item in renderer["spec"]["volumes"]
    )

    engine.remove(handle)
    assert not handle.session_root.exists()
    assert len(core.deleted) == 2


def test_kubernetes_config_rejects_workspaces_outside_host_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    environ = {
        "ORS_PPT_K8S_HOST_ROOT": str(tmp_path),
        "ORS_PPT_K8S_WORK_ROOT": str(outside),
    }

    with pytest.raises(Exception, match="must be below"):
        KubernetesPptRuntimeConfig.from_environ(config.runtime, environ)
