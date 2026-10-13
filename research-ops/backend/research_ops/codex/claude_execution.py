from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_ops.settings import Settings

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# Stable CLI aliases, not an account entitlement or remote model-discovery result.
CLAUDE_MODELS = [
    {"id": "sonnet", "name": "Sonnet"},
    {"id": "opus", "name": "Opus"},
    {"id": "haiku", "name": "Haiku"},
]


class ClaudeExecutionPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$",
    )
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    permission_mode: Literal[
        "default", "acceptEdits", "bypassPermissions", "dontAsk", "plan"
    ] | None = None


def claude_execution_snapshot(
    settings: Settings, preferences: dict[str, Any] | None = None
) -> dict[str, Any]:
    values = preferences or {}
    selected = ClaudeExecutionPreferences.model_validate(
        {
            "model": values.get("model") or settings.claude_model,
            "reasoning_effort": values.get("reasoning_effort") or settings.claude_effort,
            "permission_mode": values.get("permission_mode") or settings.claude_permission_mode,
        }
    )
    return {
        "runtime": "claude",
        "identity": "claude",
        "label": settings.claude_account_label,
        "provider": "claude-cli",
        **selected.model_dump(),
    }
