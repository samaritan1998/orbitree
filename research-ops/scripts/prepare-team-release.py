"""Copy an allowlisted, credential-free shared release; never copy live data or homes."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    destination = args.destination.resolve()
    if os.geteuid() != 0:
        parser.error("Shared releases must be installed by the server administrator")
    if destination.exists():
        parser.error("Destination exists; choose a new versioned release directory")
    if not (args.frontend / "index.html").is_file():
        parser.error("Build the frontend first")
    destination.mkdir(parents=True, mode=0o755)
    shutil.copytree(
        source / "backend",
        destination / "backend",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"),
    )
    shutil.copytree(args.frontend, destination / "web/dist")
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(source / name, destination / name)
    (destination / "tools/bin").mkdir(parents=True)
    print(f"Prepared {destination}; no database, .env, account login or workspace was copied")
    print("Next: create .venv in this directory and install .[team] there")


if __name__ == "__main__":
    main()
