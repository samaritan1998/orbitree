#!/usr/bin/env python3
"""Run the isolated official SWE-bench runtime consumed by OpenReward."""

from __future__ import annotations

import argparse

import uvicorn
from research_ops.openreward.swe_runtime import create_swe_runtime_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(
        create_swe_runtime_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
