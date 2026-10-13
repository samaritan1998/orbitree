#!/usr/bin/env python3
"""Materialize or validate the four-domain MOPD teacher runtime route file.

The command reads only endpoint-receipt *references* from the environment.
It never prints endpoint URLs, credentials, or receipt contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from research_ops.openreward.teacher_routing import (  # noqa: E402
    TeacherRoutingError,
    load_teacher_routing,
    materialize_runtime_routes,
    validate_runtime_teacher_route,
    write_runtime_routes,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--routing", type=Path, help="Versioned domain-to-checkpoint YAML")
    mode.add_argument("--runtime-routes", type=Path, help="Materialized runtime route JSON")
    parser.add_argument("--output", type=Path, help="Output JSON required with --routing")
    parser.add_argument("--domain", choices=("miaoda", "swe", "ppt", "tooluse"))
    parser.add_argument("--check-health", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    args = parser.parse_args()

    try:
        if args.routing is not None:
            if args.output is None:
                raise TeacherRoutingError("--output is required with --routing")
            routing = load_teacher_routing(args.routing)
            routes = materialize_runtime_routes(
                routing,
                check_health=bool(args.check_health),
                timeout_seconds=args.timeout_seconds,
            )
            target = write_runtime_routes(args.output, routes)
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "route_file": str(target),
                        "domains": sorted(routes["routes"]),
                        "unique_teacher_count": len(
                            {
                                value["serving_checkpoint"]
                                for value in routes["routes"].values()
                            }
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        if args.domain is None:
            raise TeacherRoutingError("--domain is required with --runtime-routes")
        route = validate_runtime_teacher_route(
            args.runtime_routes,
            domain=args.domain,
            check_health=bool(args.check_health),
            timeout_seconds=args.timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "status": "ready",
                    "domain": route.domain,
                    "checkpoint": route.checkpoint,
                    "endpoint_fingerprint": route.endpoint_fingerprint,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except TeacherRoutingError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
