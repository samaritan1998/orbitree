"""Pydantic representations of the Open Reward Standard wire types.

Only JSON-compatible values are accepted at the HTTP boundary.  We keep the
value type intentionally open (rather than attempting to model recursive JSON
types in every adapter) because task specifications are environment-owned and
may contain nested asset manifests.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JSONObject = dict[str, Any]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(_StrictModel):
    text: str
    detail: JSONObject | None = None
    type: Literal["text"] = "text"


class ImageBlock(_StrictModel):
    data: str
    mimeType: str
    detail: JSONObject | None = None
    type: Literal["image"] = "image"


Block = TextBlock | ImageBlock


class ToolOutput(_StrictModel):
    """The result of one environment action.

    ``reward`` is optional for intermediate actions.  ``finished`` is the
    episode termination signal.  This pair is the important difference from a
    plain MCP tool result and is kept at the same nesting as the ORS spec.
    """

    blocks: list[Block] = Field(default_factory=list)
    metadata: JSONObject | None = None
    reward: float | None = None
    finished: bool = False


class ToolSpec(_StrictModel):
    name: str
    description: str = ""
    input_schema: JSONObject | None = None


class Split(_StrictModel):
    name: str
    type: Literal["train", "validation", "test"]


class CreateRequest(_StrictModel):
    env_name: str | None = None
    task_spec: JSONObject | None = None
    split: str | None = None
    index: int | None = None
    # ORS transports secrets in the create request.  The server passes them to
    # the environment instance but never stores or logs them.
    secrets: dict[str, Any] = Field(default_factory=dict)
    toolset_name: str | None = None

    @model_validator(mode="after")
    def validate_task_source(self) -> CreateRequest:
        has_spec = self.task_spec is not None
        has_index = self.split is not None and self.index is not None
        if has_spec == has_index:
            raise ValueError(
                "provide exactly one of task_spec or both split and index"
            )
        if (self.split is None) != (self.index is None):
            raise ValueError("split and index must be provided together")
        return self


class ListTasksRequest(_StrictModel):
    split: str


class NumTasksRequest(_StrictModel):
    split: str


class GetTaskRequest(_StrictModel):
    split: str
    index: int


class TaskRangeRequest(_StrictModel):
    split: str
    start: int | None = None
    stop: int | None = None


class ToolCallRequest(_StrictModel):
    name: str
    input: JSONObject = Field(default_factory=dict)
    task_id: str | None = None


class RunToolSuccess(_StrictModel):
    ok: Literal[True] = True
    output: ToolOutput


class RunToolError(_StrictModel):
    ok: Literal[False] = False
    error: str
    reason: Literal["not_found", "name_collision", "input_validation"] | None = None


class SessionResponse(_StrictModel):
    sid: str


class HealthResponse(_StrictModel):
    status: Literal["ok"] = "ok"
