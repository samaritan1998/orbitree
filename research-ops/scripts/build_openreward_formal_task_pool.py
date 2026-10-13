#!/usr/bin/env python3
"""Materialize the first formal four-domain OpenReward task pool.

The pool is a task manifest, not a replay dataset.  It records immutable
source lineage and task specifications; the policy samples are generated
online by ``run_openreward_formal_iteration.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIAODA = Path(
    "/srv/research-ops/projects/"
    "example-app-data/data/miaoda_raw_query/miaoda_raw_query_train64.jsonl"
)
DEFAULT_SWE = PROJECT_ROOT / "data/example-swe-manifests/manifests/swegym_train.parquet"
DEFAULT_PPT_SELECTION = PROJECT_ROOT / "config/openreward-ppt-slideforge-0629-selection.json"
DEFAULT_PPT_BUNDLES = PROJECT_ROOT / "data/example-ppt-bundles/bundles"
DEFAULT_TOOLUSE_ROOT = Path(
    "/srv/research-ops/projects/openreward-upstreams/"
    "toolathlon-gym"
)
DEFAULT_MIAODA_URL = (
    "https://warehouse.example.com/datasets/"
    "20260717_daily-2026-07-15"
)
DEFAULT_SWE_URL = "https://huggingface.co/datasets/SWE-Gym/SWE-Gym"
SWEGYM_HARNESS_REVISION = "242429c188fcfd06aad13fce9a54d450470bf0ac"
DEFAULT_TOOLUSE_URL = (
    "https://github.com/EnvCommons/toolathlon-gym/tree/"
    "9c82859d05bc1f0376150d749cd2e3d43471d583/tasks/finalpool"
)
DEFAULT_PPT_URL = "https://knowledge.example.com/d/QFmBn5jD08xeqm"
TOOLUSE_REVISION = "9c82859d05bc1f0376150d749cd2e3d43471d583"
DEFAULT_TOOLUSE_TASKS = (
    "canvas-assessment-quality-audit",
    "canvas-assignment-feedback-gform-word",
    "canvas-at-risk-intervention",
    "canvas-course-feedback",
    "canvas-exam-prep-scheduler",
    "canvas-grade-summary",
    "canvas-learning-analytics",
    "canvas-module-completion-notion-gcal",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse JSON: {path}") from exc


def _array(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, list):
        raise ValueError("SWE verifier field is not an array")
    return [str(item) for item in value]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not an object")
            rows.append(value)
    return rows


def _materialize_miaoda(path: Path, count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _read_jsonl(path)
    eligible = [
        row
        for row in rows
        if str((row.get("metadata") or {}).get("split") or "") == "train"
        and str(row.get("query") or "").strip()
    ]
    if len(eligible) < count:
        raise ValueError(f"Miaoda pool has {len(eligible)} train rows, need {count}")
    tasks: list[dict[str, Any]] = []
    for index, row in enumerate(eligible[:count]):
        metadata = dict(row.get("metadata") or {})
        task = {
            "id": str(row.get("id") or f"miaoda-{index}"),
            "episode_id": f"formal-mopd-r1-miaoda-{index:02d}",
            "query": str(row["query"]),
            "metadata": metadata,
        }
        tasks.append(task)
    return tasks, {
        "kind": "miaoda_agentos_raw_query_train",
        "source_path": str(path.resolve()),
        "source_sha256": sha256_file(path),
        "source_batch_id": str(
            (eligible[0].get("metadata") or {}).get("source_batch_id") or ""
        ),
        "split": "train",
        "holdout_exclusion": "example-app-300 query hashes were excluded upstream",
        "source_url": DEFAULT_MIAODA_URL,
        "selected_count": count,
        "available_train_rows": len(eligible),
    }


def _swe_row_to_task(row: dict[str, Any]) -> dict[str, Any]:
    patch = str(row.get("patch") or "")
    instance_id = str(row.get("instance_id") or "").strip()
    if not instance_id or not patch:
        raise ValueError("SWE row is missing instance_id or patch")
    version = str(row.get("version") or "").strip()
    test_patch = str(row.get("test_patch") or "")
    repo = str(row.get("repo") or "").strip()
    base_commit = str(row.get("base_commit") or "").strip()
    query = str(row.get("problem_statement") or "").strip()
    if not all((version, test_patch.strip(), repo, base_commit, query)):
        raise ValueError(f"SWE-Gym row is missing executable fields: {instance_id}")
    return {
        "base_commit": base_commit,
        "benchmark_family": "swegym",
        "gold_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "query": query,
        "repo_ref": repo,
        "sandbox": {
            "image_ref": (
                "xingyaoww/sweb.eval.x86_64."
                f"{instance_id.casefold().replace('__', '_s_')}:latest"
            )
        },
        "setup": {
            "environment_setup_commit": str(
                row.get("environment_setup_commit") or base_commit
            ),
            "version": version,
        },
        "task_id": instance_id,
        "verifier": {
            "fail_to_pass": _array(row.get("FAIL_TO_PASS")),
            "pass_to_pass": _array(row.get("PASS_TO_PASS")),
            "test_patch": test_patch,
        },
    }


def _materialize_swe(
    path: Path, count: int, seed: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - runtime image dependency
        raise RuntimeError("duckdb is required to build the SWE formal pool") from exc
    connection = duckdb.connect()
    columns = [
        item[0]
        for item in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
    ]
    rows = connection.execute("SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
    records = [dict(zip(columns, row, strict=True)) for row in rows]
    records = [
        row
        for row in records
        if str(row.get("instance_id") or "").strip()
        and str(row.get("patch") or "").strip()
        and str(row.get("problem_statement") or "").strip()
        and str(row.get("repo") or "").strip()
        and str(row.get("base_commit") or "").strip()
        and str(row.get("version") or "").strip()
        and str(row.get("test_patch") or "").strip()
    ]
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        by_repo.setdefault(str(row.get("repo") or ""), []).append(row)
    rng = random.Random(seed)
    repos = sorted(by_repo)
    rng.shuffle(repos)
    chosen: list[dict[str, Any]] = []
    for repo in repos:
        candidates = sorted(by_repo[repo], key=lambda item: str(item.get("instance_id")))
        chosen.append(candidates[0])
        if len(chosen) == count:
            break
    if len(chosen) < count:
        remaining = [row for row in records if row not in chosen]
        remaining.sort(key=lambda item: str(item.get("instance_id")))
        chosen.extend(remaining[: count - len(chosen)])
    if len(chosen) < count:
        raise ValueError(f"SWE train manifest has {len(chosen)} usable rows, need {count}")
    tasks = [_swe_row_to_task(row) for row in chosen[:count]]
    return tasks, {
        "kind": "swegym_train_manifest",
        "source_path": str(path.resolve()),
        "source_sha256": sha256_file(path),
        "split": "train",
        "source_url": DEFAULT_SWE_URL,
        "dataset_revision": "bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb",
        "harness_revision": SWEGYM_HARNESS_REVISION,
        "selected_count": count,
        "selected_task_ids": [task["task_id"] for task in tasks],
        "selected_repositories": sorted({task["repo_ref"] for task in tasks}),
    }


def _git_revision(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _materialize_tooluse(path: Path, count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if count > len(DEFAULT_TOOLUSE_TASKS):
        raise ValueError("formal ToolUse pool currently has only eight pinned tasks")
    tasks: list[dict[str, Any]] = []
    selected_names = DEFAULT_TOOLUSE_TASKS[:count]
    for task_name in selected_names:
        root = path / "tasks" / "finalpool" / task_name
        query_path = root / "docs" / "task.md"
        config_path = root / "task_config.json"
        preprocess_path = root / "preprocess" / "main.py"
        evaluator_path = root / "evaluation" / "main.py"
        for required in (query_path, config_path, preprocess_path, evaluator_path):
            if not required.is_file():
                raise FileNotFoundError(f"ToolUse task asset is missing: {required}")
        task_config = load_json(config_path)
        servers = task_config.get("needed_mcp_servers")
        if not isinstance(servers, list) or not servers:
            raise ValueError(f"ToolUse task has no MCP server list: {task_name}")
        task = {
            "task_id": task_name,
            "task_name": task_name,
            "query": query_path.read_text(encoding="utf-8").strip(),
            "benchmark_family": "toolathlon_gym",
            "tool_servers": [str(item).strip() for item in servers if str(item).strip()],
            "reward_spec": {
                "type": "binary",
                "terminal_action": "claim_done",
                "evaluator": "official_task_script",
            },
            "source": {
                "repository": "EnvCommons/toolathlon-gym",
                "revision": TOOLUSE_REVISION,
                "preprocess_sha256": sha256_file(preprocess_path),
                "evaluator_sha256": sha256_file(evaluator_path),
            },
        }
        tasks.append(task)
    observed_revision = _git_revision(path)
    if observed_revision and observed_revision != TOOLUSE_REVISION:
        raise ValueError(
            f"Toolathlon checkout is at {observed_revision}, expected {TOOLUSE_REVISION}"
        )
    return tasks, {
        "kind": "toolathlon_gym_finalpool",
        "source_path": str(path.resolve()),
        "source_tree_revision": observed_revision,
        "pinned_revision": TOOLUSE_REVISION,
        "split": "finalpool_training_subset",
        "source_url": DEFAULT_TOOLUSE_URL,
        "selected_count": count,
        "selected_task_ids": selected_names,
    }


def _materialize_ppt(
    selection_path: Path, bundles_root: Path, count: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selection = load_json(selection_path)
    if not isinstance(selection, dict) or not isinstance(selection.get("tasks"), list):
        raise ValueError("PPT selection must contain a tasks array")
    selected = selection["tasks"][:count]
    if len(selected) < count:
        raise ValueError(f"PPT selection has {len(selected)} tasks, need {count}")
    tasks: list[dict[str, Any]] = []
    bundle_hashes: dict[str, str] = {}
    for item in selected:
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("PPT selection contains a task without task_id")
        task_path = bundles_root / task_id / "task.json"
        task = load_json(task_path)
        if not isinstance(task, dict):
            raise ValueError(f"PPT task is not an object: {task_path}")
        task["training_lineage"] = {
            "source_batch": selection.get("source_batch"),
            "source_run_id": selection.get("source_run_id"),
            "selection_reason": item.get("selection_reason"),
            "task_json_sha256": sha256_file(task_path),
        }
        tasks.append(task)
        bundle_hashes[task_id] = str(
            item.get("asset_bundle_sha256")
            or (task.get("source") or {}).get("asset_bundle_sha256")
            or ""
        )
    return tasks, {
        "kind": "slideforge_ppt_train_bundles",
        "source_path": str(selection_path.resolve()),
        "source_sha256": sha256_file(selection_path),
        "bundle_root": str(bundles_root.resolve()),
        "split": "train",
        "source_batch": selection.get("source_batch"),
        "source_run_id": selection.get("source_run_id"),
        "source_url": DEFAULT_PPT_URL,
        "selected_count": count,
        "selected_task_ids": [task["task_id"] for task in tasks],
        "asset_bundle_sha256": bundle_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count-per-domain", type=int, default=8)
    parser.add_argument("--miaoda", type=Path, default=DEFAULT_MIAODA)
    parser.add_argument("--swe", type=Path, default=DEFAULT_SWE)
    parser.add_argument("--ppt-selection", type=Path, default=DEFAULT_PPT_SELECTION)
    parser.add_argument("--ppt-bundles", type=Path, default=DEFAULT_PPT_BUNDLES)
    parser.add_argument("--tooluse-root", type=Path, default=DEFAULT_TOOLUSE_ROOT)
    parser.add_argument("--seed", type=int, default=20260802)
    args = parser.parse_args()
    if args.count_per_domain < 1 or args.count_per_domain > 8:
        raise SystemExit("--count-per-domain must be between 1 and 8")
    for path in (args.miaoda, args.swe, args.ppt_selection, args.ppt_bundles, args.tooluse_root):
        if not path.exists():
            raise FileNotFoundError(f"source path does not exist: {path}")

    miaoda, miaoda_source = _materialize_miaoda(args.miaoda, args.count_per_domain)
    swe, swe_source = _materialize_swe(args.swe, args.count_per_domain, args.seed)
    tooluse, tooluse_source = _materialize_tooluse(args.tooluse_root, args.count_per_domain)
    ppt, ppt_source = _materialize_ppt(
        args.ppt_selection, args.ppt_bundles, args.count_per_domain
    )
    payload = {
        "schema_version": "openreward.formal_task_pool.v1",
        "experiment": "four-domain-online-mopd-formal-r1",
        "selection": {
            "count_per_domain": args.count_per_domain,
            "seed": args.seed,
            "holdout_policy": {
                "miaoda": "example-app-300 hashes excluded upstream",
                "swe": "swegym_train only; SWE-bench test/dev manifests excluded",
                "ppt": "closure-filtered training bundles from source batch 0629",
                "tooluse": "pinned finalpool subset; evaluation result files are not copied",
            },
        },
        "source_provenance": {
            "miaoda": miaoda_source,
            "swe": swe_source,
            "tooluse": tooluse_source,
            "ppt": ppt_source,
        },
        "environments": {
            "miaoda": {"train": miaoda},
            "swe": {"train": swe},
            "tooluse": {"train": tooluse},
            "ppt": {"train": ppt},
        },
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "status": "completed",
        "output": str(output),
        "sha256": sha256_file(output),
        "counts": {
            domain: len(payload["environments"][domain]["train"])
            for domain in payload["environments"]
        },
        "source_batches": {
            "miaoda": miaoda_source.get("source_batch_id"),
            "ppt": ppt_source.get("source_batch"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
