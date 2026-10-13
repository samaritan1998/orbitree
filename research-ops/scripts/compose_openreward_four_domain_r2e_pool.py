#!/usr/bin/env python3
"""Compose the formal four-domain pool with the executable R2E SWE split."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-pool", type=Path, required=True)
    parser.add_argument("--r2e-pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base_path = args.base_pool.expanduser().resolve(strict=True)
    r2e_path = args.r2e_pool.expanduser().resolve(strict=True)
    base = _load(base_path)
    r2e = _load(r2e_path)
    environments = base.get("environments")
    r2e_environments = r2e.get("environments")
    if not isinstance(environments, dict) or not isinstance(r2e_environments, dict):
        raise ValueError("both pools must contain environments objects")
    base_domains = {str(item) for item in environments}
    if base_domains != {"miaoda", "ppt", "swe", "tooluse"}:
        raise ValueError("base pool must contain exactly the four formal domains")
    swe = r2e_environments.get("swe")
    if not isinstance(swe, dict) or not isinstance(swe.get("train"), list) or not swe["train"]:
        raise ValueError("R2E pool has no non-empty swe.train list")
    if any(
        not isinstance(task, dict) or task.get("benchmark_family") != "r2e"
        for task in swe["train"]
    ):
        raise ValueError("R2E replacement contains a non-R2E SWE task")

    environments["swe"] = {"train": swe["train"]}
    base["schema_version"] = "openreward.formal_task_pool.v2-r2e-swe"
    base["experiment"] = "four-domain-online-mopd-formal-r1-r2e-swe"
    base["swe_replacement"] = {
        "benchmark_family": "r2e",
        "source_pool": str(r2e_path),
        "source_pool_sha256": _sha256(r2e_path),
        "reason": (
            "SWE-Gym public images are unavailable from the runtime cluster; "
            "R2E images are mirrored internally and executable."
        ),
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(base, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "output": str(output),
                "domains": {name: len(value["train"]) for name, value in environments.items()},
                "swe_benchmark_family": "r2e",
                "base_pool_sha256": _sha256(base_path),
                "r2e_pool_sha256": _sha256(r2e_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
