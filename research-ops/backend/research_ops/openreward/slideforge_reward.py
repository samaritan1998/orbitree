"""HTTP adapter from the ORS PPT rubric contract to SlideForge Reward."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


class SlideForgeRewardError(RuntimeError):
    """Base error for the SlideForge Reward adapter."""


class SlideForgeRewardTaskError(SlideForgeRewardError):
    """The request does not identify a valid materialized task or artifact."""


class SlideForgeRewardInfrastructureError(SlideForgeRewardError):
    """SlideForge evaluation infrastructure failed or returned invalid output."""


_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,180}")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_GROUNDING_AREA_TERMS = (
    "source faithfulness",
    "hallucination",
    "image usage",
    "reference grounding",
    "prohibited content",
)
_SCRIPT_METHOD_WEIGHT = 0.65


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _positive_int(value: Any, *, name: str, default: int) -> int:
    raw = _text(value)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise SlideForgeRewardInfrastructureError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise SlideForgeRewardInfrastructureError(f"{name} must be positive")
    return parsed


def _absolute_directory(
    environ: Mapping[str, str],
    name: str,
    *,
    create: bool = False,
) -> Path:
    value = _text(environ.get(name))
    path = Path(value).expanduser()
    if not value or not path.is_absolute():
        raise SlideForgeRewardInfrastructureError(f"{name} must be an absolute directory")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SlideForgeRewardInfrastructureError(f"{name} is unavailable") from exc
    if not resolved.is_dir():
        raise SlideForgeRewardInfrastructureError(f"{name} is not a directory")
    return resolved


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = _text(environ.get(name))
    if not value:
        return default
    if value.casefold() in {"1", "true", "yes", "on"}:
        return True
    if value.casefold() in {"0", "false", "no", "off"}:
        return False
    raise SlideForgeRewardInfrastructureError(f"{name} must be boolean")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SlideForgeRewardInfrastructureError(f"{label} is invalid") from exc
    if not isinstance(payload, Mapping):
        raise SlideForgeRewardInfrastructureError(f"{label} is not an object")
    return dict(payload)


def _write_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _number(value: Any, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SlideForgeRewardInfrastructureError(f"{name} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise SlideForgeRewardInfrastructureError(f"{name} must be between 0 and 1")
    return result


@dataclass(frozen=True)
class SlideForgeRewardConfig:
    pipeline_root: Path
    bundle_root: Path
    artifact_root: Path
    work_root: Path
    timeout_seconds: int = 5400
    max_concurrency: int = 1

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> SlideForgeRewardConfig:
        runtime = os.environ if environ is None else environ
        config = cls(
            pipeline_root=_absolute_directory(runtime, "ORS_SLIDEFORGE_PIPELINE_ROOT"),
            bundle_root=_absolute_directory(runtime, "ORS_PPT_TASK_BUNDLE_ROOT"),
            artifact_root=_absolute_directory(runtime, "ORS_PPT_ARTIFACT_ROOT"),
            work_root=_absolute_directory(
                runtime,
                "ORS_SLIDEFORGE_REWARD_WORK_ROOT",
                create=True,
            ),
            timeout_seconds=_positive_int(
                runtime.get("ORS_SLIDEFORGE_REWARD_TIMEOUT_SECONDS"),
                name="ORS_SLIDEFORGE_REWARD_TIMEOUT_SECONDS",
                default=5400,
            ),
            max_concurrency=_positive_int(
                runtime.get("ORS_SLIDEFORGE_REWARD_MAX_CONCURRENCY"),
                name="ORS_SLIDEFORGE_REWARD_MAX_CONCURRENCY",
                default=1,
            ),
        )
        for relative in (
            "pipeline/stages/reward/build_candidate_bundle.py",
            "pipeline/stages/reward/run_reward_with_claude_skill.sh",
            "skills/ppt-reward-evaluator/scripts/validate_evaluation.py",
        ):
            if not (config.pipeline_root / relative).is_file():
                raise SlideForgeRewardInfrastructureError(
                    "ORS_SLIDEFORGE_PIPELINE_ROOT is missing required reward scripts"
                )
        return config


@dataclass(frozen=True)
class SlideForgeRewardRequest:
    task_id: str
    artifact_path: Path
    render_paths: tuple[Path, ...]
    inputs_path: Path
    inspection: dict[str, Any]


class SlideForgeEvaluationRunner(Protocol):
    def evaluate(
        self,
        *,
        task_id: str,
        artifact_path: Path,
        render_paths: Sequence[Path],
        bundle_dir: Path,
        work_dir: Path,
    ) -> Mapping[str, Any]: ...


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


class SlideForgeCommandRunner:
    """Invoke the existing SlideForge bundle builder and Reward workflow."""

    def __init__(
        self,
        config: SlideForgeRewardConfig,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)
        required_groups = {
            "model endpoint": ("API_ENDPOINT", "MDRW_LLM_ENDPOINT"),
            "model credential": ("API_KEY", "MDRW_LLM_API_KEY"),
            "model name": ("MODEL_NAME", "MDRW_LLM_MODEL"),
        }
        missing = [
            label
            for label, names in required_groups.items()
            if not any(_text(self.environ.get(name)) for name in names)
        ]
        if missing:
            raise SlideForgeRewardInfrastructureError(
                "SlideForge Reward is missing injected " + ", ".join(missing)
            )
        for command in ("ccrelay", "claude"):
            if shutil.which(command, path=self.environ.get("PATH")) is None:
                raise SlideForgeRewardInfrastructureError(
                    f"SlideForge Reward runtime is missing {command}"
                )

    def _run(
        self,
        command: Sequence[str],
        *,
        log_path: Path,
        timeout_seconds: int,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        try:
            completed = subprocess.run(
                list(command),
                cwd=self.config.pipeline_root,
                env=dict(environ) if environ is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlideForgeRewardInfrastructureError(
                "SlideForge reward command timed out"
            ) from exc
        except OSError as exc:
            raise SlideForgeRewardInfrastructureError(
                "SlideForge reward command could not start"
            ) from exc
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(completed.stdout or b"")
        if completed.returncode != 0:
            raise SlideForgeRewardInfrastructureError(
                f"SlideForge reward command exited with code {completed.returncode}"
            )

    @staticmethod
    def _style_reference(bundle_dir: Path) -> Path | None:
        task = _load_object(bundle_dir / "task.json", label="materialized task metadata")
        assets = task.get("assets")
        if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
            return None
        for asset in assets:
            if not isinstance(asset, Mapping) or asset.get("role") != "style_reference":
                continue
            value = _text(asset.get("path"))
            if not value.startswith("/inputs/"):
                continue
            candidate = (bundle_dir / "inputs" / value.removeprefix("/inputs/")).resolve()
            if candidate.is_file() and candidate.is_relative_to((bundle_dir / "inputs").resolve()):
                return candidate
        return None

    def evaluate(
        self,
        *,
        task_id: str,
        artifact_path: Path,
        render_paths: Sequence[Path],
        bundle_dir: Path,
        work_dir: Path,
    ) -> Mapping[str, Any]:
        candidate_dir = work_dir / "candidate"
        rendered_dir = candidate_dir / "rendered"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        _link_or_copy(artifact_path, candidate_dir / "slides.pptx")
        for index, source in enumerate(render_paths, start=1):
            _link_or_copy(source, rendered_dir / f"slide_{index:03d}{source.suffix.lower()}")

        candidate_bundle = work_dir / "candidate_bundle.json"
        build_command = [
            sys.executable,
            str(
                self.config.pipeline_root
                / "pipeline/stages/reward/build_candidate_bundle.py"
            ),
            "--result_dir",
            str(candidate_dir),
            "--pptx",
            str(candidate_dir / "slides.pptx"),
            "--output",
            str(candidate_bundle),
        ]
        style_reference = self._style_reference(bundle_dir)
        if style_reference is not None:
            build_command.extend(("--reference_ppt", str(style_reference)))
        self._run(
            build_command,
            log_path=work_dir / "build_candidate_bundle.log",
            timeout_seconds=min(self.config.timeout_seconds, 900),
        )

        evaluator_dir = work_dir / "evaluator"
        reward_environ = dict(self.environ)
        reward_environ.update(
            {
                "RUBRIC_PATH": str(bundle_dir / "rubric.json"),
                "RUBRIC_INPUT_PATH": str(bundle_dir / "rubric_input.json"),
                "CANDIDATE_BUNDLE_PATH": str(candidate_bundle),
                "OUTPUT_DIR": str(evaluator_dir),
                "CCRELAY_LOG_DIR": str(evaluator_dir / "ccrelay"),
            }
        )
        self._run(
            [
                str(
                    self.config.pipeline_root
                    / "pipeline/stages/reward/run_reward_with_claude_skill.sh"
                )
            ],
            log_path=work_dir / "run_reward.log",
            timeout_seconds=self.config.timeout_seconds,
            environ=reward_environ,
        )
        evaluation = _load_object(
            evaluator_dir / "evaluation.json",
            label="SlideForge evaluation",
        )
        if _text(evaluation.get("task_id")) not in {"", task_id}:
            raise SlideForgeRewardInfrastructureError(
                "SlideForge evaluation task ID does not match"
            )
        return evaluation


def _rubric_areas(rubric: Mapping[str, Any]) -> dict[str, str]:
    raw = rubric.get("core_rubric")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SlideForgeRewardInfrastructureError("SlideForge rubric has no core_rubric")
    areas: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        item_id = _text(item.get("item_id"))
        if item_id:
            areas[item_id] = _text(item.get("area")).casefold()
    return areas


def _scored_rows(evaluation: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = evaluation.get("item_results")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise SlideForgeRewardInfrastructureError("SlideForge evaluation has no item results")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        status = _text(item.get("status"))
        if status in {"not_applicable", "not_evaluated"} or item.get(
            "quality_scoring_excluded"
        ):
            continue
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            continue
        value = float(score)
        if 0.0 <= value <= 1.0:
            rows.append(dict(item))
    return rows


def _weighted_mean(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    total = 0.0
    denominator = 0.0
    for row in rows:
        weight = _SCRIPT_METHOD_WEIGHT if row.get("evaluation_method") == "script" else 1.0
        total += float(row.get("score") or 0.0) * weight
        denominator += weight
    return round(total / denominator if denominator else 0.0, 6)


def _item_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [_text(row.get("item_id")) for row in rows if _text(row.get("item_id"))]


def derive_slideforge_dimensions(
    evaluation: Mapping[str, Any],
    rubric: Mapping[str, Any],
    inspection: Mapping[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Derive ORS dimensions from item-level evidence and deterministic OOXML gates."""

    rows = _scored_rows(evaluation)
    areas = _rubric_areas(rubric)
    substance_rows = [row for row in rows if row.get("domain") == "Substance Quality"]
    grounding_rows = [
        row
        for row in rows
        if row.get("domain") in {"Substance Quality", "Requirement Compliance"}
        and any(term in areas.get(_text(row.get("item_id")), "") for term in _GROUNDING_AREA_TERMS)
    ]
    grounding_fallback = False
    if not grounding_rows:
        grounding_rows = substance_rows
        grounding_fallback = True

    requirement_rows = [
        row for row in rows if row.get("domain") == "Requirement Compliance"
    ]
    structure_rows = [
        row
        for row in requirement_rows
        if not any(
            term in areas.get(_text(row.get("item_id")), "")
            for term in _GROUNDING_AREA_TERMS
        )
    ]
    if not structure_rows:
        structure_rows = requirement_rows
    visual_rows = [
        row for row in rows if row.get("domain") == "Presentation Design Quality"
    ]

    raw_results = evaluation.get("item_results")
    iterable_results = (
        raw_results
        if isinstance(raw_results, Sequence) and not isinstance(raw_results, (str, bytes))
        else ()
    )
    result_by_id = {
        _text(item.get("item_id")): item
        for item in iterable_results
        if isinstance(item, Mapping) and _text(item.get("item_id"))
    }
    deterministic_checks = {
        item_id: result_by_id.get(item_id, {}).get("status") == "pass"
        for item_id in ("SYS_OPENABLE_FILE", "SYS_EDITABILITY")
    }
    inspection_editable = (
        int(inspection.get("slides") or 0) > 0
        and int(inspection.get("text_runs") or 0) > 0
        and int(inspection.get("characters") or 0) > 0
    )
    editability = float(all(deterministic_checks.values()) and inspection_editable)
    dimensions = {
        "content_grounding": _weighted_mean(grounding_rows),
        "structure": _weighted_mean(structure_rows),
        "visual_quality": _weighted_mean(visual_rows),
        "editability": editability,
    }
    derivation = {
        "version": "slideforge_item_mapping_v1",
        "script_item_weight": _SCRIPT_METHOD_WEIGHT,
        "content_grounding_item_ids": _item_ids(grounding_rows),
        "content_grounding_fallback_to_substance": grounding_fallback,
        "structure_item_ids": _item_ids(structure_rows),
        "visual_quality_item_ids": _item_ids(visual_rows),
        "editability_checks": {
            **deterministic_checks,
            "inspection_has_editable_text": inspection_editable,
        },
    }
    return dimensions, derivation


def map_slideforge_evaluation(
    evaluation: Mapping[str, Any],
    rubric: Mapping[str, Any],
    inspection: Mapping[str, Any],
    *,
    cache_hit: bool,
) -> dict[str, Any]:
    reward = _number(evaluation.get("reward_score"), name="SlideForge reward_score")
    dimensions, derivation = derive_slideforge_dimensions(evaluation, rubric, inspection)
    confidence = _mapping(evaluation.get("evaluation_confidence"))
    domain_scores = _mapping(evaluation.get("domain_scores"))
    normalized_domains = {
        name: _number(value, name=f"SlideForge domain score {name}")
        for name, value in domain_scores.items()
    }
    required_domains = {
        "Requirement Compliance",
        "Substance Quality",
        "Presentation Design Quality",
    }
    if not required_domains.issubset(normalized_domains):
        raise SlideForgeRewardInfrastructureError(
            "SlideForge evaluation omitted one or more domain scores"
        )
    usable = (
        confidence.get("training_usable") is True
        and confidence.get("evaluation_valid") is True
        and dimensions["editability"] == 1.0
    )
    return {
        "usable": usable,
        "reward": reward,
        "grader_id": "slideforge-ppt-reward-v1",
        "rubric": dimensions,
        "metadata": {
            "slideforge_domain_scores": normalized_domains,
            "evaluation_confidence": confidence,
            "dimension_derivation": derivation,
            "cache_hit": cache_hit,
        },
    }


class SlideForgeRewardService:
    def __init__(
        self,
        config: SlideForgeRewardConfig,
        *,
        runner: SlideForgeEvaluationRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or SlideForgeCommandRunner(config)
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, cache_key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(cache_key, asyncio.Lock())

    @staticmethod
    def _resolve_shared_file(value: Any, root: Path, *, label: str) -> Path:
        raw = _text(value)
        if not raw:
            raise SlideForgeRewardTaskError(f"{label} is required")
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise SlideForgeRewardTaskError(f"{label} is unavailable") from exc
        if not path.is_file() or not path.is_relative_to(root):
            raise SlideForgeRewardTaskError(f"{label} is outside the shared artifact root")
        return path

    def _parse_request(self, payload: Mapping[str, Any]) -> SlideForgeRewardRequest:
        task_id = _text(payload.get("task_id"))
        if not _TASK_ID.fullmatch(task_id):
            raise SlideForgeRewardTaskError("task_id is invalid")
        bundle_dir = (self.config.bundle_root / task_id).resolve()
        if not bundle_dir.is_dir() or not bundle_dir.is_relative_to(self.config.bundle_root):
            raise SlideForgeRewardTaskError("task bundle is unavailable")
        expected_inputs = (bundle_dir / "inputs").resolve()
        try:
            inputs_path = Path(_text(payload.get("inputs_path"))).expanduser().resolve(strict=True)
        except OSError as exc:
            raise SlideForgeRewardTaskError("inputs_path is unavailable") from exc
        if inputs_path != expected_inputs:
            raise SlideForgeRewardTaskError("inputs_path does not match the task bundle")

        artifact_path = self._resolve_shared_file(
            payload.get("artifact_path"),
            self.config.artifact_root,
            label="artifact_path",
        )
        render_values = payload.get("render_paths")
        if not isinstance(render_values, Sequence) or isinstance(render_values, (str, bytes)):
            raise SlideForgeRewardTaskError("render_paths must be a list")
        render_paths = tuple(
            self._resolve_shared_file(value, self.config.artifact_root, label="render_path")
            for value in render_values
        )
        if not render_paths or len(set(render_paths)) != len(render_paths):
            raise SlideForgeRewardTaskError("render_paths must contain unique slide images")
        if any(path.suffix.casefold() not in _IMAGE_SUFFIXES for path in render_paths):
            raise SlideForgeRewardTaskError("render_path has an unsupported image type")

        inspection = _mapping(payload.get("inspection"))
        try:
            slides = int(inspection.get("slides") or 0)
        except (TypeError, ValueError) as exc:
            raise SlideForgeRewardTaskError("inspection.slides must be an integer") from exc
        if slides < 1 or len(render_paths) != slides:
            raise SlideForgeRewardTaskError("render_paths do not match the inspected slide count")
        claimed_sha256 = _text(inspection.get("sha256")).casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", claimed_sha256):
            raise SlideForgeRewardTaskError("inspection.sha256 is invalid")
        if not secrets.compare_digest(_sha256_file(artifact_path), claimed_sha256):
            raise SlideForgeRewardTaskError("inspection.sha256 does not match the artifact")
        return SlideForgeRewardRequest(
            task_id=task_id,
            artifact_path=artifact_path,
            render_paths=render_paths,
            inputs_path=inputs_path,
            inspection=inspection,
        )

    async def evaluate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = await asyncio.to_thread(self._parse_request, payload)
        bundle_dir = self.config.bundle_root / request.task_id
        rubric = _load_object(bundle_dir / "rubric.json", label="SlideForge rubric")
        rubric_sha256 = _sha256_file(bundle_dir / "rubric.json")
        artifact_sha256 = _text(request.inspection.get("sha256")).casefold()
        cache_key = hashlib.sha256(
            f"{request.task_id}\0{artifact_sha256}\0{rubric_sha256}".encode()
        ).hexdigest()
        work_dir = self.config.work_root / cache_key[:2] / cache_key
        cache_path = work_dir / "evaluation.json"
        lock = await self._lock_for(cache_key)
        async with lock, self.semaphore:
            cache_hit = cache_path.is_file()
            if cache_hit:
                try:
                    evaluation = _load_object(cache_path, label="cached SlideForge evaluation")
                    _number(evaluation.get("reward_score"), name="SlideForge reward_score")
                except SlideForgeRewardInfrastructureError:
                    cache_hit = False
            if not cache_hit:
                work_dir.mkdir(parents=True, exist_ok=True)
                evaluation = dict(
                    await asyncio.to_thread(
                        self.runner.evaluate,
                        task_id=request.task_id,
                        artifact_path=request.artifact_path,
                        render_paths=request.render_paths,
                        bundle_dir=bundle_dir,
                        work_dir=work_dir,
                    )
                )
                _write_object(cache_path, evaluation)
        result = map_slideforge_evaluation(
            evaluation,
            rubric,
            request.inspection,
            cache_hit=cache_hit,
        )
        result["metadata"]["artifact_sha256"] = artifact_sha256
        result["metadata"]["rubric_sha256"] = rubric_sha256
        return result


def create_slideforge_reward_app(
    service: SlideForgeRewardService | None = None,
    *,
    auth_token: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    runtime = os.environ if environ is None else environ
    if service is None:
        service = SlideForgeRewardService(SlideForgeRewardConfig.from_environ(runtime))
    token = _text(
        auth_token
        if auth_token is not None
        else runtime.get("ORS_SLIDEFORGE_REWARD_TOKEN")
    )
    if _env_bool(runtime, "ORS_SLIDEFORGE_REWARD_REQUIRE_AUTH", True) and not token:
        raise SlideForgeRewardInfrastructureError(
            "ORS_SLIDEFORGE_REWARD_TOKEN is required when authentication is enabled"
        )

    app = FastAPI(title="SlideForge OpenReward adapter", version="1")

    def check_auth(authorization: str | None) -> None:
        if token and not secrets.compare_digest(
            _text(authorization),
            f"Bearer {token}",
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "adapter": "slideforge-ppt-reward-v1",
            "max_concurrency": service.config.max_concurrency,
        }

    @app.post("/v1/evaluate")
    async def evaluate(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        try:
            payload = await request.json()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=400, detail="request body must be an object")
        try:
            result = await service.evaluate(payload)
        except SlideForgeRewardTaskError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SlideForgeRewardInfrastructureError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail="unexpected SlideForge Reward failure",
            ) from exc
        return JSONResponse(result)

    return app
