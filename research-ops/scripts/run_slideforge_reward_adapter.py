#!/usr/bin/env python3
"""Run the SlideForge Reward HTTP adapter consumed by the ORS PPT runtime."""

from __future__ import annotations

import argparse

import uvicorn
from research_ops.openreward.slideforge_reward import create_slideforge_reward_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9104)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(
        create_slideforge_reward_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
