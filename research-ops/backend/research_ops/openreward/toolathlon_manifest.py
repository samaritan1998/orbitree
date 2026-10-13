"""Build stable Toolathlon-Gym task specs without copying evaluator source."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_TASK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_toolathlon_task(
    *,
    task_name: str,
    query: str,
    task_config: Mapping[str, Any],
    repository: str,
    revision: str,
    preprocess_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    """Map one official task directory to a server-owned ORS task spec."""

    normalized_name = str(task_name).strip()
    normalized_query = str(query).strip()
    if not _TASK_NAME.fullmatch(normalized_name):
        raise ValueError("Toolathlon task name is invalid")
    if not normalized_query:
        raise ValueError("Toolathlon task query is empty")
    servers = task_config.get("needed_mcp_servers")
    if not isinstance(servers, Sequence) or isinstance(servers, (str, bytes)):
        raise ValueError("Toolathlon task has no MCP server list")
    normalized_servers = [str(item).strip() for item in servers if str(item).strip()]
    if not normalized_servers:
        raise ValueError("Toolathlon task MCP server list is empty")

    return {
        "task_id": normalized_name,
        "task_name": normalized_name,
        "query": normalized_query,
        "benchmark_family": "toolathlon_gym",
        "tool_servers": normalized_servers,
        "reward_spec": {
            "type": "binary",
            "terminal_action": "claim_done",
            "evaluator": "official_task_script",
        },
        "source": {
            "repository": str(repository),
            "revision": str(revision),
            "preprocess_sha256": str(preprocess_sha256),
            "evaluator_sha256": str(evaluator_sha256),
        },
    }
