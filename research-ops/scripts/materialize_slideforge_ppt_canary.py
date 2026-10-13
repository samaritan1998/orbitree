#!/usr/bin/env python3
"""Materialize the pinned real SlideForge PPT canary task selection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_ops.openreward.slideforge_tasks import materialize_slideforge_tasks

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION = PROJECT_ROOT / "config" / "openreward-ppt-slideforge-0629-selection.json"
DEFAULT_BUNDLE_ROOT = PROJECT_ROOT / "data" / "example-ppt-bundles" / "bundles"
DEFAULT_CATALOG = PROJECT_ROOT / "config" / "openreward-ppt-canary.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE_ROOT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    manifest = materialize_slideforge_tasks(
        run_root=args.run_root,
        selection_path=args.selection,
        bundle_root=args.bundle_root,
        catalog_path=args.catalog,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
