"""Four domain adapters sharing one ORS environment contract.

These adapters deliberately stop at a narrow backend interface.  The backend
is where an installation plugs in AgentOS, Harbor/SWE-ReX, SlideForge or a
tool registry.  Keeping that dependency behind ``execute`` lets us test the
whole ORS lifecycle without requiring internal images or credentials, and it
prevents a mock rollout from being mistaken for a real online MOPD sample.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .environment import (
    ORSEnvironment,
    ToolDefinition,
    maybe_await,
    sensitive_task_key,
    validate_tool_input,
    visible_task_value,
)
from .types import Block, ImageBlock, JSONObject, Split, TextBlock, ToolOutput, ToolSpec


class BackendResult(BaseModel):
    """Backend-neutral result converted directly to an ORS ``ToolOutput``."""

    model_config = ConfigDict(extra="forbid")

    blocks: list[Block] = Field(default_factory=list)
    reward: float | None = None
    finished: bool = False
    metadata: JSONObject | None = None


class EnvironmentBackend(Protocol):
    """Minimal async/sync-compatible execution backend contract."""

    def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> Any:
        ...

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult | Any:
        ...

    def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> Any:
        ...

    # Optional: a stateful ToolUse registry can publish task-specific tools
    # after ``setup``.  DelegatingEnvironment probes this method dynamically,
    # so existing backends do not need to implement it.
    def list_tools(
        self,
        *,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> Sequence[ToolSpec] | Any:
        ...


@dataclass
class MockBackend:
    """Safe deterministic backend used for protocol smoke tests.

    It never executes shell commands or contacts a service.  A ``submit``
    action terminates the episode and receives reward 1.0 when the task has an
    ``expected_output`` that matches the submitted ``result``/``answer``;
    otherwise it receives 0.0.  Other actions are non-terminal observations.
    """

    label: str = "mock"

    def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        del task_spec, secrets
        state["backend"] = self.label

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult:
        del state
        if action in {"submit", "submit_deliverable", "finish"}:
            expected = task_spec.get("expected_output")
            submitted = payload.get(
                "result",
                payload.get("answer", payload.get("content", payload.get("path"))),
            )
            reward = 1.0 if expected is not None and submitted == expected else 0.0
            return BackendResult(
                blocks=[TextBlock(text="mock submission accepted")],
                reward=reward,
                finished=True,
                metadata={"backend": self.label, "action": action},
            )
        return BackendResult(
            blocks=[TextBlock(text=f"mock observation for {action}")],
            reward=None,
            finished=False,
            metadata={"backend": self.label, "action": action},
        )

    def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec, state


def _text(value: Any) -> str:
    return str(value or "").strip()


def _schema(properties: Mapping[str, Any], required: Sequence[str] = ()) -> JSONObject:
    result: JSONObject = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        result["required"] = list(required)
    return result


class DelegatingEnvironment(ORSEnvironment):
    """Shared implementation for domain adapters.

    A backend can return a :class:`BackendResult`, a mapping with equivalent
    fields, or a plain string.  The adapter normalizes all three forms and
    annotates only non-sensitive turn metadata.
    """

    _task_catalog: ClassVar[dict[str, tuple[JSONObject, ...]]] = {}

    def __init__(
        self,
        task_spec: Mapping[str, Any],
        secrets: Mapping[str, Any] | None = None,
        *,
        backend: EnvironmentBackend | None = None,
    ) -> None:
        super().__init__(task_spec, secrets)
        if backend is None:
            # Keep direct ``MiaodaEnvironment(...)``/domain construction
            # consistent with the server registry.  The import is lazy because
            # the domain mocks return ``BackendResult`` from this module.
            from .mock_backends import build_mock_backend

            backend = build_mock_backend(self.name())
        self.backend = backend

    @classmethod
    async def list_tasks(cls, split: str) -> Sequence[JSONObject]:
        valid = {
            item.name if isinstance(item, Split) else str(item)
            for item in cls.list_splits()
        }
        if split not in valid:
            raise ValueError(f"unknown split {split!r}")
        return tuple(dict(item) for item in cls._task_catalog.get(split, ()))

    async def setup(self) -> None:
        setup = getattr(self.backend, "setup", None)
        if setup is not None:
            await maybe_await(
                setup(task_spec=self.task_spec, secrets=self.secrets, state=self.state)
            )

    async def teardown(self) -> None:
        teardown = getattr(self.backend, "teardown", None)
        if teardown is not None:
            await maybe_await(teardown(task_spec=self.task_spec, state=self.state))
        # Do not retain request-scoped secrets after the session is torn down.
        self.secrets.clear()

    async def list_task_tools(self) -> Sequence[ToolSpec]:
        list_tools = getattr(self.backend, "list_tools", None)
        if list_tools is None:
            return ()
        raw_tools = await maybe_await(
            list_tools(task_spec=self.task_spec, state=self.state)
        )
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
            raise TypeError("backend list_tools must return a sequence")
        tools: list[ToolSpec] = []
        for raw in raw_tools:
            if isinstance(raw, ToolSpec):
                tools.append(raw)
            elif isinstance(raw, Mapping):
                tools.append(ToolSpec.model_validate(raw))
            else:
                raise TypeError("backend list_tools returned an unsupported item")
        return tools

    async def all_tools(self) -> list[ToolSpec]:
        """Allow a proxied ORS environment to replace local placeholder tools."""

        if bool(getattr(self.backend, "replace_environment_tools", False)):
            return list(await self.list_task_tools())
        return await super().all_tools()

    async def get_prompt(self) -> Sequence[Block]:
        get_prompt = getattr(self.backend, "get_prompt", None)
        if get_prompt is not None:
            raw_blocks = await maybe_await(
                get_prompt(task_spec=self.task_spec, state=self.state)
            )
            if not isinstance(raw_blocks, Sequence) or isinstance(raw_blocks, (str, bytes)):
                raise TypeError("backend get_prompt must return a sequence")
            blocks: list[Block] = []
            for raw in raw_blocks:
                if isinstance(raw, (TextBlock, ImageBlock)):
                    blocks.append(raw)
                elif isinstance(raw, Mapping):
                    if raw.get("type") == "image":
                        blocks.append(ImageBlock.model_validate(raw))
                    else:
                        blocks.append(TextBlock.model_validate(raw))
                else:
                    raise TypeError("backend get_prompt returned an unsupported block")
            return blocks
        prompt = _text(
            self.task_spec.get("prompt")
            or self.task_spec.get("query")
            or self.task_spec.get("instruction")
            or self.task_spec.get("description")
        )
        if not prompt:
            visible = {
                key: visible_task_value(value)
                for key, value in self.task_spec.items()
                if key not in {"expected_output", "ground_truth"}
                and not sensitive_task_key(key)
            }
            prompt = json.dumps(visible, ensure_ascii=False, sort_keys=True)
        blocks: list[Block] = [TextBlock(text=prompt)]
        raw_images = self.task_spec.get("images") or self.task_spec.get("image_blocks") or []
        if isinstance(raw_images, Sequence) and not isinstance(raw_images, (str, bytes)):
            for raw in raw_images:
                if not isinstance(raw, Mapping):
                    continue
                data = _text(raw.get("data") or raw.get("base64"))
                mime = _text(raw.get("mimeType") or raw.get("mime_type")) or "image/png"
                if data:
                    blocks.append(
                        ImageBlock(
                            data=data,
                            mimeType=mime,
                            detail=dict(raw.get("detail") or {}) or None,
                        )
                    )
        return blocks

    async def call_tool(
        self,
        name: str,
        input: JSONObject,
        *,
        task_id: str | None = None,
    ) -> ToolOutput:
        del task_id
        known = {tool.name: tool for tool in await self.all_tools()}
        tool = known.get(name)
        if tool is None:
            raise KeyError(name)
        validate_tool_input(input, tool.input_schema)
        self.state["turns"] = int(self.state.get("turns", 0)) + 1
        result = await maybe_await(
            self.backend.execute(
                action=name,
                payload=dict(input),
                task_spec=self.task_spec,
                state=self.state,
            )
        )
        normalized = self._normalize_result(result, name)
        metadata = dict(normalized.metadata or {})
        metadata.setdefault("environment", self.name())
        metadata.setdefault("turn", self.state["turns"])
        return ToolOutput(
            blocks=normalized.blocks or [TextBlock(text="")],
            reward=normalized.reward,
            finished=normalized.finished,
            metadata=metadata,
        )

    @staticmethod
    def _normalize_result(result: Any, action: str) -> BackendResult:
        if isinstance(result, BackendResult):
            return result
        if isinstance(result, Mapping):
            raw_blocks = result.get("blocks")
            blocks: list[Block] = []
            if isinstance(raw_blocks, Sequence) and not isinstance(raw_blocks, (str, bytes)):
                for raw in raw_blocks:
                    if isinstance(raw, (TextBlock, ImageBlock)):
                        blocks.append(raw)
                    elif isinstance(raw, Mapping):
                        if raw.get("type") == "image":
                            blocks.append(ImageBlock.model_validate(raw))
                        else:
                            blocks.append(TextBlock.model_validate(raw))
            if not blocks and result.get("text") is not None:
                blocks.append(TextBlock(text=_text(result.get("text"))))
            return BackendResult(
                blocks=blocks,
                reward=result.get("reward"),
                finished=bool(result.get("finished", False)),
                metadata=dict(result.get("metadata") or {}) or {"action": action},
            )
        if isinstance(result, str):
            return BackendResult(blocks=[TextBlock(text=result)])
        raise TypeError(f"backend for {action!r} returned unsupported {type(result).__name__}")


class MiaodaEnvironment(DelegatingEnvironment):
    """Miaoda/AgentOS task adapter.

    ``start_rollout``/``poll_rollout`` are the canonical complete-rollout
    actions for Datawheel + Goblin.  ``miaoda_step`` remains a direct
    continuation/debug action.  A backend that
    only exposes Datawheel's whole-rollout API can implement this action as a
    terminal one-shot and set ``finished=True``; that is useful for migration
    smoke tests but is explicitly not equivalent to per-turn online MOPD.
    """

    env_name = "miaoda"
    _tools = (
        ToolDefinition(
            name="start_rollout",
            description="Submit one complete Miaoda application rollout to Datawheel.",
            input_schema=_schema({}),
        ),
        ToolDefinition(
            name="poll_rollout",
            description="Poll AgentOS and Goblin until this Miaoda episode has a reward.",
            input_schema=_schema({}),
        ),
        ToolDefinition(
            name="miaoda_step",
            description="Send one agent action/message to the Miaoda application session.",
            input_schema=_schema(
                {
                    "message": {"type": "string", "minLength": 1},
                    "metadata": {"type": "object"},
                },
                required=("message",),
            ),
        ),
        ToolDefinition(
            name="submit",
            description="Submit the current Miaoda application result and finish the episode.",
            input_schema=_schema({"result": {"type": "string"}}),
        ),
    )


class SweEnvironment(DelegatingEnvironment):
    """SWE task adapter; the backend normally wraps a disposable sandbox."""

    env_name = "swe"
    _agent_protocol = (
        "Work directly in the disposable /testbed repository. Diagnose the task, "
        "edit the repository to implement the requested fix, run focused tests, and "
        "call submit once the solution is ready. Use the available bash, read_file, "
        "and write_file tools as needed. Do not merely describe a solution or stop "
        "after inspection."
    )
    _tools = (
        ToolDefinition(
            name="bash",
            description="Run a bounded shell command in the task sandbox.",
            input_schema=_schema(
                {"command": {"type": "string", "minLength": 1}},
                required=("command",),
            ),
        ),
        ToolDefinition(
            name="read_file",
            description="Read a file from the task sandbox.",
            input_schema=_schema(
                {"path": {"type": "string", "minLength": 1}}, required=("path",)
            ),
        ),
        ToolDefinition(
            name="write_file",
            description="Write a file in the task sandbox.",
            input_schema=_schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                required=("path", "content"),
            ),
        ),
        ToolDefinition(
            name="submit",
            description="Run the task verifier and finish the SWE episode.",
            input_schema=_schema({}),
        ),
    )

    async def get_prompt(self) -> Sequence[Block]:
        """Prepend the domain protocol without exposing verifier or gold data."""

        blocks = list(await super().get_prompt())
        if blocks and isinstance(blocks[0], TextBlock):
            first = blocks[0]
            blocks[0] = TextBlock(
                text=f"{self._agent_protocol}\n\nTask:\n{first.text}",
                detail=first.detail,
            )
        else:
            blocks.insert(0, TextBlock(text=self._agent_protocol))
        return blocks


class PptEnvironment(DelegatingEnvironment):
    """PowerPoint/document task adapter.

    Images in the initial task (reference slides or screenshots) are returned
    as ORS ``ImageBlock`` values, so a multimodal policy can consume them
    without a PPT-specific transport extension.
    """

    env_name = "ppt"
    _agent_protocol = (
        "Work directly in the isolated /workspace presentation environment. "
        "Inspect any declared /inputs assets. The installed PPTX skill is at "
        "/opt/claude-skills/pptx/SKILL.md and python-pptx is already available; "
        "inspect only /workspace, /inputs, and /opt/claude-skills/pptx and never "
        "scan the filesystem root or unrelated mounts. Use the skill and available "
        "tools to create or edit /workspace/slides.pptx. Keep generation actions "
        "compact by using reusable helper functions and structured slide-data arrays "
        "instead of repeated per-slide boilerplate; keep each tool call below 6000 "
        "policy tokens. For an existing-deck edit task, copy or unpack the declared "
        "current PPTX once, inspect only the requested slides, apply all requested "
        "edits in one bounded script when possible, and repack promptly. "
        "For small text, shape, and slide additions to an existing deck, prefer one "
        "python-pptx script that opens the source and saves /workspace/slides.pptx; "
        "do not echo or replace an entire slide XML document through edit_file. "
        "For an existing-deck edit, do not run scripts/clean.py: it may delete valid "
        "slide parts after relationship edits. Save directly with python-pptx, or "
        "repack the bounded unpacked tree without a cleanup pass. Do not rebuild "
        "presentation.xml.rels wholesale. "
        "When adding a slide with the installed scripts/add_slide.py helper, treat its "
        "presentation.xml and presentation.xml.rels updates as authoritative; do not "
        "append duplicate slide IDs or relationships by hand. "
        "Then inspect the artifact "
        "with ppt_read and render. Fix material content or rendering problems before "
        "calling submit_deliverable with /workspace/slides.pptx. Do not merely "
        "describe a deck or stop after planning."
    )
    _tools = (
        ToolDefinition(
            name="bash",
            description="Run a bounded command in the isolated presentation workspace.",
            input_schema=_schema(
                {"command": {"type": "string", "minLength": 1}},
                required=("command",),
            ),
        ),
        ToolDefinition(
            name="read_file",
            description="Read a text file from /workspace or a reference from /inputs.",
            input_schema=_schema(
                {"path": {"type": "string", "minLength": 1}}, required=("path",)
            ),
        ),
        ToolDefinition(
            name="write_file",
            description="Write a text file below /workspace.",
            input_schema=_schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                required=("path", "content"),
            ),
        ),
        ToolDefinition(
            name="edit_file",
            description="Replace one exact text fragment in a file below /workspace.",
            input_schema=_schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"},
                },
                required=("path", "old_text", "new_text"),
            ),
        ),
        ToolDefinition(
            name="ppt_read",
            description="Inspect slide count and editable text in a PPTX artifact.",
            input_schema=_schema(
                {"path": {"type": "string", "minLength": 1}}, required=("path",)
            ),
        ),
        ToolDefinition(
            name="ppt_write",
            description="Write a text source file used to create or modify a presentation.",
            input_schema=_schema(
                {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                required=("path", "content"),
            ),
        ),
        ToolDefinition(
            name="render",
            description="Render the current presentation for visual inspection.",
            input_schema=_schema(
                {"path": {"type": "string", "minLength": 1}}, required=("path",)
            ),
        ),
        ToolDefinition(
            name="submit_deliverable",
            description="Submit the PPT artifact to the rubric/verifier and finish.",
            input_schema=_schema(
                {"path": {"type": "string", "minLength": 1}}, required=("path",)
            ),
        ),
    )

    async def get_prompt(self) -> Sequence[Block]:
        """Prepend the PPT execution/QA protocol without exposing rubric answers."""

        blocks = list(await super().get_prompt())
        if blocks and isinstance(blocks[0], TextBlock):
            first = blocks[0]
            blocks[0] = TextBlock(
                text=f"{self._agent_protocol}\n\nTask:\n{first.text}",
                detail=first.detail,
            )
        else:
            blocks.insert(0, TextBlock(text=self._agent_protocol))
        return blocks


class ToolUseEnvironment(DelegatingEnvironment):
    """Generic stateful API/tool-use task adapter."""

    env_name = "tooluse"
    _tools = (
        ToolDefinition(
            name="invoke_tool",
            description="Invoke the task's registered external tool.",
            input_schema=_schema(
                {
                    "tool_name": {"type": "string", "minLength": 1},
                    "arguments": {"type": "object"},
                },
                required=("tool_name",),
            ),
        ),
        ToolDefinition(
            name="get_state",
            description="Inspect the current task-visible state without exposing credentials.",
            input_schema=_schema({}),
        ),
        ToolDefinition(
            name="submit",
            description="Submit the final state for verification and finish.",
            input_schema=_schema({"answer": {"type": "string"}}),
        ),
    )


def _factory(
    cls: type[DelegatingEnvironment],
    backend: EnvironmentBackend | None,
    task_catalog: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> type[DelegatingEnvironment]:
    # A per-factory subclass keeps task listing isolated between environments
    # and tests.  It avoids a process-global mutable task registry.
    catalog = {
        str(split): tuple(dict(task) for task in tasks)
        for split, tasks in (task_catalog or {}).items()
    }

    class ConfiguredEnvironment(cls):
        _task_catalog = catalog

        def __init__(self, task_spec: Mapping[str, Any], secrets: Mapping[str, Any]) -> None:
            super().__init__(task_spec, secrets, backend=backend)

    ConfiguredEnvironment.__name__ = f"{cls.__name__}FactoryBound"
    return ConfiguredEnvironment


def build_default_environments(
    *,
    backends: Mapping[str, EnvironmentBackend] | None = None,
    task_catalogs: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]] | None = None,
) -> dict[str, type[ORSEnvironment]]:
    """Return the four standard environments as server-ready classes.

    ``backends`` and ``task_catalogs`` are optional.  With neither supplied,
    the result is safe for local protocol smoke tests.  Production callers
    should inject real backend objects and task catalogs explicitly.
    """

    backends = backends or {}
    task_catalogs = task_catalogs or {}
    classes: dict[str, type[ORSEnvironment]] = {}
    for name, cls in (
        ("miaoda", MiaodaEnvironment),
        ("swe", SweEnvironment),
        ("ppt", PptEnvironment),
        ("tooluse", ToolUseEnvironment),
    ):
        if name in backends:
            backend = backends[name]
        else:
            # Import lazily to avoid a module cycle: the domain mocks return
            # BackendResult, which is defined above in this module.
            from .mock_backends import build_mock_backend

            backend = build_mock_backend(name)
        bound = _factory(cls, backend, task_catalogs.get(name))
        classes[name] = bound
    return classes
