#!/usr/bin/env python3
"""Validate real ORS backend requirements without printing runtime values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_ops.openreward.contracts import (
    BackendContractError,
    backend_readiness,
    environment_references,
    load_backend_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = PROJECT_ROOT / "config" / "openreward-backends.example.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="return non-zero if any referenced runtime variable is absent",
    )
    parser.add_argument(
        "--domain",
        action="append",
        choices=("miaoda", "swe", "ppt", "tooluse"),
        help="check only one real domain; repeat as needed",
    )
    args = parser.parse_args()
    try:
        contract = load_backend_contract(args.config)
    except BackendContractError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        return 2

    references = environment_references(contract)
    report = backend_readiness(contract)
    selected = tuple(dict.fromkeys(args.domain or ("miaoda", "swe", "ppt", "tooluse")))
    output = {
        "valid": True,
        "version": contract.version,
        "domains": {
            name: {
                "adapter": getattr(contract, name).adapter,
                "environment_references": references[name],
                **report[name],
            }
            for name in selected
        },
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if args.check_env and not all(report[name]["ready"] for name in selected):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
