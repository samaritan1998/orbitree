#!/usr/bin/env python3
"""Materialize an executable OpenReward task pool from R2E-Gym train shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = str(
    PROJECT_ROOT / "data/example-swe-manifests/manifests/r2e_train_*.parquet"
)
DEFAULT_IMAGE_PREFIX = "registry.example.com/acg-airec/r2e_gym/"
R2E_SOURCE_SHA256 = "79e48caff7be979740a96cda596d516387f2e5bc53cb3bd62222ee64bd7bd1ea"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _internal_image(value: Any, prefix: str) -> str:
    image = str(value or "").strip()
    if not image:
        raise ValueError("R2E row has no docker image")
    if image.startswith("registry.example.com/"):
        return image
    return prefix.rstrip("/") + "/" + image.lstrip("/")


def _load_rows(pattern: str) -> list[dict[str, Any]]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - project dependency
        raise RuntimeError("duckdb is required to build the R2E task pool") from exc
    connection = duckdb.connect()
    columns = [
        row[0]
        for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [pattern]
        ).fetchall()
    ]
    records = connection.execute(
        "SELECT * FROM read_parquet(?, union_by_name=true)", [pattern]
    ).fetchall()
    return [dict(zip(columns, row, strict=True)) for row in records]


def _image_available(image: str, authfile: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "skopeo",
                "inspect",
                "--authfile",
                str(authfile),
                "docker://" + image,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _task(row: dict[str, Any], *, image_prefix: str, row_number: int) -> dict[str, Any]:
    repo = str(row.get("repo_name") or "").strip()
    commit = str(row.get("commit_hash") or "").strip()
    query = str(row.get("problem_statement") or "").strip()
    execution = row.get("execution_result_content")
    expected = row.get("expected_output_json")
    if not all((repo, commit, query, execution, expected)):
        raise ValueError("R2E row is missing executable fields")
    if isinstance(execution, str):
        execution = json.loads(execution)
    if isinstance(expected, str):
        expected = json.loads(expected)
    if not isinstance(execution, dict) or not isinstance(expected, dict):
        raise ValueError("R2E evaluator fields must be JSON objects")
    names = execution.get("test_file_names")
    codes = execution.get("test_file_codes")
    if not isinstance(names, list) or not isinstance(codes, list) or len(names) != len(codes):
        raise ValueError("R2E evaluator test files are incomplete")
    image = _internal_image(row.get("docker_image"), image_prefix)
    task_id = f"{repo}_final_{commit}"
    return {
        "task_id": task_id,
        "query": query,
        "benchmark_family": "r2e",
        "repo_ref": repo,
        "base_commit": commit,
        "sandbox": {"image_ref": image},
        "verifier": {
            "test_file_names": [str(item) for item in names],
            "test_file_codes": [str(item) for item in codes],
            "expected_tests": [
                {"name": str(key), "status": str(value).upper()}
                for key, value in sorted(expected.items())
            ],
        },
        "source": {
            "dataset": "R2E-Gym/filtered_r2e",
            "split": "train",
            "source_row": row_number,
            "source_revision": R2E_SOURCE_SHA256,
            "image_ref_normalized": image,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--image-prefix", default=DEFAULT_IMAGE_PREFIX)
    parser.add_argument("--verify-images", action="store_true")
    parser.add_argument("--authfile", type=Path, default=Path("/root/.docker/config.json"))
    args = parser.parse_args()
    if not 1 <= args.count <= 8:
        raise SystemExit("--count must be between 1 and 8")
    rows = _load_rows(args.manifest)
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, row in enumerate(rows, start=1):
        repo = str(row.get("repo_name") or "").strip()
        if repo:
            grouped.setdefault(repo, []).append((index, row))
    rng = random.Random(args.seed)
    repos = sorted(grouped)
    rng.shuffle(repos)
    selected: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for repo in repos:
        candidates = sorted(grouped[repo], key=lambda item: str(item[1].get("commit_hash") or ""))
        for row_number, row in candidates:
            try:
                task = _task(row, image_prefix=args.image_prefix, row_number=row_number)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if args.verify_images and not _image_available(
                task["sandbox"]["image_ref"], args.authfile
            ):
                unavailable.append(task["task_id"])
                continue
            selected.append(task)
            break
        if len(selected) >= args.count:
            break
    if len(selected) < args.count:
        raise RuntimeError(
            f"only {len(selected)} executable R2E repositories found; "
            f"unavailable={len(unavailable)}"
        )
    payload = {
        "schema_version": "openreward.r2e_task_pool.v1",
        "experiment": "four-domain-online-mopd-formal-r1",
        "selection": {
            "count": args.count,
            "seed": args.seed,
            "holdout_policy": "R2E-Gym train shards only; no benchmark test/dev rows",
        },
        "source_provenance": {
            "kind": "r2e_gym_train_manifest",
            "manifest_glob": args.manifest,
            "source_revision": R2E_SOURCE_SHA256,
            "image_prefix": args.image_prefix,
            "selected_count": args.count,
            "selected_task_ids": [task["task_id"] for task in selected],
            "selected_repositories": sorted({task["repo_ref"] for task in selected}),
            "image_preflight": args.verify_images,
        },
        "environments": {"swe": {"train": selected}},
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": "completed",
                "output": str(output),
                "count": len(selected),
                "task_ids": [task["task_id"] for task in selected],
                "image_preflight": args.verify_images,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
