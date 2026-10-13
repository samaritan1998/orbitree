#!/usr/bin/env python3
"""Run the unified Miaoda/SWE/PPT/ToolUse OpenReward server.

Mock mode is credential-free and safe for protocol tests.  Contract mode
loads real backend adapters from environment-variable references; it fails
closed when a reference is missing and never prints runtime values.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import uvicorn
from research_ops.openreward import (
    OpenRewardRuntimeError,
    build_default_environments,
    build_environments_from_contract,
    create_openreward_app,
    load_task_catalogs_many,
)
from research_ops.openreward.policy_proxy import install_policy_capture_proxy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKENDS = PROJECT_ROOT / "config" / "openreward-backends.example.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mode", choices=("mock", "contract"), default="mock")
    parser.add_argument("--backend-config", type=Path, default=DEFAULT_BACKENDS)
    parser.add_argument(
        "--task-catalog",
        type=Path,
        action="append",
        help="Task catalog to load; repeat to combine domain catalogs.",
    )
    parser.add_argument(
        "--api-key-env",
        help="environment-variable name accepted through X-API-Key or Bearer auth",
    )
    parser.add_argument(
        "--real-domain",
        action="append",
        choices=("miaoda", "swe", "ppt", "tooluse"),
        help="Connect one real domain; repeat as needed. Defaults to all in contract mode.",
    )
    args = parser.parse_args()

    try:
        catalogs = (
            load_task_catalogs_many(args.task_catalog)
            if args.task_catalog
            else None
        )
        if args.mode == "contract":
            environments = build_environments_from_contract(
                args.backend_config,
                real_domains=args.real_domain,
                task_catalogs=catalogs,
            )
        else:
            environments = build_default_environments(task_catalogs=catalogs)
    except OpenRewardRuntimeError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.api_key_env and not os.environ.get(args.api_key_env, "").strip():
        print(
            json.dumps(
                {"ready": False, "error": f"missing runtime reference: {args.api_key_env}"},
                ensure_ascii=False,
            )
        )
        return 2

    app = create_openreward_app(environments, api_key_env=args.api_key_env)
    upstream = os.environ.get("MOPD_POLICY_UPSTREAM_URL", "").strip()
    upstream_route_file = os.environ.get(
        "MOPD_POLICY_UPSTREAM_ROUTE_FILE", ""
    ).strip()
    capture_dir = os.environ.get("MOPD_POLICY_CAPTURE_DIR", "").strip()
    tokenizer_path = os.environ.get("MOPD_POLICY_TOKENIZER_PATH", "").strip()
    chat_template_path = os.environ.get(
        "MOPD_POLICY_CHAT_TEMPLATE_PATH", ""
    ).strip()
    if (upstream or upstream_route_file) and capture_dir:
        install_policy_capture_proxy(
            app,
            upstream_url=upstream or None,
            upstream_route_file=upstream_route_file or None,
            capture_dir=capture_dir,
            tokenizer_path=tokenizer_path or None,
            chat_template_path=chat_template_path or None,
            prefix=os.environ.get("MOPD_POLICY_PROXY_PREFIX", "/policy"),
        )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
