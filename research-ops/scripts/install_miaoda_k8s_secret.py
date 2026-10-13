#!/usr/bin/env python3
"""Interactively install the Miaoda runtime Secret without echoing values."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from kubernetes import client, config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=Path("/srv/research-ops/tools/cluster-config"),
    )
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--name", default="openreward-miaoda-auth")
    args = parser.parse_args()
    values = {
        "datawheel_token": getpass.getpass("Datawheel token (hidden): ").strip(),
        "goblin_token": getpass.getpass("Goblin token (hidden): ").strip(),
        "generated_user_id": input("Datawheel generated_user_id: ").strip(),
    }
    if not all(values.values()):
        raise SystemExit("all three runtime fields are required")
    cfg = client.Configuration()
    config.load_kube_config(config_file=str(args.kubeconfig), client_configuration=cfg)
    cfg.proxy = None
    api = client.CoreV1Api(client.ApiClient(cfg))
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=args.name,
            namespace=args.namespace,
            labels={
                "app.kubernetes.io/part-of": "research-ops",
                "research-ops.work-id": "harn-0001",
            },
        ),
        type="Opaque",
        string_data=values,
    )
    try:
        api.read_namespaced_secret(args.name, args.namespace, _request_timeout=20)
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            raise SystemExit(f"cannot inspect Secret: {type(exc).__name__}") from exc
        api.create_namespaced_secret(args.namespace, body, _request_timeout=20)
        print(f"created {args.namespace}/{args.name} with keys: {', '.join(sorted(values))}")
        return 0
    api.patch_namespaced_secret(args.name, args.namespace, body, _request_timeout=20)
    print(f"updated {args.namespace}/{args.name} with keys: {', '.join(sorted(values))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
