#!/usr/bin/env python3
"""Submit and monitor one Kubernetes-native OpenReward PPT control episode."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

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
    from scripts.run_openreward_ppt_k8s_control import DEFAULT_TASK_ID
    from scripts.run_openreward_ppt_k8s_parity import _clients, wait_for_job
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
    from run_openreward_ppt_k8s_control import DEFAULT_TASK_ID  # type: ignore[no-redef]
    from run_openreward_ppt_k8s_parity import (  # type: ignore[no-redef]
        _clients,
        wait_for_job,
    )

DEFAULT_TEMPLATE = PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime-control-job.yaml"


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
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-pull-secret", default="iregistry-secret")
    parser.add_argument("--auth-secret", default="openreward-ppt-auth")
    parser.add_argument("--wait-seconds", type=int, default=7200)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bundle_root = _absolute_directory(args.bundle_root)
    task_path = bundle_root / args.task_id / "task.json"
    if not task_path.is_file():
        raise ValueError(f"PPT task bundle is missing task.json: {args.task_id}")
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
        "RESEARCH_OPS_ROOT": str(PROJECT_ROOT.resolve(strict=True)),
        "BUNDLE_ROOT": str(bundle_root),
        "ARTIFACT_ROOT": str(artifact_root),
        "TASK_ID": args.task_id,
    }
    documents = render_documents(args.template, replacements)
    if len(documents) != 1 or documents[0].get("kind") != "Job":
        raise ValueError("control manifest must contain exactly one Job")
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
            raise RuntimeError("deploy the PPT runtime before submitting control") from exc
        raise
    if "runtime_token" not in (secret.data or {}):
        raise RuntimeError("PPT auth Secret is missing runtime_token")
    deployment = core.list_namespaced_pod(
        args.namespace,
        label_selector="app.kubernetes.io/name=openreward-ppt-runtime",
        _request_timeout=30,
    ).items
    if not any(
        pod.status.phase == "Running"
        and all(item.ready for item in (pod.status.container_statuses or []))
        for pod in deployment
    ):
        raise RuntimeError("PPT runtime Deployment is not ready")
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
        container_name="control",
    )
    pod_name = result.get("pod")
    logs = core.read_namespaced_pod_log(
        pod_name,
        args.namespace,
        container="control",
        tail_lines=160,
        _request_timeout=30,
    )
    if isinstance(logs, bytes):
        logs = logs.decode("utf-8", errors="replace")
    print(logs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
