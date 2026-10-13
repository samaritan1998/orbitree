#!/usr/bin/env python3
"""Read one Kubernetes Secret field for runtime command substitution.

The value is intentionally written to stdout so the caller can place it in an
environment variable without persisting it in a manifest or evidence file.
Errors are deliberately generic and never include the API response body.
"""

from __future__ import annotations

import argparse
import base64
import ssl
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml


def _kube_context(path: Path) -> tuple[str, str, str, str | None]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        current = str(document["current-context"])
        context = next(item["context"] for item in document["contexts"] if item["name"] == current)
        cluster_name = str(context["cluster"])
        user_name = str(context["user"])
        cluster = next(
            item["cluster"] for item in document["clusters"] if item["name"] == cluster_name
        )
        user = next(item["user"] for item in document["users"] if item["name"] == user_name)
        server = str(cluster["server"])
        token = str(user["token"])
        ca_data = cluster.get("certificate-authority-data")
    except (KeyError, IndexError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        raise RuntimeError("invalid Kubernetes runtime reference") from exc
    if not server or not token:
        raise RuntimeError("Kubernetes runtime reference is incomplete")
    return server.rstrip("/"), token, str(ca_data or ""), None


def _verify_context(ca_data: str) -> ssl.SSLContext | bool:
    if not ca_data:
        return False
    try:
        pem = base64.b64decode(ca_data).decode("utf-8")
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=pem)
        return context
    except (ValueError, OSError, ssl.SSLError) as exc:
        raise RuntimeError("invalid Kubernetes CA reference") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()

    server, bearer, ca_data, _ = _kube_context(args.kubeconfig.expanduser().resolve(strict=True))
    url = (
        f"{server}/api/v1/namespaces/{quote(args.namespace, safe='')}/secrets/"
        f"{quote(args.name, safe='')}"
    )
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        with httpx.Client(verify=_verify_context(ca_data), trust_env=False, timeout=30) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError("Kubernetes Secret request failed") from exc
    if response.status_code != 200:
        raise RuntimeError("Kubernetes Secret request was not successful")
    try:
        payload = response.json()
        encoded = payload["data"][args.key]
        value = base64.b64decode(str(encoded), validate=True).decode("utf-8")
    except (TypeError, ValueError, KeyError, UnicodeError) as exc:
        raise RuntimeError("Kubernetes Secret field is invalid") from exc
    if not value:
        raise RuntimeError("Kubernetes Secret field is empty")
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
