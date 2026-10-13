#!/usr/bin/env python3
"""Submit and monitor one real SlideForge HTTP parity as a Kubernetes Job."""

from __future__ import annotations

import argparse
import json
import os
import time
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
        render_documents,
    )

DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-ppt-k8s/reward-parity-job.yaml"


def _clients(kubeconfig: Path) -> tuple[Any, Any]:
    try:
        from kubernetes import client, config
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("install the ppt-k8s extra before submitting") from exc
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig),
        client_configuration=configuration,
    )
    configuration.proxy = None
    api_client = client.ApiClient(configuration)
    return client.CoreV1Api(api_client), client.BatchV1Api(api_client)


def _pod_state(core: Any, *, namespace: str, job_name: str) -> dict[str, Any]:
    pods = core.list_namespaced_pod(
        namespace,
        label_selector=f"job-name={job_name}",
        _request_timeout=30,
    ).items
    if not pods:
        return {"pod": None, "phase": "Pending"}
    pod = pods[0]
    waiting = next(
        (
            item.state.waiting.reason
            for item in (pod.status.container_statuses or [])
            if item.state and item.state.waiting
        ),
        None,
    )
    return {
        "pod": pod.metadata.name,
        "phase": pod.status.phase,
        "waiting_reason": waiting,
    }


def wait_for_job(
    core: Any,
    batch: Any,
    *,
    namespace: str,
    job_name: str,
    timeout_seconds: int,
    container_name: str = "parity",
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_report: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        job = batch.read_namespaced_job(job_name, namespace, _request_timeout=30)
        state = _pod_state(core, namespace=namespace, job_name=job_name)
        report = {
            "job": job_name,
            **state,
            "active": job.status.active or 0,
            "succeeded": job.status.succeeded or 0,
            "failed": job.status.failed or 0,
        }
        if report != last_report:
            print(json.dumps(report, sort_keys=True), flush=True)
            last_report = report
        if job.status.succeeded:
            return report
        if job.status.failed:
            pod_name = state.get("pod")
            logs = (
                core.read_namespaced_pod_log(
                    pod_name,
                    namespace,
                    container=container_name,
                    tail_lines=120,
                    _request_timeout=30,
                )
                if pod_name
                else ""
            )
            if isinstance(logs, bytes):
                logs = logs.decode("utf-8", errors="replace")
            raise RuntimeError(f"parity Job failed; logs:\n{logs}")
        time.sleep(10)
    raise RuntimeError(f"parity Job timed out: {json.dumps(last_report)}")


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
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-ppt-auth")
    parser.add_argument("--wait-seconds", type=int, default=7200)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle_root = _absolute_directory(args.bundle_root)
    artifact_root = _absolute_directory(
        args.artifact_root,
        create=not args.dry_run,
        allow_missing=args.dry_run,
    )
    replacements = {
        "NAMESPACE": args.namespace,
        "PPT_IMAGE": args.image,
        "IMAGE_PULL_SECRET": args.image_pull_secret,
        "AUTH_SECRET": args.auth_secret,
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT),
        "BUNDLE_ROOT": str(bundle_root),
        "ARTIFACT_ROOT": str(artifact_root),
    }
    documents = render_documents(args.template, replacements)
    if len(documents) != 1 or documents[0].get("kind") != "Job":
        raise ValueError("parity manifest must contain exactly one Job")
    if args.dry_run:
        print(json.dumps({"ready": True, "resources": ["Job"]}))
        return 0

    core, batch = _clients(args.kubeconfig.expanduser().resolve(strict=True))
    try:
        secret = core.read_namespaced_secret(
            args.auth_secret,
            args.namespace,
            _request_timeout=30,
        )
    except Exception as exc:
        if _status(exc) == 404:
            raise RuntimeError("deploy the Reward adapter before submitting parity") from exc
        raise
    if "rubric_token" not in (secret.data or {}):
        raise RuntimeError("Reward auth Secret is missing rubric_token")
    created = batch.create_namespaced_job(
        args.namespace,
        documents[0],
        _request_timeout=30,
    )
    job_name = created.metadata.name
    print(json.dumps({"submitted": True, "job": job_name}), flush=True)
    result = wait_for_job(
        core,
        batch,
        namespace=args.namespace,
        job_name=job_name,
        timeout_seconds=args.wait_seconds,
    )
    pod_name = result.get("pod")
    logs = core.read_namespaced_pod_log(
        pod_name,
        args.namespace,
        container="parity",
        tail_lines=120,
        _request_timeout=30,
    )
    if isinstance(logs, bytes):
        logs = logs.decode("utf-8", errors="replace")
    print(logs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
