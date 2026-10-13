#!/usr/bin/env python3
"""Run a local ORS server for protocol smoke tests.

This command intentionally uses safe mock backends.  It is a contract test
server, not a production rollout runner.  Inject real backends in Python (or
in a deployment image) only after the per-domain interfaces are confirmed.
"""

from __future__ import annotations

import argparse

import uvicorn
from research_ops.openreward import build_default_environments, create_openreward_app


def build_app(*, demo_tasks: bool = False):
    catalogs = None
    if demo_tasks:
        catalogs = {
            name: {
                "train": [
                    {
                        "id": f"demo-{name}-0",
                        "query": f"OpenReward protocol smoke task for {name}",
                    }
                ]
            }
            for name in ("miaoda", "swe", "ppt", "tooluse")
        }
    return create_openreward_app(build_default_environments(task_catalogs=catalogs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--demo-tasks",
        action="store_true",
        help="publish one synthetic train task per environment",
    )
    args = parser.parse_args()
    uvicorn.run(build_app(demo_tasks=args.demo_tasks), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

