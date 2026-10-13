"""Materialize real SlideForge tasks into server-owned OpenReward bundles."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .ppt_manifest import build_ppt_online_task
from .ppt_runtime import inspect_pptx


class SlideForgeTaskMaterializationError(RuntimeError):
    """Raised when a selected SlideForge task is incomplete or inconsistent."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SlideForgeTaskMaterializationError(
            f"invalid JSON input: {path.name}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SlideForgeTaskMaterializationError(f"JSON input is not an object: {path.name}")
    return dict(payload)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: Any) -> Path:
    path = Path(str(value or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SlideForgeTaskMaterializationError("task asset path is not safely relative")
    return path


def _copy_file(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise SlideForgeTaskMaterializationError(f"task asset is missing: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_task_asset(query_root: Path, inputs_root: Path, relative: Path) -> None:
    source = (query_root / relative).resolve()
    if not source.is_relative_to(query_root.resolve()):
        raise SlideForgeTaskMaterializationError("task asset escaped the query root")
    if relative.parts[0] == "references" and len(relative.parts) > 2:
        source_dir = query_root / relative.parts[0] / relative.parts[1]
        destination_dir = inputs_root / relative.parts[0] / relative.parts[1]
        if not source_dir.is_dir():
            raise SlideForgeTaskMaterializationError(
                f"reference asset directory is missing: {relative.parts[1]}"
            )
        shutil.copytree(source_dir, destination_dir, dirs_exist_ok=True, copy_function=shutil.copy2)
        return
    _copy_file(source, inputs_root / relative)


def _single_stage(batch_root: Path, prefix: str, leaf: str) -> Path:
    matches = sorted(
        path / leaf
        for path in batch_root.glob(f"{prefix}_*")
        if path.is_dir() and (path / leaf).is_dir()
    )
    if len(matches) != 1:
        raise SlideForgeTaskMaterializationError(
            f"expected one {prefix} stage with {leaf}, found {len(matches)}"
        )
    return matches[0]


def _task_rows(task_file: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    try:
        lines = task_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SlideForgeTaskMaterializationError("SlideForge tasks.jsonl is unavailable") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SlideForgeTaskMaterializationError("SlideForge tasks.jsonl is invalid") from exc
        if not isinstance(row, Mapping):
            continue
        task_id = str(row.get("task_id") or "").strip()
        if task_id:
            rows[task_id] = dict(row)
    return rows


def _selected_tasks(selection: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = selection.get("tasks")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SlideForgeTaskMaterializationError("selection has no task list")
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise SlideForgeTaskMaterializationError("selection task is not an object")
        task_id = str(item.get("task_id") or "").strip()
        if not task_id or task_id in seen:
            raise SlideForgeTaskMaterializationError("selection task IDs must be unique")
        seen.add(task_id)
        tasks.append(
            {
                "task_id": task_id,
                "selection_reason": str(item.get("selection_reason") or "").strip(),
            }
        )
    if not tasks:
        raise SlideForgeTaskMaterializationError("selection is empty")
    return tasks


def _prompt_path(result_dir: Path) -> Path:
    prompts = sorted((result_dir / "prompts").glob("attempt*.claude_prompt.md"))
    if not prompts:
        raise SlideForgeTaskMaterializationError("successful rollout prompt is missing")
    return prompts[-1]


def _normalized_prompt(prompt_path: Path, query_root: Path, result_dir: Path) -> str:
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt = prompt.replace(str(query_root.resolve()), "/inputs")
    prompt = prompt.replace(
        str((result_dir / "slides.pptx").resolve()),
        "/workspace/slides.pptx",
    )
    if str(query_root.resolve()) in prompt or str(result_dir.resolve()) in prompt:
        raise SlideForgeTaskMaterializationError("rollout prompt retained source host paths")
    if re.search(r"/(?:mnt|tmp)/[^\s]+", prompt):
        raise SlideForgeTaskMaterializationError("rollout prompt retained an unexpected host path")
    return prompt.strip()


def _task_asset_paths(row: Mapping[str, Any]) -> list[Path]:
    values: list[Any] = []
    references = row.get("reference_paths")
    if isinstance(references, Sequence) and not isinstance(references, (str, bytes)):
        values.extend(references)
    values.extend((row.get("style_reference_pptx"), row.get("current_pptx")))
    return [_safe_relative_path(value) for value in values if str(value or "").strip()]


def _materialized_rubric_input(source: Path, query_root: Path) -> dict[str, Any]:
    payload = _load_json(source)

    def normalize(value: Any) -> str:
        path = Path(str(value or ""))
        if path.is_absolute():
            try:
                relative = path.resolve().relative_to(query_root.resolve())
            except ValueError as exc:
                raise SlideForgeTaskMaterializationError(
                    "rubric input path escaped the query root"
                ) from exc
        else:
            relative = _safe_relative_path(path)
        if relative.parts[0] == "inputs":
            return relative.as_posix()
        return (Path("inputs") / relative).as_posix()

    for key in ("REFERENCE_PATHS", "reference_paths"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = [normalize(value)]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            payload[key] = [normalize(item) for item in value if str(item or "").strip()]
    for key in (
        "REFERENCE_PPT",
        "reference_ppt",
        "style_reference_pptx",
        "CURRENT_PPT",
        "current_ppt",
        "current_pptx",
    ):
        if str(payload.get(key) or "").strip():
            payload[key] = normalize(payload[key])
    return payload


def _materialize_one(
    *,
    task_id: str,
    selection_reason: str,
    row: Mapping[str, Any],
    run_root: Path,
    batch_name: str,
    query_root: Path,
    rollout_results: Path,
    rubric_dir: Path,
    evaluation_dir: Path,
    bundle_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result_dir = rollout_results / task_id
    artifact_path = result_dir / "slides.pptx"
    rubric_path = rubric_dir / f"{task_id}.json"
    evaluation_path = evaluation_dir / f"{task_id}.json"
    rubric_input_relative = _safe_relative_path(row.get("rubric_input_path"))
    rubric_input_path = query_root / rubric_input_relative
    for required in (artifact_path, rubric_path, evaluation_path, rubric_input_path):
        if not required.is_file():
            raise SlideForgeTaskMaterializationError(
                f"selected task {task_id} is missing {required.name}"
            )

    evaluation = _load_json(evaluation_path)
    confidence = evaluation.get("evaluation_confidence")
    if not isinstance(confidence, Mapping) or confidence.get("training_usable") is not True:
        raise SlideForgeTaskMaterializationError(
            f"selected task {task_id} is not a training-usable positive control"
        )
    reward = evaluation.get("reward_score")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        raise SlideForgeTaskMaterializationError(
            f"selected task {task_id} has no numeric historical reward"
        )

    prompt = _normalized_prompt(_prompt_path(result_dir), query_root, result_dir)
    prompt_sha256 = _sha256_bytes(prompt.encode("utf-8"))
    task_record_sha256 = _sha256_bytes(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    artifact_raw = artifact_path.read_bytes()
    inspection = inspect_pptx(artifact_raw)

    bundle_dir = bundle_root / task_id
    inputs_root = bundle_dir / "inputs"
    inputs_root.mkdir(parents=True, exist_ok=True)
    for relative in _task_asset_paths(row):
        _copy_task_asset(query_root, inputs_root, relative)

    _copy_file(rubric_path, bundle_dir / "rubric.json")
    normalized_rubric_input = _materialized_rubric_input(rubric_input_path, query_root)
    _write_json(bundle_dir / "rubric_input.json", normalized_rubric_input)
    normalized_input_paths = list(
        normalized_rubric_input.get("REFERENCE_PATHS")
        or normalized_rubric_input.get("reference_paths")
        or []
    )
    for key in (
        "REFERENCE_PPT",
        "reference_ppt",
        "style_reference_pptx",
        "CURRENT_PPT",
        "current_ppt",
        "current_pptx",
    ):
        if str(normalized_rubric_input.get(key) or "").strip():
            normalized_input_paths.append(normalized_rubric_input[key])
    for path_value in normalized_input_paths:
        if not (bundle_dir / _safe_relative_path(path_value)).is_file():
            raise SlideForgeTaskMaterializationError(
                f"normalized rubric input for {task_id} references a missing file"
            )
    historical = bundle_dir / "historical"
    _copy_file(artifact_path, historical / "slides.pptx")
    _copy_file(evaluation_path, historical / "evaluation.json")
    rendered_dir = result_dir / "rendered"
    if rendered_dir.is_dir():
        shutil.copytree(
            rendered_dir,
            historical / "rendered",
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
        )
    (bundle_dir / "prompt.md").write_text(prompt + "\n", encoding="utf-8")

    task = build_ppt_online_task(
        {
            "messages": [{"role": "user", "content": prompt}],
            "metadata": {
                "original_id": task_id,
                "source_line": 0,
                "prompt_sha256": prompt_sha256,
            },
        }
    )
    asset_manifest = []
    for asset in task["assets"]:
        relative = _safe_relative_path(str(asset["path"]).removeprefix("/inputs/"))
        materialized = inputs_root / relative
        digest = _sha256_file(materialized)
        size = materialized.stat().st_size
        asset["sha256"] = digest
        asset["bytes"] = size
        asset_manifest.append(
            {
                "path": asset["path"],
                "role": asset["role"],
                "sha256": digest,
                "bytes": size,
            }
        )
    asset_bundle_sha256 = _sha256_bytes(
        json.dumps(asset_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    axes = row.get("task_axes")
    task["task_axes"] = dict(axes) if isinstance(axes, Mapping) else {}
    if task["task_axes"].get("interaction_mode") == "edit":
        task["verifier"]["min_slides"] = inspection.slides
        task["verifier"]["max_slides"] = inspection.slides
    if not task["verifier"]["min_slides"] <= inspection.slides <= task["verifier"]["max_slides"]:
        raise SlideForgeTaskMaterializationError(
            f"historical output for {task_id} violates the catalog slide range"
        )
    task["source"] = {
        "provenance_status": "confirmed_task_bundle",
        "asset_status": "materialized_task_bundle",
        "source_run_id": run_root.name,
        "source_batch": batch_name,
        "task_record_sha256": task_record_sha256,
        "prompt_sha256": prompt_sha256,
        "rubric_sha256": _sha256_file(rubric_path),
        "rubric_input_sha256": _sha256_file(bundle_dir / "rubric_input.json"),
        "asset_bundle_sha256": asset_bundle_sha256,
        "historical_artifact_sha256": inspection.sha256,
        "historical_reward": float(reward),
        "historical_training_usable": True,
        "selection_reason": selection_reason,
    }
    _write_json(bundle_dir / "task.json", task)
    return task, {
        "task_id": task_id,
        "task_axes": task["task_axes"],
        "assets": len(task["assets"]),
        "asset_bundle_sha256": asset_bundle_sha256,
        "historical_reward": float(reward),
        "historical_slides": inspection.slides,
        "historical_artifact_sha256": inspection.sha256,
    }


def materialize_slideforge_tasks(
    *,
    run_root: Path,
    selection_path: Path,
    bundle_root: Path,
    catalog_path: Path,
) -> dict[str, Any]:
    """Materialize a pinned SlideForge selection and write its ORS catalog."""

    run_root = run_root.expanduser().resolve()
    bundle_root = bundle_root.expanduser().resolve()
    selection = _load_json(selection_path.expanduser().resolve())
    batch_name = str(selection.get("source_batch") or "").strip()
    if not re.fullmatch(r"batch_\d{3}", batch_name):
        raise SlideForgeTaskMaterializationError("selection source_batch is invalid")
    expected_run = str(selection.get("source_run_id") or "").strip()
    if expected_run and expected_run != run_root.name:
        raise SlideForgeTaskMaterializationError("selection source_run_id does not match run root")
    batch_root = run_root / batch_name
    query_root = batch_root / "query"
    rows = _task_rows(query_root / "tasks.jsonl")
    rollout_results = _single_stage(batch_root, "rollout", "results")
    rubric_dir = _single_stage(batch_root, "rubric", "rubrics")
    evaluation_dir = _single_stage(batch_root, "reward", "evaluations")

    tasks: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []
    for selected in _selected_tasks(selection):
        task_id = selected["task_id"]
        row = rows.get(task_id)
        if row is None:
            raise SlideForgeTaskMaterializationError(
                f"selected task {task_id} is absent from tasks.jsonl"
            )
        task, manifest = _materialize_one(
            task_id=task_id,
            selection_reason=selected["selection_reason"],
            row=row,
            run_root=run_root,
            batch_name=batch_name,
            query_root=query_root,
            rollout_results=rollout_results,
            rubric_dir=rubric_dir,
            evaluation_dir=evaluation_dir,
            bundle_root=bundle_root,
        )
        tasks.append(task)
        manifest_rows.append(manifest)

    catalog = {"environments": {"ppt": {"train": tasks}}}
    _write_json(catalog_path.expanduser().resolve(), catalog)
    manifest = {
        "schema_version": "openreward.ppt.slideforge_bundle.v1",
        "source_run_id": run_root.name,
        "source_batch": batch_name,
        "tasks": manifest_rows,
    }
    _write_json(bundle_root / "manifest.json", manifest)
    return manifest
