#!/usr/bin/env python3
"""Prepare an offline Python 3.11 wheelhouse and Node binary for PPT runtime Pods."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = (
    PROJECT_ROOT / "deploy/openreward-ppt-k8s/runtime-wheelhouse-requirements.txt"
)
DEFAULT_WHEELHOUSE = PROJECT_ROOT / "data/openreward_ppt_k8s/runtime-wheelhouse"
DEFAULT_NODE_TARGET = PROJECT_ROOT / "data/openreward_ppt_k8s/runtime-tools/node"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    node = shutil.which("node")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--wheelhouse-root", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument("--node-source", type=Path, default=Path(node) if node else None)
    parser.add_argument("--node-target", type=Path, default=DEFAULT_NODE_TARGET)
    parser.add_argument("--python-version", default="311")
    parser.add_argument("--platform", default="manylinux2014_x86_64")
    parser.add_argument("--index-url", default="https://packages.example.com/simple/")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requirements = args.requirements.expanduser().resolve(strict=True)
    if args.node_source is None:
        raise RuntimeError("Node is unavailable; pass --node-source explicitly")
    node_source = args.node_source.expanduser().resolve(strict=True)
    if not node_source.is_file() or not os.access(node_source, os.X_OK):
        raise RuntimeError("--node-source must be an executable file")
    wheelhouse = args.wheelhouse_root.expanduser().resolve(strict=False)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheelhouse),
            "--only-binary=:all:",
            "--platform",
            args.platform,
            "--implementation",
            "cp",
            "--python-version",
            args.python_version,
            "--abi",
            f"cp{args.python_version}",
            "--index-url",
            args.index_url,
            "--requirement",
            str(requirements),
        ],
        check=True,
    )
    node_target = args.node_target.expanduser().resolve(strict=False)
    node_target.parent.mkdir(parents=True, exist_ok=True)
    temporary = node_target.with_name(f".{node_target.name}.tmp")
    shutil.copyfile(node_source, temporary)
    temporary.chmod(0o755)
    temporary.replace(node_target)
    wheels = sorted(wheelhouse.glob("*.whl"))
    print(
        json.dumps(
            {
                "ready": True,
                "node": {
                    "path": str(node_target),
                    "bytes": node_target.stat().st_size,
                    "sha256": _sha256(node_target),
                },
                "wheelhouse": {
                    "path": str(wheelhouse),
                    "wheels": len(wheels),
                    "bytes": sum(item.stat().st_size for item in wheels),
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
