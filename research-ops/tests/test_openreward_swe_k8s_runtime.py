from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_ops.openreward.swe_k8s_runtime import (
    KubernetesSweEngine,
    KubernetesSweRuntimeConfig,
)
from research_ops.openreward.swe_runtime import SweInfrastructureError, SweRuntimeConfig

from scripts.deploy_openreward_swe_k8s import render_manifest


class FakeCore:
    def list_namespaced_pod(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return object()


class ImagePullCore(FakeCore):
    def read_namespaced_pod(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return SimpleNamespace(
            status=SimpleNamespace(
                phase="Pending",
                container_statuses=[
                    SimpleNamespace(
                        name="policy",
                        state=SimpleNamespace(
                            running=None,
                            waiting=SimpleNamespace(reason="ErrImagePull"),
                        ),
                    )
                ],
            )
        )


def _config(
    tmp_path: Path, *, prefix: str = "registry.test/swebench"
) -> KubernetesSweRuntimeConfig:
    work = tmp_path / "swe-work"
    work.mkdir()
    return KubernetesSweRuntimeConfig.from_environ(
        SweRuntimeConfig(),
        {
            "ORS_SWE_K8S_NAMESPACE": "rl-training",
            "ORS_SWE_K8S_HOST_ROOT": str(tmp_path),
            "ORS_SWE_K8S_WORK_ROOT": str(work),
            "ORS_SWE_K8S_IMAGE_PULL_SECRET": "registry-auth",
            "ORS_SWE_K8S_IMAGE_PREFIX": prefix,
        },
    )


def test_kubernetes_swe_maps_official_images_to_internal_registry(tmp_path: Path) -> None:
    engine = KubernetesSweEngine(
        _config(tmp_path),
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    assert engine._resolve_image("swebench/sweb.eval.x86_64.astropy:latest") == (
        "registry.test/swebench/sweb.eval.x86_64.astropy:latest"
    )


def test_kubernetes_swe_requires_a_mirror_for_official_images(tmp_path: Path) -> None:
    engine = KubernetesSweEngine(
        _config(tmp_path, prefix=""),
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    try:
        engine._resolve_image("swebench/sweb.eval.x86_64.astropy:latest")
    except SweInfrastructureError as exc:
        assert "mirror" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("official Docker Hub image was accepted without a mirror")


def test_kubernetes_swe_applies_explicit_image_map_before_prefix(tmp_path: Path) -> None:
    work = tmp_path / "swe-work"
    work.mkdir()
    image_map = tmp_path / "image-map.json"
    image_map.write_text(
        '{"swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest": '
        '"registry.test/mirror:sweb.eval.x86_64.astropy__astropy-12907"}\n',
        encoding="utf-8",
    )
    config = KubernetesSweRuntimeConfig.from_environ(
        SweRuntimeConfig(),
        {
            "ORS_SWE_K8S_NAMESPACE": "rl-training",
            "ORS_SWE_K8S_HOST_ROOT": str(tmp_path),
            "ORS_SWE_K8S_WORK_ROOT": str(work),
            "ORS_SWE_K8S_IMAGE_PREFIX": "registry.test/swebench",
            "ORS_SWE_K8S_IMAGE_MAP_PATH": str(image_map),
        },
    )
    engine = KubernetesSweEngine(
        config,
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    assert engine._resolve_image(
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    ) == "registry.test/mirror:sweb.eval.x86_64.astropy__astropy-12907"


def test_kubernetes_swe_maps_swegym_public_images_to_internal_prefix(tmp_path: Path) -> None:
    engine = KubernetesSweEngine(
        _config(tmp_path),
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    assert engine._resolve_image(
        "xingyaoww/sweb.eval.x86_64.pydantic_s_pydantic-4882:latest"
    ) == "registry.example.com/acg-agi/swegym/sweb.eval.x86_64.pydantic_s_pydantic-4882:latest"


def test_kubernetes_swe_can_keep_swegym_image_public_for_a_pull_probe(tmp_path: Path) -> None:
    work = tmp_path / "swe-work"
    work.mkdir()
    config = KubernetesSweRuntimeConfig.from_environ(
        SweRuntimeConfig(),
        {
            "ORS_SWE_K8S_NAMESPACE": "rl-training",
            "ORS_SWE_K8S_HOST_ROOT": str(tmp_path),
            "ORS_SWE_K8S_WORK_ROOT": str(work),
            "ORS_SWE_K8S_IMAGE_PREFIX": "registry.test/swebench",
            "ORS_SWE_K8S_SWEGYM_IMAGE_PREFIX": "",
        },
    )
    engine = KubernetesSweEngine(
        config,
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    assert engine._resolve_image(
        "xingyaoww/sweb.eval.x86_64.pydantic_s_pydantic-4882:latest"
    ) == "xingyaoww/sweb.eval.x86_64.pydantic_s_pydantic-4882:latest"


def test_kubernetes_swe_fails_fast_when_image_pull_is_rejected(tmp_path: Path) -> None:
    engine = KubernetesSweEngine(
        _config(tmp_path),
        core_api=ImagePullCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    with pytest.raises(SweInfrastructureError, match="ErrImagePull"):
        engine._wait_running("ors-swe-policy-test", "policy")


def test_kubernetes_swe_pod_only_mounts_bounded_exchange_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = KubernetesSweEngine(
        config,
        core_api=FakeCore(),
        stream_fn=lambda *args, **kwargs: "",
    )
    io_root = config.work_root / "session" / "io"
    io_root.mkdir(parents=True)
    pod = engine._pod_body(
        name="ors-swe-policy-test",
        role="policy",
        image="registry.test/swebench/task:fixed",
        io_root=io_root,
        active_deadline_seconds=3600,
    )["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "registry-auth"}]
    assert pod["containers"][0]["workingDir"] == "/testbed"
    assert pod["containers"][0]["resources"]["limits"]["memory"] == "8Gi"
    assert pod["volumes"][0]["hostPath"]["path"] == str(io_root)
    assert "/testbed" not in str(pod["volumes"])
    assert "docker.sock" not in str(pod)


def test_swe_k8s_deployment_has_least_privilege_rbac_and_no_docker_socket(
    tmp_path: Path,
) -> None:
    host = tmp_path / "host"
    work = host / "work"
    harness = tmp_path / "harness"
    host.mkdir()
    work.mkdir()
    harness.mkdir()
    args = Namespace(
        namespace="rl-training",
        image="registry.test/openreward-swe:fixed",
        image_prefix="registry.test/swebench",
        image_pull_secret="registry-auth",
        auth_secret="swe-auth",
        host_root=host,
        work_root=work,
        harness_root=harness,
        dry_run=False,
    )
    docs = render_manifest(
        Path(__file__).resolve().parents[1] / "deploy/openreward-swe-k8s/deployment.yaml",
        args,
    )
    deployment = next(item for item in docs if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "openreward-swe-runtime"
    assert pod["automountServiceAccountToken"] is True
    assert not any("docker.sock" in str(item) for item in pod["volumes"])
    role = next(item for item in docs if item["kind"] == "Role")
    resources = {tuple(rule["resources"]): set(rule["verbs"]) for rule in role["rules"]}
    assert resources[("pods",)] == {"create", "get", "list", "delete"}
    assert resources[("pods/exec",)] == {"create", "get"}
