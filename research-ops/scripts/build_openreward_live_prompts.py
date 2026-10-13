#!/usr/bin/env python3
"""Materialize deterministic mixed-domain Slime prompts for online ORS rollout."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

DOMAIN_ORDER = ("miaoda", "swe", "ppt", "tooluse")
DOMAIN_MODES = {
    "miaoda": "managed",
    "swe": "interactive",
    "ppt": "interactive",
    "tooluse": "interactive",
}


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
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
            } or normalized.endswith(
                ("_api_key", "_password", "_secret", "_token")
            ):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if _contains_secret_key(value):
        raise ValueError(f"{label} contains a credential-like key")
    return value


def _task_id(task: Mapping[str, Any], fallback: str) -> str:
    for key in ("task_id", "episode_id", "case_id", "id"):
        value = str(task.get(key) or "").strip()
        if value:
            return value
    return fallback


def _parse_domain_weights(
    values: Sequence[str],
    available: Iterable[str],
    default_weights: Mapping[str, int] | None = None,
) -> dict[str, int]:
    available_set = set(available)
    if not values:
        defaults = default_weights or {}
        return {
            domain: int(defaults.get(domain, 1))
            for domain in DOMAIN_ORDER
            if domain in available_set
        }

    weights: dict[str, int] = {}
    for raw in values:
        domain, separator, weight_text = raw.partition("=")
        domain = domain.strip().casefold()
        if separator != "=" or domain not in DOMAIN_MODES:
            raise ValueError("--domain-weight must use DOMAIN=POSITIVE_INTEGER")
        if domain not in available_set:
            raise ValueError(f"task pool has no selected domain: {domain}")
        if domain in weights:
            raise ValueError(f"duplicate domain weight: {domain}")
        try:
            weight = int(weight_text)
        except ValueError as exc:
            raise ValueError("domain weights must be positive integers") from exc
        if weight < 1:
            raise ValueError("domain weights must be positive integers")
        weights[domain] = weight
    return weights


def _allocate_quotas(total: int, weights: Mapping[str, int]) -> dict[str, int]:
    if total < 1:
        raise ValueError("sample count must be positive")
    if not weights:
        raise ValueError("at least one domain must be selected")
    weight_sum = sum(weights.values())
    quotas = {
        domain: total * weight // weight_sum for domain, weight in weights.items()
    }
    remainder = total - sum(quotas.values())
    order = {domain: index for index, domain in enumerate(DOMAIN_ORDER)}
    ranked = sorted(
        weights,
        key=lambda domain: (
            -(total * weights[domain] % weight_sum),
            order[domain],
        ),
    )
    for domain in ranked[:remainder]:
        quotas[domain] += 1
    return quotas


def _interleaved_domains(quotas: Mapping[str, int]) -> list[str]:
    """Spread every domain across the file instead of writing domain blocks."""

    emitted = {domain: 0 for domain in quotas}
    order = {domain: index for index, domain in enumerate(DOMAIN_ORDER)}
    result: list[str] = []
    for _ in range(sum(quotas.values())):
        candidates = [
            domain for domain, quota in quotas.items() if emitted[domain] < quota
        ]
        domain = min(
            candidates,
            key=lambda item: (
                (emitted[item] + 1) / quotas[item],
                order[item],
            ),
        )
        emitted[domain] += 1
        result.append(domain)
    return result


def _pool_tasks(pool: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    environments = pool.get("environments")
    if not isinstance(environments, Mapping):
        raise ValueError("task pool must contain an environments object")
    result: dict[str, list[dict[str, Any]]] = {}
    for domain in DOMAIN_ORDER:
        entry = environments.get(domain)
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            raise ValueError(f"task pool domain {domain} must be an object")
        train = entry.get("train")
        if not isinstance(train, list) or not train:
            raise ValueError(f"task pool domain {domain} must have a non-empty train list")
        if any(not isinstance(task, dict) for task in train):
            raise ValueError(f"task pool domain {domain} contains a non-object task")
        result[domain] = train
    if not result:
        raise ValueError("task pool has no supported training domains")
    return result


def _pool_source_counts(
    pool: Mapping[str, Any], tasks: Mapping[str, list[dict[str, Any]]]
) -> dict[str, int]:
    """Read authoritative source sizes, falling back only for test pools."""

    provenance = pool.get("source_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    counts: dict[str, int] = {}
    for domain, rows in tasks.items():
        entry = provenance.get(domain)
        raw_count = entry.get("available_train_rows") if isinstance(entry, Mapping) else None
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = len(rows)
        if count < 1:
            raise ValueError(f"source_provenance.{domain}.available_train_rows must be positive")
        counts[domain] = count
    return counts


def _row(
    *,
    domain: str,
    task: Mapping[str, Any],
    task_index: int,
    slot: int,
    domain_copy: int,
    pure_opd: bool,
) -> dict[str, Any]:
    task_spec = copy.deepcopy(dict(task))
    if pure_opd:
        task_spec["training_objective"] = "pure_opd"
        task_spec["task_reward_required"] = False
    source_id = _task_id(task_spec, f"{domain}-{task_index}")
    online_task_id = f"{source_id}:online:{domain_copy}"
    # Miaoda may repeat one source query many times in a logical rollout
    # batch. AgentOS/Datawheel needs a distinct correlation ID for every
    # stochastic rollout; retain the source ID separately instead of letting
    # all copies write to the same policy-capture stream.
    if domain == "miaoda":
        task_spec["source_episode_id"] = source_id
        task_spec["episode_id"] = online_task_id
    metadata: dict[str, Any] = {
        "openreward_env": domain,
        "openreward_mode": DOMAIN_MODES[domain],
        "openreward_task_id": online_task_id,
        "openreward_task": task_spec,
        "openreward_source_task_index": task_index,
        "openreward_batch_slot": slot,
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


def build_rows(
    tasks: Mapping[str, list[dict[str, Any]]],
    *,
    total_samples: int,
    weights: Mapping[str, int],
    pure_opd: bool,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    quotas = _allocate_quotas(total_samples, weights)
    emitted = {domain: 0 for domain in quotas}
    rows: list[dict[str, Any]] = []
    for slot, domain in enumerate(_interleaved_domains(quotas)):
        domain_copy = emitted[domain]
        task_index = domain_copy % len(tasks[domain])
        rows.append(
            _row(
                domain=domain,
                task=tasks[domain][task_index],
                task_index=task_index,
                slot=slot,
                domain_copy=domain_copy,
                pure_opd=pure_opd,
            )
        )
        emitted[domain] += 1
    return rows, quotas


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        for row in rows:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            handle.write(line)
            digest.update(line.encode("utf-8"))
    temporary.replace(path)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task-spec", type=Path)
    source.add_argument("--task-pool", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--copies", type=int)
    parser.add_argument("--total-samples", type=int)
    parser.add_argument("--domain-weight", action="append", default=[])
    parser.add_argument(
        "--training-objective",
        choices=("pure_opd", "reward"),
        default="pure_opd",
    )
    args = parser.parse_args(argv)

    if args.copies is not None and args.total_samples is not None:
        raise SystemExit("use only one of --copies and --total-samples")
    total_samples = args.total_samples or args.copies
    pure_opd = args.training_objective == "pure_opd"

    try:
        if args.task_spec is not None:
            if args.domain_weight:
                raise ValueError("--domain-weight requires --task-pool")
            task = _load_object(args.task_spec, label="task spec")
            tasks = {"tooluse": [task]}
            total_samples = total_samples or 2
            weights = {"tooluse": 1}
        else:
            assert args.task_pool is not None
            pool = _load_object(args.task_pool, label="task pool")
            tasks = _pool_tasks(pool)
            if total_samples is None:
                raise ValueError("--task-pool requires --total-samples")
            weights = _parse_domain_weights(
                args.domain_weight,
                tasks,
                default_weights=_pool_source_counts(pool, tasks),
            )
        rows, quotas = build_rows(
            tasks,
            total_samples=total_samples,
            weights=weights,
            pure_opd=pure_opd,
        )
        digest = _write_jsonl(args.output, rows)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "output": str(args.output),
                "rows": len(rows),
                "domains": quotas,
                "sha256": digest,
                "training_objective": args.training_objective,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
