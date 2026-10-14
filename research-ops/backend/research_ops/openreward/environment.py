"""Environment-side abstraction for the ORS server.

The official OpenReward package provides a decorator-heavy implementation.  A
small explicit interface is more useful inside Orbitree: domain adapters
can delegate to AgentOS, Harbor, SlideForge, or a test double without coupling
the control plane to any one SDK.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from .types import Block, JSONObject, Split, ToolOutput, ToolSpec

_SENSITIVE_TASK_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "jwt",
    "password",
    "replay_url",
    "secret",
    "signed_url",
    "token",
)


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def sensitive_task_key(key: Any) -> bool:
    """Return whether a task field name looks credential-like."""

    normalized = str(key).casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_TASK_KEY_PARTS)


def visible_task_value(value: Any) -> Any:
    """Recursively copy task metadata while dropping secret-like fields."""

    if isinstance(value, Mapping):
        return {
            str(key): visible_task_value(child)
            for key, child in value.items()
            if not sensitive_task_key(key)
        }
    if isinstance(value, list):
        return [visible_task_value(item) for item in value]
    if isinstance(value, tuple):
        return [visible_task_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ToolDefinition:
    """Static tool metadata exposed by ``/{env}/tools``."""

    name: str
    description: str
    input_schema: JSONObject | None = None

    def as_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )


def _json_type_matches(value: Any, expected: str) -> bool:
    """Return whether a Python value matches one JSON Schema primitive type."""

    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_tool_input(
    value: Any,
    schema: Mapping[str, Any] | None,
    *,
    path: str = "$",
) -> None:
    """Validate the JSON Schema subset used by ORS tool definitions.

    The validator intentionally reports only paths and schema expectations,
    never submitted values.  This keeps malformed inputs (which may contain
    credentials) out of SSE errors while still enforcing the advertised tool
    contract.  It supports the structural keywords used by the built-in and
    expected task-specific tools; unsupported annotation keywords are ignored.
    """

    if not schema:
        return
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not match const")
    enum = schema.get("enum")
    if (
        isinstance(enum, Sequence)
        and not isinstance(enum, (str, bytes))
        and value not in enum
    ):
        raise ValueError(f"{path} is not an allowed value")

    for keyword in ("allOf",):
        variants = schema.get(keyword)
        if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
            for variant in variants:
                if isinstance(variant, Mapping):
                    validate_tool_input(value, variant, path=path)

    for keyword, exact in (("anyOf", False), ("oneOf", True)):
        variants = schema.get(keyword)
        if isinstance(variants, Sequence) and not isinstance(variants, (str, bytes)):
            matches = 0
            for variant in variants:
                if not isinstance(variant, Mapping):
                    continue
                try:
                    validate_tool_input(value, variant, path=path)
                except ValueError:
                    continue
                matches += 1
            if matches == 0 or (exact and matches != 1):
                raise ValueError(f"{path} does not match {keyword}")

    declared = schema.get("type")
    expected_types: list[str] = []
    if isinstance(declared, str):
        expected_types = [declared]
    elif isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        expected_types = [str(item) for item in declared]
    if expected_types and not any(_json_type_matches(value, item) for item in expected_types):
        raise ValueError(f"{path} must be {' or '.join(expected_types)}")

    if isinstance(value, Mapping):
        required = schema.get("required")
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
            for key in required:
                if str(key) not in value:
                    raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        additional = schema.get("additionalProperties", True)
        if additional is False:
            unexpected = sorted(str(key) for key in value if key not in properties)
            if unexpected:
                raise ValueError(f"{path} has unexpected properties")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, Mapping):
                child_schema = additional
            if isinstance(child_schema, Mapping):
                validate_tool_input(child, child_schema, path=f"{path}.{key}")
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} has too few properties")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} has too many properties")

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(value):
                validate_tool_input(child, items, path=f"{path}[{index}]")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} has too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} has too many items")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} is too short")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} is too long")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} is below minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} is above maximum")


class ORSEnvironment(ABC):
    """A stateful, task-scoped environment implementing the ORS contract."""

    env_name: ClassVar[str]
    _splits: ClassVar[tuple[Split, ...]] = (
        Split(name="train", type="train"),
        Split(name="validation", type="validation"),
        Split(name="test", type="test"),
    )
    _tools: ClassVar[tuple[ToolDefinition, ...]] = ()

    def __init__(
        self,
        task_spec: Mapping[str, Any],
        secrets: Mapping[str, Any] | None = None,
    ) -> None:
        self.task_spec: JSONObject = dict(task_spec)
        # A secret is request-scoped.  Adapters may consume it in setup, but
        # the server never serializes this mapping or puts it in task metadata.
        self.secrets: dict[str, Any] = dict(secrets or {})
        self.state: dict[str, Any] = {"turns": 0}

    @classmethod
    def name(cls) -> str:
        return cls.env_name

    @classmethod
    def list_splits(cls) -> Sequence[Split]:
        return cls._splits

    @classmethod
    async def list_tasks(cls, split: str) -> Sequence[JSONObject]:
        valid = {
            item.name if isinstance(item, Split) else str(item)
            for item in cls.list_splits()
        }
        if split not in valid:
            raise ValueError(f"unknown split {split!r}")
        return ()

    @classmethod
    async def num_tasks(cls, split: str) -> int:
        return len(await maybe_await(cls.list_tasks(split)))

    @classmethod
    async def get_task(cls, split: str, index: int) -> JSONObject:
        tasks = list(await maybe_await(cls.list_tasks(split)))
        try:
            return dict(tasks[index])
        except IndexError as exc:
            raise ValueError(f"task index out of range: {split}[{index}]") from exc

    @classmethod
    async def get_task_range(
        cls,
        split: str,
        start: int | None = None,
        stop: int | None = None,
    ) -> list[JSONObject]:
        tasks = list(await maybe_await(cls.list_tasks(split)))
        return [dict(item) for item in tasks[slice(start, stop)]]

    @classmethod
    def list_tools(cls) -> Sequence[ToolSpec]:
        return tuple(tool.as_spec() for tool in cls._tools)

    async def list_task_tools(self) -> Sequence[ToolSpec]:
        return ()

    async def setup(self) -> None:
        """Allocate per-session resources (sandbox, AgentOS app, etc.)."""
        return None

    async def teardown(self) -> None:
        """Release per-session resources."""
        return None

    @abstractmethod
    async def get_prompt(self) -> Sequence[Block]:
        """Return the initial prompt/asset blocks for this task."""

    @abstractmethod
    async def call_tool(
        self,
        name: str,
        input: JSONObject,
        *,
        task_id: str | None = None,
    ) -> ToolOutput:
        """Execute one named action and return an ORS tool output."""

    async def all_tools(self) -> list[ToolSpec]:
        """Merge shared and task-specific tools, preserving first occurrence."""

        merged: list[ToolSpec] = list(self.list_tools())
        seen = {item.name for item in merged}
        for item in await maybe_await(self.list_task_tools()):
            if item.name not in seen:
                merged.append(item)
                seen.add(item.name)
        return merged


EnvironmentFactory = Callable[[JSONObject, Mapping[str, Any]], ORSEnvironment]
