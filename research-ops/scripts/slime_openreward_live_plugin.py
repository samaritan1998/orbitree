#!/usr/bin/env python3
"""Load the Slime OpenReward adapter without replacing the stable Slime tree.

The training job uses a known-good Slime checkout for the actor/optimizer and
loads only ``slime.openreward.client`` plus ``openreward_rollout.py`` from the
adapter checkout.  This keeps unrelated worktree changes out of the trainer.

The module also writes bounded, secret-free rollout receipts.  Those receipts
are evidence only: the optimizer consumes the freshly generated ``Sample``
objects directly in memory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sys
import tempfile
import threading
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_ADAPTER: types.ModuleType | None = None
_POLICY_ROUTE_LOCK = threading.Lock()
_PUBLISHED_POLICY_ROUTES: set[tuple[str, str]] = set()


def _annotate_miaoda_optimizer_batch(
    groups: list[list[Any]],
    *,
    rollout_id: int,
    fetch_index: int,
    run_identity: str,
) -> int:
    """Attach one explicit ORS batch envelope to this Slime candidate fetch."""

    managed: list[Any] = []
    for group in groups:
        for sample in group:
            metadata = getattr(sample, "metadata", None)
            if not isinstance(metadata, dict):
                raise RuntimeError("OpenReward sample metadata must be an object")
            domain = str(metadata.get("openreward_env") or "").strip().casefold()
            mode = str(metadata.get("openreward_mode") or "auto").strip().casefold()
            if (
                domain == "miaoda"
                and mode in {"auto", "managed"}
                and metadata.get("openreward_pure_opd") is True
            ):
                managed.append(sample)
    if not managed:
        return 0

    material = (
        f"{run_identity}\0{int(rollout_id)}\0{int(fetch_index)}\0{len(groups)}"
    ).encode()
    batch_id = (
        f"slime-r{int(rollout_id)}-f{int(fetch_index)}-"
        f"{hashlib.sha256(material).hexdigest()[:16]}"
    )
    expected_count = len(managed)
    for member_index, sample in enumerate(managed):
        metadata = sample.metadata
        existing = {
            key
            for key in (
                "openreward_optimizer_batch_id",
                "openreward_optimizer_batch_expected_count",
                "openreward_optimizer_batch_member_index",
            )
            if metadata.get(key) is not None
        }
        if existing:
            raise RuntimeError("Miaoda sample already has an optimizer batch envelope")
        metadata.update(
            {
                "openreward_optimizer_batch_id": batch_id,
                "openreward_optimizer_batch_expected_count": expected_count,
                "openreward_optimizer_batch_member_index": member_index,
                "openreward_rollout_id": int(rollout_id),
                "openreward_candidate_fetch_index": int(fetch_index),
            }
        )
    return expected_count


class _OptimizerBatchDataSource:
    def __init__(self, delegate: Any, *, rollout_id: int) -> None:
        self.delegate = delegate
        self.rollout_id = int(rollout_id)
        self.fetch_index = 0
        self.run_identity = str(
            os.environ.get("FULL_JOB_NAME")
            or os.environ.get("JOB_NAME")
            or "local-openreward"
        ).strip()

    def get_samples(self, count: int) -> list[list[Any]]:
        groups = self.delegate.get_samples(count)
        _annotate_miaoda_optimizer_batch(
            groups,
            rollout_id=self.rollout_id,
            fetch_index=self.fetch_index,
            run_identity=self.run_identity,
        )
        self.fetch_index += 1
        return groups

    def add_samples(self, samples: list[list[Any]]) -> Any:
        return self.delegate.add_samples(samples)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


def generate_rollout(
    args: Any,
    rollout_id: int,
    data_source: Any,
    evaluation: bool = False,
) -> Any:
    """Run the standard Slime rollout with trainer-owned Miaoda batch IDs."""

    from slime.rollout.sglang_rollout import generate_rollout as standard_generate_rollout

    source = (
        data_source
        if evaluation
        else _OptimizerBatchDataSource(data_source, rollout_id=rollout_id)
    )
    return standard_generate_rollout(
        args,
        rollout_id,
        source,
        evaluation=evaluation,
    )


def _required_env(name: str) -> str:
    value = str(os.environ.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"missing runtime reference: {name}")
    return value


def _load_file(name: str, path: Path, *, package: bool = False) -> types.ModuleType:
    kwargs: dict[str, Any] = {}
    if package:
        kwargs["submodule_search_locations"] = [str(path.parent)]
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_adapter() -> types.ModuleType:
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER

    package_root = Path(_required_env("SLIME_OPENREWARD_ADAPTER_ROOT")).resolve()
    client_path = package_root / "slime" / "openreward" / "client.py"
    adapter_path = package_root / "slime" / "rollout" / "openreward_rollout.py"
    for path in (client_path, adapter_path):
        if not path.is_file():
            raise RuntimeError(f"OpenReward adapter component is missing: {path.name}")

    # The stable training checkout does not yet carry slime.openreward.  Add
    # only that package to sys.modules; all other slime imports resolve to the
    # stable checkout that launched the trainer.
    if "slime.openreward" not in sys.modules:
        package = types.ModuleType("slime.openreward")
        package.__file__ = str(client_path.parent / "__init__.py")
        package.__package__ = "slime.openreward"
        package.__path__ = [str(client_path.parent)]
        sys.modules["slime.openreward"] = package
        import slime

        slime.openreward = package
    if "slime.openreward.client" not in sys.modules:
        client = _load_file("slime.openreward.client", client_path)
        sys.modules["slime.openreward"].client = client

    _ADAPTER = _load_file("_research_ops_slime_openreward_rollout", adapter_path)
    return _ADAPTER


async def generate(args: Any, sample: Any, sampling_params: dict[str, Any]) -> Any:
    """Delegate one live episode to the isolated OpenReward adapter."""

    from openreward_dynamic_tasks import (
        clear_materialized_task,
        materialize_task_reference,
    )

    loaded_reference = materialize_task_reference(sample)
    try:
        _publish_managed_policy_route(args, sample)
        return await _load_adapter().generate(args, sample, sampling_params)
    finally:
        if loaded_reference:
            # Verifier code can exceed hundreds of KiB for one SWE task. ORS
            # has already copied the task at create time, so do not carry that
            # payload through Slime's optimizer/checkpoint pipeline.
            clear_materialized_task(sample)


def _publish_managed_policy_route(args: Any, sample: Any) -> None:
    """Publish the trainer-owned SGLang origin for AgentOS managed rollout.

    Interactive domains already call Slime's rollout router directly. Miaoda
    runs through AgentOS, so the trusted ORS policy proxy needs the same router
    origin. The receipt contains no credential or prompt and is replaced
    atomically before AgentOS submits its first model request.
    """

    destination_text = str(
        os.environ.get("SLIME_OPENREWARD_MANAGED_POLICY_ROUTE_FILE", "")
    ).strip()
    if not destination_text:
        return
    metadata = getattr(sample, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    domain = str(
        metadata.get("openreward_env") or os.environ.get("SLIME_OPENREWARD_ENV", "")
    ).strip().casefold()
    mode = str(metadata.get("openreward_mode") or "auto").strip().casefold()
    if domain != "miaoda" or mode not in {"auto", "managed"}:
        return

    from slime.rollout.sglang_rollout import get_model_url

    model = str(
        metadata.get("openreward_policy_model")
        or getattr(args, "openreward_policy_model", "")
        or ""
    )
    raw_url = get_model_url(args, model, "")
    parts = urlsplit(str(raw_url).strip())
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or parts.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    ):
        raise RuntimeError("managed policy route is not externally reachable")
    upstream_url = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    destination = Path(destination_text)
    if not destination.is_absolute():
        raise RuntimeError("managed policy route file must be absolute")

    cache_key = (str(destination), upstream_url)
    with _POLICY_ROUTE_LOCK:
        if cache_key in _PUBLISHED_POLICY_ROUTES and destination.is_file():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "status": "ready",
            "protocol": "slime-sglang-policy-route.v1",
            "upstream_url": upstream_url,
            "published_at": datetime.now(UTC).isoformat(),
            "job_id": str(
                os.environ.get("FULL_JOB_NAME") or os.environ.get("JOB_NAME") or ""
            )[:80],
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _PUBLISHED_POLICY_ROUTES.add(cache_key)


def _status(sample: Any) -> str:
    value = getattr(sample, "status", None)
    return str(getattr(value, "value", value) or "pending").strip().casefold()


def _finite_count(values: Any) -> int:
    if values is None:
        return 0
    return sum(math.isfinite(float(value)) for value in values)


def _env_true(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "" if not default else "true")).strip().casefold()
    return raw in {"1", "true", "yes", "on"}


def _validation_reason(
    *,
    status: str,
    removed: bool,
    reward: Any,
    response_length: int,
    behavior_count: int,
    finite_behavior_count: int,
    teacher_count: int,
    finite_teacher_count: int,
    loss_mask_count: int,
    loss_mask_ones: int,
    versions: list[str],
    require_versions: bool,
    pure_opd: bool,
    metadata: Any,
) -> str | None:
    """Return a bounded reason code without serializing model output/errors."""

    if status != "completed" or removed:
        return "status_not_completed"
    if reward is None:
        return "missing_reward"
    if pure_opd:
        try:
            neutral = math.isfinite(float(reward)) and abs(float(reward)) <= 1e-12
        except (TypeError, ValueError, OverflowError):
            neutral = False
        if not neutral:
            return "pure_opd_non_neutral_reward"
        if (
            not isinstance(metadata, dict)
            or metadata.get("openreward_training_objective") != "pure_opd"
        ):
            return "pure_opd_metadata_missing"
        if metadata.get("openreward_task_reward_used") is not False:
            return "pure_opd_task_reward_used"
    if behavior_count != response_length or finite_behavior_count != response_length:
        return "behavior_logprobs_mismatch"
    if teacher_count != response_length or finite_teacher_count != response_length:
        return "teacher_logprobs_mismatch"
    if loss_mask_count != response_length or loss_mask_ones < 1:
        return "loss_mask_invalid"
    if require_versions and (not versions or any(value == "default" for value in versions)):
        return "weight_version_missing"
    return None


def filter_trainable_group(args: Any, samples: list[Any], **kwargs: Any) -> Any:
    """Drop failed online episodes before Slime's batch-size assertion.

    OpenReward environments can reject one malformed tool call while sibling
    episodes remain valid.  Dynamic sampling then asks the data source for a
    replacement, keeping the optimizer batch made only of complete MOPD
    samples.  The function imports Slime's filter type lazily so this module
    remains usable with the stable training checkout.
    """

    from slime.rollout.filter_hub.base_types import DynamicFilterOutput

    require_versions = str(
        os.environ.get("SLIME_OPENREWARD_REQUIRE_WEIGHT_VERSION", "true")
    ).strip().casefold() in {"1", "true", "yes", "on"}
    pure_opd = _env_true("SLIME_OPENREWARD_PURE_OPD")
    for sample in samples:
        response_length = int(getattr(sample, "response_length", 0) or 0)
        behavior = list(getattr(sample, "rollout_log_probs", None) or [])
        teacher = list(getattr(sample, "teacher_log_probs", None) or [])
        loss_mask = list(getattr(sample, "loss_mask", None) or [])
        try:
            finite_behavior = _finite_count(behavior)
        except (TypeError, ValueError, OverflowError):
            finite_behavior = -1
        try:
            finite_teacher = _finite_count(teacher)
        except (TypeError, ValueError, OverflowError):
            finite_teacher = -1
        reason = _validation_reason(
            status=_status(sample),
            removed=bool(getattr(sample, "remove_sample", False)),
            reward=getattr(sample, "reward", None),
            response_length=response_length,
            behavior_count=len(behavior),
            finite_behavior_count=finite_behavior,
            teacher_count=len(teacher),
            finite_teacher_count=finite_teacher,
            loss_mask_count=len(loss_mask),
            loss_mask_ones=sum(int(bool(value)) for value in loss_mask),
            versions=[str(value) for value in getattr(sample, "weight_versions", [])],
            require_versions=require_versions,
            pure_opd=pure_opd,
            metadata=getattr(sample, "metadata", None),
        )
        if reason is not None:
            return DynamicFilterOutput(keep=False, reason=reason)
    return DynamicFilterOutput(keep=True)


def _safe_identity(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    allowed = (
        "openreward_env",
        "openreward_task_id",
        "openreward_ors_session_id",
        "openreward_policy_weight_version",
        "openreward_turns",
        "openreward_finished",
        "openreward_finalized_on_limit",
        "openreward_teacher_source",
        "openreward_teacher_domain",
        "openreward_teacher_checkpoint",
        "openreward_teacher_logprob_count",
        "openreward_training_objective",
        "openreward_task_reward_used",
        "openreward_reward_source",
    )
    return {
        key: metadata[key]
        for key in allowed
        if key in metadata and isinstance(metadata[key], (str, int, float, bool))
    }


def log_rollout_data(
    rollout_id: int,
    args: Any,
    samples: list[Any],
    rollout_extra_metrics: dict[str, Any] | None,
    rollout_time: float,
) -> bool:
    """Persist a bounded receipt and enforce the online MOPD sample contract."""

    evidence_dir = Path(_required_env("SLIME_OPENREWARD_EVIDENCE_DIR"))
    require_versions = str(
        os.environ.get("SLIME_OPENREWARD_REQUIRE_WEIGHT_VERSION", "true")
    ).strip().casefold() in {"1", "true", "yes", "on"}
    pure_opd = _env_true("SLIME_OPENREWARD_PURE_OPD")
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for sample in samples:
        response_length = int(getattr(sample, "response_length", 0) or 0)
        behavior = list(getattr(sample, "rollout_log_probs", None) or [])
        teacher = list(getattr(sample, "teacher_log_probs", None) or [])
        loss_mask = list(getattr(sample, "loss_mask", None) or [])
        versions = [str(value) for value in getattr(sample, "weight_versions", [])]
        status = _status(sample)

        try:
            finite_behavior_count = _finite_count(behavior)
        except (TypeError, ValueError, OverflowError):
            finite_behavior_count = -1
        try:
            finite_teacher_count = _finite_count(teacher)
        except (TypeError, ValueError, OverflowError):
            finite_teacher_count = -1
        failure_reason = _validation_reason(
            status=status,
            removed=bool(getattr(sample, "remove_sample", False)),
            reward=getattr(sample, "reward", None),
            response_length=response_length,
            behavior_count=len(behavior),
            finite_behavior_count=finite_behavior_count,
            teacher_count=len(teacher),
            finite_teacher_count=finite_teacher_count,
            loss_mask_count=len(loss_mask),
            loss_mask_ones=sum(int(bool(value)) for value in loss_mask),
            versions=versions,
            require_versions=require_versions,
            pure_opd=pure_opd,
            metadata=getattr(sample, "metadata", None),
        )

        record = {
                "sample_index": getattr(sample, "index", None),
                "group_index": getattr(sample, "group_index", None),
                "status": status,
                "reward": getattr(sample, "reward", None),
                "token_count": len(getattr(sample, "tokens", []) or []),
                "response_length": response_length,
                "loss_mask_ones": sum(int(bool(value)) for value in loss_mask),
                "behavior_logprob_count": len(behavior),
                "finite_behavior_logprob_count": finite_behavior_count,
                "teacher_logprob_count": len(teacher),
                "finite_teacher_logprob_count": finite_teacher_count,
                "weight_versions": versions,
                "identity": _safe_identity(getattr(sample, "metadata", None)),
                "validation_error": failure_reason,
            }
        records.append(record)
        if failure_reason is not None:
            failures.append(
                {
                    "sample_index": record["sample_index"],
                    "status": status,
                    "reason": failure_reason,
                }
            )

    payload = {
        "version": 1,
        "source": "live_memory_rollout",
        "rollout_id": int(rollout_id),
        "rollout_seconds": float(rollout_time),
        "sample_count": len(records),
        "samples": records,
        "validation_failures": failures,
        "extra_metric_keys": sorted((rollout_extra_metrics or {}).keys()),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"rollout_{int(rollout_id)}.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=evidence_dir,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    if failures:
        raise RuntimeError(
            f"online OpenReward sample is not trainable: {failures[0]['reason']}"
        )
    return False
