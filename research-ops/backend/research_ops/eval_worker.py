from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class EvalWorkerConfig:
    run_id: str
    evaluator_url: str
    control_url: str = "http://127.0.0.1:8020"
    worker_id: str = ""
    slots: int = 1
    claim_size: int = 8
    lease_seconds: int = 900
    poll_seconds: float = 2.0
    evaluator_timeout_seconds: float = 600.0
    control_token_env: str = "ROPS_ACCESS_TOKEN"
    evaluator_token_env: str | None = None
    once: bool = False


@dataclass
class EvalWorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    empty_claims: int = 0

    def add(self, other: EvalWorkerStats) -> None:
        self.claimed += other.claimed
        self.succeeded += other.succeeded
        self.failed += other.failed
        self.empty_claims += other.empty_claims


async def run_eval_worker(config: EvalWorkerConfig) -> EvalWorkerStats:
    _validate_config(config)
    worker_id = config.worker_id.strip() or f"{socket.gethostname()}:{os.getpid()}"
    control_headers = _bearer_headers(config.control_token_env)
    evaluator_headers = _bearer_headers(config.evaluator_token_env)
    control_timeout = httpx.Timeout(30.0)
    evaluator_timeout = httpx.Timeout(config.evaluator_timeout_seconds)
    async with (
        httpx.AsyncClient(
            base_url=config.control_url.rstrip("/"),
            headers=control_headers,
            timeout=control_timeout,
        ) as control,
        httpx.AsyncClient(
            headers=evaluator_headers,
            timeout=evaluator_timeout,
        ) as evaluator,
    ):
        results = await asyncio.gather(
            *[
                _slot_loop(
                    config,
                    worker_id=f"{worker_id}:{slot}",
                    control=control,
                    evaluator=evaluator,
                )
                for slot in range(config.slots)
            ]
        )
    combined = EvalWorkerStats()
    for result in results:
        combined.add(result)
    return combined


async def _slot_loop(
    config: EvalWorkerConfig,
    *,
    worker_id: str,
    control: httpx.AsyncClient,
    evaluator: httpx.AsyncClient,
) -> EvalWorkerStats:
    stats = EvalWorkerStats()
    while True:
        response = await control.post(
            f"/api/eval-runs/{config.run_id}/claim",
            json={
                "worker_id": worker_id,
                "limit": config.claim_size,
                "lease_seconds": config.lease_seconds,
            },
        )
        if response.status_code == 409:
            stats.empty_claims += 1
            if config.once:
                return stats
            await asyncio.sleep(config.poll_seconds)
            continue
        response.raise_for_status()
        claim = _object(response.json(), "claim response")
        units = claim.get("units")
        if not isinstance(units, list):
            raise RuntimeError("Eval claim response has no units list")
        if not units:
            stats.empty_claims += 1
            if config.once:
                return stats
            await asyncio.sleep(config.poll_seconds)
            continue
        stats.claimed += len(units)
        succeeded, failed = await _evaluate_claim(
            config,
            claim=claim,
            units=[_object(unit, "claimed unit") for unit in units],
            control=control,
            evaluator=evaluator,
        )
        stats.succeeded += succeeded
        stats.failed += failed
        if config.once:
            return stats


async def _evaluate_claim(
    config: EvalWorkerConfig,
    *,
    claim: dict[str, Any],
    units: list[dict[str, Any]],
    control: httpx.AsyncClient,
    evaluator: httpx.AsyncClient,
) -> tuple[int, int]:
    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            control,
            units,
            lease_seconds=config.lease_seconds,
        )
    )
    try:
        response = await evaluator.post(
            config.evaluator_url,
            json={"run": claim.get("run"), "units": units},
        )
        response.raise_for_status()
        payload = _object(response.json(), "evaluator response")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError("Evaluator response has no results list")
        results: dict[str, dict[str, Any]] = {}
        for raw_result in raw_results:
            result = _object(raw_result, "evaluator result")
            unit_id = str(result.get("unit_id") or "")
            if not unit_id or unit_id in results:
                raise RuntimeError("Evaluator result unit_id is missing or duplicated")
            results[unit_id] = result
    except Exception as exc:
        await _fail_many(
            control,
            units,
            category="evaluator_request",
            message=str(exc),
            retryable=True,
        )
        return 0, len(units)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat

    succeeded = 0
    failed = 0
    for unit in units:
        unit_id = str(unit["id"])
        result = results.get(unit_id)
        if result is None:
            await _fail_one(
                control,
                unit,
                category="missing_result",
                message="Evaluator omitted the claimed unit",
                retryable=True,
            )
            failed += 1
            continue
        error = result.get("error")
        if error is not None:
            error_payload = _object(error, "evaluator error")
            await _fail_one(
                control,
                unit,
                category=str(error_payload.get("category") or "evaluator_error"),
                message=str(error_payload.get("message") or ""),
                retryable=bool(error_payload.get("retryable", True)),
                duration_ms=_optional_int(result.get("duration_ms")),
            )
            failed += 1
            continue
        score = result.get("score")
        if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 1:
            await _fail_one(
                control,
                unit,
                category="invalid_score",
                message="Evaluator score must be numeric and within [0, 1]",
                retryable=False,
                duration_ms=_optional_int(result.get("duration_ms")),
            )
            failed += 1
            continue
        result_json = result.get("result_json", {})
        if not isinstance(result_json, dict):
            await _fail_one(
                control,
                unit,
                category="invalid_result",
                message="Evaluator result_json must be an object",
                retryable=False,
            )
            failed += 1
            continue
        completed = await control.post(
            f"/api/eval-units/{unit_id}/complete",
            json={
                "lease_token": unit["lease_token"],
                "score": float(score),
                "result_json": result_json,
                "duration_ms": _optional_int(result.get("duration_ms")),
                "cache_hit": bool(result.get("cache_hit", False)),
            },
        )
        completed.raise_for_status()
        succeeded += 1
    return succeeded, failed


async def _heartbeat_loop(
    control: httpx.AsyncClient,
    units: list[dict[str, Any]],
    *,
    lease_seconds: int,
) -> None:
    interval = max(10.0, min(30.0, lease_seconds / 3))
    while True:
        await asyncio.sleep(interval)
        await asyncio.gather(
            *[
                control.post(
                    f"/api/eval-units/{unit['id']}/heartbeat",
                    json={
                        "lease_token": unit["lease_token"],
                        "lease_seconds": lease_seconds,
                    },
                )
                for unit in units
            ]
        )


async def _fail_many(
    control: httpx.AsyncClient,
    units: list[dict[str, Any]],
    *,
    category: str,
    message: str,
    retryable: bool,
) -> None:
    await asyncio.gather(
        *[
            _fail_one(
                control,
                unit,
                category=category,
                message=message,
                retryable=retryable,
            )
            for unit in units
        ]
    )


async def _fail_one(
    control: httpx.AsyncClient,
    unit: dict[str, Any],
    *,
    category: str,
    message: str,
    retryable: bool,
    duration_ms: int | None = None,
) -> None:
    response = await control.post(
        f"/api/eval-units/{unit['id']}/fail",
        json={
            "lease_token": unit["lease_token"],
            "category": category[:80],
            "message": message[:8000],
            "retryable": retryable,
            "duration_ms": duration_ms,
        },
    )
    response.raise_for_status()


def _bearer_headers(environment_name: str | None) -> dict[str, str]:
    if not environment_name:
        return {}
    token = os.environ.get(environment_name, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value)


def _validate_config(config: EvalWorkerConfig) -> None:
    if not config.run_id.strip():
        raise ValueError("run_id is required")
    if not config.evaluator_url.startswith(("http://", "https://")):
        raise ValueError("evaluator_url must be HTTP(S)")
    if not config.control_url.startswith(("http://", "https://")):
        raise ValueError("control_url must be HTTP(S)")
    if not 1 <= config.slots <= 128:
        raise ValueError("slots must be within [1, 128]")
    if not 1 <= config.claim_size <= 128:
        raise ValueError("claim_size must be within [1, 128]")
    if not 30 <= config.lease_seconds <= 7200:
        raise ValueError("lease_seconds must be within [30, 7200]")
