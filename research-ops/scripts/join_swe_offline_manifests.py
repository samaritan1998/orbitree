#!/usr/bin/env python3
"""Join Warehouse SWE trajectories to pinned executable task manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_ops.openreward.swe_manifest import (
    ManifestProvenance,
    join_swe_trajectories,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WAREHOUSE_ROOT = Path(
    "/data/trajectory-warehouse/sft"
)
DEFAULT_MANIFEST_DIR = PROJECT_ROOT / "data" / "example-swe-manifests" / "manifests"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "example-swe-manifests" / "output"
R2E_LOCAL_MANIFEST = DEFAULT_MANIFEST_DIR / "r2e_filtered_compact.jsonl"
R2E_LOCAL_ORIGIN = Path(
    "/srv/research-ops/workspaces/example/storage/r2e_8k/filtered_r2e.jsonl"
)
R2E_LOCAL_ORIGIN_SHA256 = "79e48caff7be979740a96cda596d516387f2e5bc53cb3bd62222ee64db7bd1ea"

SOURCE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "family": "rebench",
        "warehouse_batch": "20260401_sonnet-rebench-swe",
        "dataset_id": "nebius/SWE-rebench",
        "revision": "89cdfbab4ab1bd8f5a658bb212d1b63624f4f881",
        "manifests": (
            ("test", "rebench_test_00.parquet"),
            ("test", "rebench_test_01.parquet"),
        ),
        "columns": (
            "instance_id",
            "repo",
            "base_commit",
            "problem_statement",
            "test_patch",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
            "patch",
            "docker_image",
            "image_name",
            "environment_setup_commit",
            "version",
            "install_config",
            "requirements",
            "environment",
        ),
    },
    {
        "family": "r2e",
        "warehouse_batch": "20260401_sonnet-r2e-swe",
        "dataset_id": "local:R2E-Gym/filtered_r2e",
        "revision": f"sha256:{R2E_LOCAL_ORIGIN_SHA256}",
        "derived_from": {
            "path": str(R2E_LOCAL_ORIGIN),
            "sha256": R2E_LOCAL_ORIGIN_SHA256,
        },
        "manifests": (("train", str(R2E_LOCAL_MANIFEST)),),
        "columns": (
            "instance_id",
            "repo_name",
            "image",
            "base_commit",
            "problem_statement",
            "expected_output_json",
        ),
    },
    {
        "family": "swegym",
        "warehouse_batch": "20260401_sonnet-swegym-swe",
        "dataset_id": "SWE-Gym/SWE-Gym",
        "revision": "bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb",
        "manifests": (("train", "swegym_train.parquet"),),
        "columns": (
            "instance_id",
            "repo",
            "base_commit",
            "problem_statement",
            "test_patch",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
            "patch",
            "version",
        ),
    },
    {
        "family": "swebench",
        "warehouse_batch": "20260401_sonnet-swebench-swe",
        "dataset_id": "princeton-nlp/SWE-bench",
        "revision": "e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6",
        "manifests": (
            ("dev", "swebench_dev.parquet"),
            ("test", "swebench_test.parquet"),
            ("train", "swebench_train.parquet"),
        ),
        "columns": (
            "instance_id",
            "repo",
            "base_commit",
            "problem_statement",
            "test_patch",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
            "patch",
            "environment_setup_commit",
            "version",
        ),
    },
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            yield value


def _quote_column(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_parquet(
    *,
    connection: Any,
    path: Path,
    columns: Sequence[str],
    dataset_id: str,
    revision: str,
    split: str,
    instance_ids: Sequence[str],
) -> Iterator[tuple[Mapping[str, Any], ManifestProvenance]]:
    projection = ", ".join(_quote_column(column) for column in columns)
    cursor = connection.execute(
        f"SELECT {projection} FROM read_parquet(?) "  # noqa: S608 - allow-listed projection
        "WHERE instance_id IN (SELECT unnest(?))",
        [str(path), list(instance_ids)],
    )
    names = tuple(item[0] for item in cursor.description)
    row_number = 0
    while rows := cursor.fetchmany(512):
        for raw_row in rows:
            row_number += 1
            yield (
                dict(zip(names, raw_row, strict=True)),
                ManifestProvenance(
                    dataset_id=dataset_id,
                    revision=revision,
                    split=split,
                    file=path.name,
                    row=row_number,
                ),
            )


def _read_manifest_jsonl(
    *,
    path: Path,
    columns: Sequence[str],
    dataset_id: str,
    revision: str,
    split: str,
    instance_ids: set[str],
) -> Iterator[tuple[Mapping[str, Any], ManifestProvenance]]:
    for row_number, record in enumerate(_read_jsonl(path), start=1):
        if record.get("instance_id") not in instance_ids:
            continue
        yield (
            {column: record.get(column) for column in columns},
            ManifestProvenance(
                dataset_id=dataset_id,
                revision=revision,
                split=split,
                file=str(path),
                row=row_number,
            ),
        )


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse-root", type=Path, default=DEFAULT_WAREHOUSE_ROOT)
    parser.add_argument("--manifest-dir", type=Path, default=DEFAULT_MANIFEST_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--family",
        action="append",
        choices=tuple(spec["family"] for spec in SOURCE_SPECS),
        help="join one family; repeat as needed (default: all)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero unless every warehouse row has one exact match",
    )
    args = parser.parse_args()

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - environment-specific diagnostic
        raise SystemExit("duckdb is required to read official parquet manifests") from exc

    selected = set(args.family or [spec["family"] for spec in SOURCE_SPECS])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    family_summaries: dict[str, Any] = {}
    file_provenance: dict[str, Any] = {}
    combined: list[Mapping[str, Any]] = []

    for spec in SOURCE_SPECS:
        family = spec["family"]
        if family not in selected:
            continue
        warehouse_batch = spec["warehouse_batch"]
        warehouse_file = args.warehouse_root / warehouse_batch / "data.jsonl"
        if not warehouse_file.is_file():
            raise SystemExit(f"missing Warehouse file: {warehouse_file}")
        warehouse_records = [
            {
                "instance_id": record.get("instance_id"),
                "task_id": record.get("task_id"),
                "trajectory_file": record.get("trajectory_file"),
                "is_solved": record.get("is_solved"),
            }
            for record in _read_jsonl(warehouse_file)
        ]
        instance_ids = {
            value
            for record in warehouse_records
            for key in ("instance_id", "task_id")
            if isinstance((value := record.get(key)), str) and value
        }

        manifest_rows: list[tuple[Mapping[str, Any], ManifestProvenance]] = []
        for split, filename in spec["manifests"]:
            manifest_path = Path(filename)
            if not manifest_path.is_absolute():
                manifest_path = args.manifest_dir / manifest_path
            if not manifest_path.is_file():
                raise SystemExit(f"missing task manifest: {manifest_path}")
            manifest_sha256 = _sha256_file(manifest_path)
            revision = spec["revision"]
            if revision == "sha256":
                revision = f"sha256:{manifest_sha256}"
            provenance_key = (
                manifest_path.name
                if manifest_path.is_relative_to(args.manifest_dir)
                else str(manifest_path)
            )
            file_provenance[provenance_key] = {
                "dataset_id": spec["dataset_id"],
                "revision": revision,
                "split": split,
                "bytes": manifest_path.stat().st_size,
                "sha256": manifest_sha256,
            }
            if "derived_from" in spec:
                file_provenance[provenance_key]["derived_from"] = spec["derived_from"]
            if manifest_path.suffix.casefold() == ".jsonl":
                records = _read_manifest_jsonl(
                    path=manifest_path,
                    columns=spec["columns"],
                    dataset_id=spec["dataset_id"],
                    revision=revision,
                    split=split,
                    instance_ids=instance_ids,
                )
            else:
                records = _read_parquet(
                    connection=connection,
                    path=manifest_path,
                    columns=spec["columns"],
                    dataset_id=spec["dataset_id"],
                    revision=revision,
                    split=split,
                    instance_ids=sorted(instance_ids),
                )
            manifest_rows.extend(records)

        result = join_swe_trajectories(
            family=family,
            warehouse_batch=warehouse_batch,
            warehouse_records=warehouse_records,
            manifest_records=manifest_rows,
        )
        _atomic_jsonl(args.output_dir / f"{family}.joined.jsonl", result.joined)
        _atomic_jsonl(args.output_dir / f"{family}.unmatched.jsonl", result.unmatched)
        family_summaries[family] = result.summary
        combined.extend(result.joined)

    _atomic_jsonl(args.output_dir / "swe.joined.jsonl", combined)
    overall_rows = sum(item["warehouse_rows"] for item in family_summaries.values())
    overall_joined = sum(item["joined_rows"] for item in family_summaries.values())
    summary = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "join_policy": "exact_instance_id_only",
        "gold_patch_policy": "sha256_only_not_exposed_to_policy",
        "warehouse_root": str(args.warehouse_root),
        "manifest_dir": str(args.manifest_dir),
        "output_dir": str(args.output_dir),
        "overall": {
            "warehouse_rows": overall_rows,
            "joined_rows": overall_joined,
            "unmatched_rows": overall_rows - overall_joined,
            "coverage": overall_joined / overall_rows if overall_rows else 0.0,
        },
        "families": family_summaries,
        "manifest_files": dict(sorted(file_provenance.items())),
    }
    _atomic_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.strict and overall_joined != overall_rows:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
