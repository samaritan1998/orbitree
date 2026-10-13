#!/usr/bin/env python3
"""Build a lightweight Slime prompt manifest over all executable ORS tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from build_openreward_r2e_task_pool import _task as build_r2e_task
from research_ops.openreward.toolathlon_manifest import build_toolathlon_task

DOMAIN_ORDER = ("miaoda", "swe", "ppt", "tooluse")
DOMAIN_MODES = {
    "miaoda": "managed",
    "swe": "interactive",
    "ppt": "interactive",
    "tooluse": "interactive",
}
STORE_SCHEMA = "openreward.dynamic_task_store.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {
                "api_key",
                "authorization",
                "cookie",
                "credential",
                "password",
                "secret",
                "token",
            } or normalized.endswith(("_api_key", "_password", "_secret", "_token")):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _object(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if _contains_secret_key(value):
        raise ValueError(f"{label} contains a credential-like key")
    return value


def _stable_id(task: Mapping[str, Any]) -> str:
    for key in ("task_id", "episode_id", "case_id", "id"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    raise ValueError("OpenReward task has no stable ID")


def _miaoda_tasks(source: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    path = Path(str(source.get("path") or ""))
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"Miaoda source row {line_number} is not an object")
            metadata = row.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("split") != "train":
                raise ValueError(f"Miaoda source row {line_number} is not a train row")
            query = str(row.get("query") or "").strip()
            task_id = str(row.get("id") or "").strip()
            if not query or not task_id:
                raise ValueError(f"Miaoda source row {line_number} is incomplete")
            yield {
                "id": task_id,
                "episode_id": task_id,
                "query": query,
                "metadata": dict(metadata),
            }


def _swe_tasks(source: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("duckdb is required to materialize R2E tasks") from exc
    pattern = str(source.get("pattern") or "").strip()
    image_prefix = str(source.get("image_prefix") or "").strip()
    connection = duckdb.connect()
    cursor = connection.execute(
        """
        SELECT repo_name, docker_image, commit_hash, problem_statement,
               execution_result_content, expected_output_json
        FROM read_parquet(?, union_by_name=true)
        """,
        [pattern],
    )
    row_number = 0
    columns = [item[0] for item in cursor.description]
    while rows := cursor.fetchmany(64):
        for values in rows:
            row_number += 1
            row = dict(zip(columns, values, strict=True))
            yield build_r2e_task(
                row,
                image_prefix=image_prefix,
                row_number=row_number,
            )
    connection.close()


def _ppt_tasks(source: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    root = Path(str(source.get("root") or ""))
    for task_path in sorted(root.glob("*/task.json")):
        task = _object(task_path, label="PPT task")
        if _stable_id(task) != task_path.parent.name:
            raise ValueError(f"PPT task ID does not match bundle directory: {task_path}")
        yield task


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _tooluse_tasks(source: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    root = Path(str(source.get("root") or ""))
    repository = str(source.get("repository") or "").strip()
    revision = str(source.get("revision") or "").strip()
    observed = _git_revision(root)
    if revision and observed != revision:
        raise ValueError(
            f"Toolathlon checkout revision is {observed or 'unknown'}, expected {revision}"
        )
    finalpool = root / "tasks" / "finalpool"
    task_roots = (
        item
        for item in finalpool.iterdir()
        if item.is_dir() and not item.name.startswith(".")
    )
    for task_root in sorted(task_roots):
        query_path = task_root / "docs" / "task.md"
        config_path = task_root / "task_config.json"
        preprocess_path = task_root / "preprocess" / "main.py"
        evaluator_path = task_root / "evaluation" / "main.py"
        for required in (query_path, config_path, preprocess_path, evaluator_path):
            if not required.is_file():
                raise FileNotFoundError(f"ToolUse task asset is missing: {required}")
        yield build_toolathlon_task(
            task_name=task_root.name,
            query=query_path.read_text(encoding="utf-8"),
            task_config=_object(config_path, label="ToolUse task config"),
            repository=repository,
            revision=revision,
            preprocess_sha256=_sha256(preprocess_path),
            evaluator_sha256=_sha256(evaluator_path),
        )


def _tasks(domain: str, source: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    kind = str(source.get("kind") or "").strip()
    expected = {
        "miaoda": "miaoda_jsonl",
        "swe": "r2e_parquet",
        "ppt": "ppt_task_bundles",
        "tooluse": "toolathlon_finalpool",
    }[domain]
    if kind != expected:
        raise ValueError(f"dynamic source {domain} must use kind={expected}")
    loaders = {
        "miaoda": _miaoda_tasks,
        "swe": _swe_tasks,
        "ppt": _ppt_tasks,
        "tooluse": _tooluse_tasks,
    }
    yield from loaders[domain](source)


def _prompt_row(
    *,
    domain: str,
    source_task_id: str,
    store_path: Path,
    offset: int,
    payload: bytes,
    pure_opd: bool,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "openreward_env": domain,
        "openreward_mode": DOMAIN_MODES[domain],
        "openreward_source_task_id": source_task_id,
        "openreward_task_id": source_task_id,
        "openreward_task_ref": {
            "path": str(store_path),
            "offset": offset,
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    if pure_opd:
        metadata.update(
            {
                "openreward_pure_opd": True,
                "openreward_complete_on_turn_limit": True,
                "openreward_require_logprobs": True,
                "openreward_require_teacher_logprobs": True,
            }
        )
    return {
        "input": [
            {
                "role": "user",
                "content": "Start the assigned OpenReward environment task.",
            }
        ],
        "metadata": metadata,
    }


def _reuse_receipt(
    output_dir: Path,
    *,
    manifest_sha256: str,
    training_objective: str,
) -> dict[str, Any] | None:
    receipt_path = output_dir / "receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = _object(receipt_path, label="dynamic task-store receipt")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        receipt.get("schema_version") != STORE_SCHEMA
        or receipt.get("source_manifest_sha256") != manifest_sha256
        or receipt.get("training_objective") != training_objective
    ):
        return None
    artifacts = (
        ("tasks.jsonl", "task_store_bytes", "task_store_sha256"),
        ("prompts.jsonl", "prompt_bytes", "prompt_sha256"),
    )
    for name, size_key, digest_key in artifacts:
        path = output_dir / name
        if (
            not path.is_file()
            or path.stat().st_size != receipt.get(size_key)
            or _sha256(path) != receipt.get(digest_key)
        ):
            return None
    return receipt


def build_store(
    source_manifest: Path,
    output_dir: Path,
    *,
    training_objective: str,
    reuse_valid: bool = False,
) -> dict[str, Any]:
    manifest = _object(source_manifest, label="dynamic source manifest")
    if manifest.get("schema_version") != "openreward.dynamic_sources.v1":
        raise ValueError("unsupported dynamic source manifest schema")
    sources = manifest.get("sources")
    if not isinstance(sources, Mapping) or set(sources) != set(DOMAIN_ORDER):
        raise ValueError("dynamic source manifest must define exactly four domains")
    manifest_sha256 = _sha256(source_manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    if reuse_valid and (
        receipt := _reuse_receipt(
            output_dir,
            manifest_sha256=manifest_sha256,
            training_objective=training_objective,
        )
    ):
        return {**receipt, "reused": True}

    pure_opd = training_objective == "pure_opd"
    store_path = (output_dir / "tasks.jsonl").resolve()
    prompts_path = output_dir / "prompts.jsonl"
    counts: Counter[str] = Counter()
    store_digest = hashlib.sha256()
    prompt_digest = hashlib.sha256()
    temporary_paths: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_dir, prefix=".tasks.", suffix=".tmp", delete=False
        ) as store_handle, tempfile.NamedTemporaryFile(
            mode="wb", dir=output_dir, prefix=".prompts.", suffix=".tmp", delete=False
        ) as prompt_handle:
            temporary_paths.extend((Path(store_handle.name), Path(prompt_handle.name)))
            for domain in DOMAIN_ORDER:
                source = sources[domain]
                if not isinstance(source, Mapping):
                    raise ValueError(f"dynamic source {domain} must be an object")
                expected_count = int(source.get("expected_count"))
                for task in _tasks(domain, source):
                    if _contains_secret_key(task):
                        raise ValueError(
                            f"dynamic source task {domain} contains a credential-like key"
                        )
                    source_task_id = _stable_id(task)
                    record = {"domain": domain, "task": task}
                    payload = json.dumps(
                        record,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    offset = store_handle.tell()
                    store_handle.write(payload + b"\n")
                    store_digest.update(payload + b"\n")
                    prompt = _prompt_row(
                        domain=domain,
                        source_task_id=source_task_id,
                        store_path=store_path,
                        offset=offset,
                        payload=payload,
                        pure_opd=pure_opd,
                    )
                    prompt_payload = (
                        json.dumps(
                            prompt,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    prompt_handle.write(prompt_payload)
                    prompt_digest.update(prompt_payload)
                    counts[domain] += 1
                if counts[domain] != expected_count:
                    raise ValueError(
                        f"dynamic source {domain} produced {counts[domain]} tasks, "
                        f"expected {expected_count}"
                    )
            store_handle.flush()
            prompt_handle.flush()
            os.fsync(store_handle.fileno())
            os.fsync(prompt_handle.fileno())
        temporary_paths[0].replace(store_path)
        temporary_paths[1].replace(prompts_path)
        temporary_paths.clear()
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)

    receipt = {
        "schema_version": STORE_SCHEMA,
        "source_manifest": str(source_manifest.resolve()),
        "source_manifest_sha256": manifest_sha256,
        "training_objective": training_objective,
        "counts": dict(counts),
        "rows": sum(counts.values()),
        "task_store": str(store_path),
        "task_store_bytes": store_path.stat().st_size,
        "task_store_sha256": store_digest.hexdigest(),
        "prompt_data": str(prompts_path.resolve()),
        "prompt_bytes": prompts_path.stat().st_size,
        "prompt_sha256": prompt_digest.hexdigest(),
        "reused": False,
    }
    temporary_receipt = output_dir / ".receipt.tmp"
    temporary_receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_receipt.replace(output_dir / "receipt.json")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--training-objective", choices=("pure_opd", "reward"), default="pure_opd"
    )
    parser.add_argument("--reuse-valid", action="store_true")
    args = parser.parse_args()
    receipt = build_store(
        args.source_manifest.expanduser().resolve(),
        args.output_dir.expanduser().resolve(),
        training_objective=args.training_objective,
        reuse_valid=args.reuse_valid,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
