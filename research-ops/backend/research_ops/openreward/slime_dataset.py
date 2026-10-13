"""Policy-safe Slime dataset rows backed by an ORS server-side task catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _normalized_environment(value: str) -> str:
    name = str(value).strip().casefold()
    return {"pptx": "ppt", "tool-use": "tooluse", "tool_use": "tooluse"}.get(
        name, name
    )


def _task_id(task: Mapping[str, Any]) -> str:
    for key in ("task_id", "episode_id", "case_id", "id"):
        value = task.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("OpenReward catalog task has no stable task ID")


def build_slime_selector_rows(
    task_catalogs: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    *,
    environment: str,
    split: str,
    mode: str,
) -> list[dict[str, Any]]:
    """Map catalog tasks to public Slime rows without copying runtime/verifier data."""

    env_name = _normalized_environment(environment)
    split_name = str(split).strip()
    rollout_mode = str(mode).strip().casefold()
    if not env_name or env_name not in task_catalogs:
        raise ValueError(f"OpenReward catalog has no environment {env_name!r}")
    if not split_name or split_name not in task_catalogs[env_name]:
        raise ValueError(
            f"OpenReward catalog environment {env_name!r} has no split {split_name!r}"
        )
    if rollout_mode not in {"auto", "managed", "interactive"}:
        raise ValueError("OpenReward mode must be auto, managed, or interactive")

    rows: list[dict[str, Any]] = []
    for index, task in enumerate(task_catalogs[env_name][split_name]):
        query = task.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"OpenReward task at {split_name}[{index}] has no public query")
        rows.append(
            {
                "text": query,
                "metadata": {
                    "openreward_env": env_name,
                    "openreward_mode": rollout_mode,
                    "openreward_split": split_name,
                    "openreward_task_index": index,
                    "openreward_task_id": _task_id(task),
                },
            }
        )
    return rows
