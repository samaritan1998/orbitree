#!/usr/bin/env python3
"""Create and clean up one real SWE-Gym ORS sandbox with secret-safe evidence."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POOL = PROJECT_ROOT / "config" / "openreward-four-domain-formal-r1-task-pool.json"
DEFAULT_KUBECONFIG = Path("/srv/research-ops/tools/cluster-config")
DEFAULT_ORS_URL = "http://198.51.100.7:8200/sandbox/internal/openreward-ors-harn0001/ors"


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>"
            if any(part in str(key).casefold() for part in ("token", "secret", "key", "auth"))
            else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _task(pool: Path, index: int) -> dict[str, Any]:
    payload = json.loads(pool.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    tasks = payload.get("environments", {}).get("swe", {}).get("train", [])
    if not isinstance(tasks, list) or not 0 <= index < len(tasks):
        raise ValueError("SWE-Gym task index is unavailable")
    task = tasks[index]
    if not isinstance(task, dict) or task.get("benchmark_family") != "swegym":
        raise ValueError("selected task is not a SWE-Gym task")
    return task


def _ors_token(kubeconfig: Path, namespace: str, secret_name: str, field: str) -> str:
    from kubernetes import client, config

    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig.expanduser().resolve(strict=True)),
        client_configuration=configuration,
    )
    configuration.proxy = None
    core = client.CoreV1Api(client.ApiClient(configuration))
    secret = core.read_namespaced_secret(secret_name, namespace, _request_timeout=30)
    encoded = (secret.data or {}).get(field)
    if not encoded:
        raise RuntimeError(f"Kubernetes Secret is missing required field: {field}")
    return base64.b64decode(encoded).decode("utf-8").strip()


def _response(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        body = {"text": response.text[:1000]}
    return {"status_code": response.status_code, "body": _safe(body)}


def probe(args: argparse.Namespace) -> dict[str, Any]:
    task = _task(args.task_pool, args.task_index)
    token = _ors_token(args.kubeconfig, args.namespace, args.auth_secret, args.auth_field)
    headers = {"Authorization": f"Bearer {token}"}
    evidence: dict[str, Any] = {
        "schema_version": "openreward.swegym_sandbox_probe.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "task_id": task["task_id"],
        "benchmark_family": task["benchmark_family"],
        "image_ref": task.get("sandbox", {}).get("image_ref"),
        "base_commit": task.get("base_commit"),
        "status": "failed",
    }
    sid = ""
    base_url = args.ors_url.rstrip("/")
    try:
        with httpx.Client(timeout=args.timeout_seconds, trust_env=False) as client:
            created = client.post(f"{base_url}/create_session", headers=headers)
            evidence["create_session"] = _response(created)
            created.raise_for_status()
            sid = str(created.json()["sid"])
            sandbox = client.post(
                f"{base_url}/create",
                headers={**headers, "X-Session-ID": sid},
                json={"env_name": "swe", "task_spec": task, "secrets": {}},
            )
            evidence["create"] = _response(sandbox)
            sandbox.raise_for_status()
            prompt = client.get(
                f"{base_url}/swe/prompt",
                headers={**headers, "X-Session-ID": sid},
            )
            evidence["prompt"] = {
                "status_code": prompt.status_code,
                "message_count": len(prompt.json()) if prompt.is_success else 0,
            }
            prompt.raise_for_status()
            evidence["status"] = "completed"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        evidence["error"] = str(exc).replace(token, "<redacted>")[:1000]
    finally:
        if sid:
            try:
                with httpx.Client(timeout=60, trust_env=False) as client:
                    deleted = client.post(
                        f"{base_url}/delete_session",
                        headers={**headers, "X-Session-ID": sid},
                    )
                    evidence["cleanup"] = _response(deleted)
            except Exception as exc:
                evidence["cleanup"] = {"error_type": type(exc).__name__}
    evidence["finished_at"] = datetime.now(UTC).isoformat()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-pool", type=Path, default=DEFAULT_POOL)
    parser.add_argument("--task-index", type=int, default=0)
    parser.add_argument("--ors-url", default=DEFAULT_ORS_URL)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--namespace", default="rl-training")
    parser.add_argument("--auth-secret", default="openreward-ors-auth")
    parser.add_argument("--auth-field", default="api_key")
    parser.add_argument("--timeout-seconds", type=float, default=960.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = probe(args)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(_safe(evidence), ensure_ascii=False, sort_keys=True))
    return 0 if evidence["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
