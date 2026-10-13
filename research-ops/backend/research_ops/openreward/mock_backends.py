"""Deterministic, side-effect-free mock backends for the four ORS domains.

The mocks model each domain's important state transitions instead of merely
returning canned strings.  They are suitable for local protocol tests and
examples only: no shell command is executed, no Office file is created, and no
external service is contacted.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .adapters import BackendResult
from .types import ImageBlock, JSONObject, TextBlock, ToolSpec

# A valid 1x1 transparent PNG.  Returning a real image block exercises the ORS
# multimodal wire format without requiring LibreOffice or a rendering image.
_TRANSPARENT_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/x8AAusB9Y9Z4WQAAAAASUVORK5CYII="
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mock_id(task_spec: Mapping[str, Any]) -> str:
    source = _text(
        task_spec.get("episode_id")
        or task_spec.get("case_id")
        or task_spec.get("id")
        or task_spec.get("query")
        or "mock"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _safe_path(value: Any) -> str:
    raw = _text(value)
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError("mock workspace path must be relative and contained")
    normalized = path.as_posix()
    if len(normalized) > 512:
        raise ValueError("mock workspace path is too long")
    return normalized


def _numeric_reward(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _submission_reward(task_spec: Mapping[str, Any], submitted: Any) -> float:
    expected = task_spec.get("expected_output")
    if expected is None:
        return _numeric_reward(task_spec.get("mock_reward"), 1.0)
    return 1.0 if submitted == expected else 0.0


@dataclass
class MiaodaMockBackend:
    """Model a complete AgentOS -> Goblin asynchronous episode."""

    label: str = "mock/miaoda"

    def setup(self, *, task_spec: JSONObject, secrets: Mapping[str, Any], state: dict) -> None:
        del secrets
        suffix = _mock_id(task_spec)
        state["mock_miaoda"] = {
            "phase": "ready",
            "messages": [],
            "app_id": f"mock-app-{suffix}",
            "conv_id": f"mock-conv-{suffix}",
        }

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict,
    ) -> BackendResult:
        runtime = state["mock_miaoda"]
        phase = str(runtime["phase"])
        metadata = {
            "backend": self.label,
            "phase": phase,
            "app_id": runtime["app_id"],
            "conv_id": runtime["conv_id"],
        }
        if phase == "terminal":
            return BackendResult(
                blocks=[TextBlock(text="mock Miaoda episode already finished")],
                reward=float(runtime["reward"]),
                finished=True,
                metadata=metadata,
            )
        if action == "start_rollout":
            if phase != "ready":
                return BackendResult(
                    blocks=[TextBlock(text="mock Miaoda rollout already started")],
                    metadata=metadata,
                )
            runtime["phase"] = "rollout"
            metadata["phase"] = "rollout"
            metadata["status"] = "submitted"
            return BackendResult(
                blocks=[TextBlock(text="mock AgentOS rollout submitted")],
                metadata=metadata,
            )
        if action == "poll_rollout":
            if phase == "ready":
                raise ValueError("start_rollout must be called before poll_rollout")
            if phase == "rollout":
                runtime["phase"] = "reward"
                metadata.update({"phase": "reward", "status": "goblin_pending"})
                return BackendResult(
                    blocks=[TextBlock(text="mock AgentOS completed; Goblin pending")],
                    metadata=metadata,
                )
            reward = _numeric_reward(task_spec.get("mock_reward"), 1.0)
            runtime.update({"phase": "terminal", "reward": reward})
            metadata.update(
                {"phase": "terminal", "status": "completed", "usable": True}
            )
            return BackendResult(
                blocks=[TextBlock(text="mock Goblin reward ready")],
                reward=reward,
                finished=True,
                metadata=metadata,
            )
        if action == "miaoda_step":
            message = _text(payload.get("message"))
            runtime["messages"].append(message)
            return BackendResult(
                blocks=[TextBlock(text="mock AgentOS awaits the next user action")],
                metadata={
                    **metadata,
                    "agent_state": "awaiting_user_input",
                    "message_count": len(runtime["messages"]),
                },
            )
        if action == "submit":
            submitted = payload.get("result")
            reward = _submission_reward(task_spec, submitted)
            runtime.update({"phase": "terminal", "reward": reward})
            return BackendResult(
                blocks=[TextBlock(text="mock Miaoda direct result submitted")],
                reward=reward,
                finished=True,
                metadata={**metadata, "phase": "terminal", "usable": True},
            )
        raise KeyError(action)

    def teardown(self, *, task_spec: JSONObject, state: dict) -> None:
        del task_spec
        state.pop("mock_miaoda", None)


def _swe_verdict(task_spec: Mapping[str, Any], files: Mapping[str, str]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    expected = task_spec.get("expected_files")
    if isinstance(expected, Mapping):
        for raw_path, content in expected.items():
            path = _safe_path(raw_path)
            if files.get(path) != str(content):
                failures.append(path)
    required = task_spec.get("required_paths")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        for raw_path in required:
            path = _safe_path(raw_path)
            if path not in files:
                failures.append(path)
    return not failures, sorted(set(failures))


@dataclass
class SweMockBackend:
    """In-memory SWE workspace with a deterministic verifier."""

    label: str = "mock/swe"

    def setup(self, *, task_spec: JSONObject, secrets: Mapping[str, Any], state: dict) -> None:
        del secrets
        initial = task_spec.get("initial_files")
        files: dict[str, str] = {}
        if isinstance(initial, Mapping):
            files = {_safe_path(path): str(content) for path, content in initial.items()}
        state["mock_swe"] = {"files": files, "terminal": False, "reward": None}

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict,
    ) -> BackendResult:
        runtime = state["mock_swe"]
        files: dict[str, str] = runtime["files"]
        if runtime["terminal"]:
            return BackendResult(
                blocks=[TextBlock(text="mock SWE episode already finished")],
                reward=runtime["reward"],
                finished=True,
                metadata={"backend": self.label, "file_count": len(files)},
            )
        if action == "write_file":
            path = _safe_path(payload.get("path"))
            files[path] = str(payload.get("content") or "")
            return BackendResult(
                blocks=[TextBlock(text=f"wrote {path}")],
                metadata={"backend": self.label, "path": path, "file_count": len(files)},
            )
        if action == "read_file":
            path = _safe_path(payload.get("path"))
            if path not in files:
                return BackendResult(
                    blocks=[TextBlock(text=f"{path}: not found")],
                    metadata={"backend": self.label, "path": path, "found": False},
                )
            return BackendResult(
                blocks=[TextBlock(text=files[path])],
                metadata={"backend": self.label, "path": path, "found": True},
            )
        if action == "bash":
            command = _text(payload.get("command"))
            output, exit_code = self._mock_command(command, task_spec, files)
            return BackendResult(
                blocks=[TextBlock(text=output)],
                metadata={"backend": self.label, "exit_code": exit_code},
            )
        if action == "submit":
            passed, failures = _swe_verdict(task_spec, files)
            reward = 1.0 if passed else 0.0
            runtime.update({"terminal": True, "reward": reward})
            text = "mock SWE verifier passed"
            if failures:
                text = "mock SWE verifier failed for: " + ", ".join(failures)
            return BackendResult(
                blocks=[TextBlock(text=text)],
                reward=reward,
                finished=True,
                metadata={
                    "backend": self.label,
                    "verifier_passed": passed,
                    "failure_count": len(failures),
                    "file_count": len(files),
                },
            )
        raise KeyError(action)

    @staticmethod
    def _mock_command(
        command: str,
        task_spec: Mapping[str, Any],
        files: Mapping[str, str],
    ) -> tuple[str, int]:
        configured = task_spec.get("mock_command_outputs")
        if isinstance(configured, Mapping) and command in configured:
            result = configured[command]
            if isinstance(result, Mapping):
                return _text(result.get("text")), int(result.get("exit_code") or 0)
            return str(result), 0
        try:
            parts = shlex.split(command)
        except ValueError:
            return "mock shell parse error", 2
        if parts == ["pwd"]:
            return "/workspace", 0
        if parts and parts[0] in {"ls", "find"}:
            return "\n".join(sorted(files)), 0
        if len(parts) == 2 and parts[0] == "cat":
            try:
                path = _safe_path(parts[1])
            except ValueError:
                return "mock path rejected", 2
            return (files[path], 0) if path in files else (f"{path}: not found", 1)
        verifier_command = _text(task_spec.get("test_command"))
        if command in {"pytest", "pytest -q", verifier_command} - {""}:
            passed, failures = _swe_verdict(task_spec, files)
            return (
                "mock tests passed" if passed else f"mock tests failed: {len(failures)}",
                0 if passed else 1,
            )
        return "mock backend did not execute this command", 127

    def teardown(self, *, task_spec: JSONObject, state: dict) -> None:
        del task_spec
        state.pop("mock_swe", None)


@dataclass
class PptMockBackend:
    """In-memory presentation store with a real ORS ImageBlock render."""

    label: str = "mock/ppt"

    def setup(self, *, task_spec: JSONObject, secrets: Mapping[str, Any], state: dict) -> None:
        del secrets
        initial = task_spec.get("initial_presentations")
        presentations: dict[str, str] = {}
        if isinstance(initial, Mapping):
            presentations = {
                _safe_path(path): str(content) for path, content in initial.items()
            }
        state["mock_ppt"] = {
            "presentations": presentations,
            "terminal": False,
            "reward": None,
        }

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict,
    ) -> BackendResult:
        runtime = state["mock_ppt"]
        presentations: dict[str, str] = runtime["presentations"]
        if runtime["terminal"]:
            return BackendResult(
                blocks=[TextBlock(text="mock PPT episode already finished")],
                reward=runtime["reward"],
                finished=True,
                metadata={"backend": self.label},
            )
        if action == "ppt_write":
            path = _safe_path(payload.get("path"))
            presentations[path] = str(payload.get("content") or "")
            return BackendResult(
                blocks=[TextBlock(text=f"updated {path}")],
                metadata={"backend": self.label, "artifact_path": path},
            )
        if action == "ppt_read":
            path = _safe_path(payload.get("path"))
            content = presentations.get(path)
            return BackendResult(
                blocks=[TextBlock(text=content if content is not None else f"{path}: not found")],
                metadata={
                    "backend": self.label,
                    "artifact_path": path,
                    "found": content is not None,
                },
            )
        if action == "render":
            path = _safe_path(payload.get("path"))
            if path not in presentations:
                return BackendResult(
                    blocks=[TextBlock(text=f"{path}: not found")],
                    metadata={"backend": self.label, "artifact_path": path, "found": False},
                )
            return BackendResult(
                blocks=[
                    TextBlock(text=f"rendered {path}"),
                    ImageBlock(data=_TRANSPARENT_PNG, mimeType="image/png"),
                ],
                metadata={
                    "backend": self.label,
                    "artifact_path": path,
                    "render_count": 1,
                },
            )
        if action == "submit_deliverable":
            path = _safe_path(payload.get("path"))
            content = presentations.get(path)
            passed = content is not None
            expected_path = task_spec.get("expected_output")
            if expected_path is not None:
                passed = passed and path == _safe_path(expected_path)
            expected_content = task_spec.get("expected_content")
            if expected_content is not None:
                passed = passed and content == str(expected_content)
            required_text = task_spec.get("required_text")
            if isinstance(required_text, Sequence) and not isinstance(
                required_text, (str, bytes)
            ):
                passed = passed and content is not None and all(
                    str(item) in content for item in required_text
                )
            reward = 1.0 if passed else 0.0
            runtime.update({"terminal": True, "reward": reward})
            return BackendResult(
                blocks=[
                    TextBlock(
                        text="mock PPT verifier passed"
                        if passed
                        else "mock PPT verifier failed"
                    )
                ],
                reward=reward,
                finished=True,
                metadata={
                    "backend": self.label,
                    "artifact_path": path,
                    "verifier_passed": passed,
                },
            )
        raise KeyError(action)

    def teardown(self, *, task_spec: JSONObject, state: dict) -> None:
        del task_spec
        state.pop("mock_ppt", None)


def _tool_spec(registry: Any, name: str) -> Mapping[str, Any] | str | None:
    if isinstance(registry, Mapping):
        value = registry.get(name)
        if isinstance(value, (str, Mapping)):
            return value
        return None
    return name if name in {"set", "get", "increment", "append", "delete"} else None


@dataclass
class ToolUseMockBackend:
    """Stateful mock tool registry with target-state verification."""

    label: str = "mock/tooluse"

    def setup(self, *, task_spec: JSONObject, secrets: Mapping[str, Any], state: dict) -> None:
        del secrets
        initial = task_spec.get("initial_state")
        state["mock_tooluse"] = {
            "values": dict(initial) if isinstance(initial, Mapping) else {},
            "terminal": False,
            "reward": None,
            "invocations": 0,
        }

    def list_tools(
        self,
        *,
        task_spec: JSONObject,
        state: dict,
    ) -> list[ToolSpec]:
        del state
        registry = task_spec.get("tool_registry")
        if not isinstance(registry, Mapping):
            return []
        tools: list[ToolSpec] = []
        for name, raw in registry.items():
            if not isinstance(raw, Mapping) or "input_schema" not in raw:
                continue
            schema = raw.get("input_schema")
            if schema is not None and not isinstance(schema, Mapping):
                raise ValueError("mock tool input_schema must be an object")
            tools.append(
                ToolSpec(
                    name=str(name),
                    description=_text(raw.get("description")),
                    input_schema=dict(schema) if isinstance(schema, Mapping) else None,
                )
            )
        return tools

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict,
    ) -> BackendResult:
        runtime = state["mock_tooluse"]
        values: dict[str, Any] = runtime["values"]
        if runtime["terminal"]:
            return BackendResult(
                blocks=[TextBlock(text="mock ToolUse episode already finished")],
                reward=runtime["reward"],
                finished=True,
                metadata={"backend": self.label},
            )
        if action == "invoke_tool":
            name = _text(payload.get("tool_name"))
            raw_spec = _tool_spec(task_spec.get("tool_registry"), name)
            if raw_spec is None:
                raise ValueError("tool_name is not registered for this task")
            spec = dict(raw_spec) if isinstance(raw_spec, Mapping) else {"operation": raw_spec}
            operation = _text(spec.get("operation") or name)
            arguments = payload.get("arguments")
            arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
            key = _text(arguments.get("key") or spec.get("key"))
            result = self._apply(operation, key, arguments, spec, values)
            runtime["invocations"] += 1
            visible = json.dumps(result, ensure_ascii=False, sort_keys=True)[:2_000]
            return BackendResult(
                blocks=[TextBlock(text=visible)],
                metadata={
                    "backend": self.label,
                    "tool_name": name,
                    "operation": operation,
                    "invocation_count": runtime["invocations"],
                },
            )
        if action == "get_state":
            return BackendResult(
                blocks=[
                    TextBlock(
                        text=json.dumps(values, ensure_ascii=False, sort_keys=True)[:2_000]
                    )
                ],
                metadata={
                    "backend": self.label,
                    "state_keys": sorted(values),
                    "invocation_count": runtime["invocations"],
                },
            )
        if action not in {"submit", "invoke_tool"}:
            raw_spec = _tool_spec(task_spec.get("tool_registry"), action)
            if raw_spec is None:
                raise KeyError(action)
            spec = dict(raw_spec) if isinstance(raw_spec, Mapping) else {"operation": raw_spec}
            operation = _text(spec.get("operation") or action)
            key = _text(payload.get("key") or spec.get("key"))
            result = self._apply(operation, key, payload, spec, values)
            runtime["invocations"] += 1
            return BackendResult(
                blocks=[
                    TextBlock(
                        text=json.dumps(result, ensure_ascii=False, sort_keys=True)[:2_000]
                    )
                ],
                metadata={
                    "backend": self.label,
                    "tool_name": action,
                    "operation": operation,
                    "invocation_count": runtime["invocations"],
                },
            )
        if action == "submit":
            target = task_spec.get("target_state")
            passed = True
            if isinstance(target, Mapping):
                passed = all(values.get(str(key)) == value for key, value in target.items())
            expected = task_spec.get("expected_output")
            if expected is not None:
                passed = passed and payload.get("answer") == expected
            reward = 1.0 if passed else 0.0
            runtime.update({"terminal": True, "reward": reward})
            return BackendResult(
                blocks=[
                    TextBlock(
                        text="mock ToolUse verifier passed"
                        if passed
                        else "mock ToolUse verifier failed"
                    )
                ],
                reward=reward,
                finished=True,
                metadata={
                    "backend": self.label,
                    "verifier_passed": passed,
                    "state_keys": sorted(values),
                    "invocation_count": runtime["invocations"],
                },
            )
        raise KeyError(action)

    @staticmethod
    def _apply(
        operation: str,
        key: str,
        arguments: Mapping[str, Any],
        spec: Mapping[str, Any],
        values: dict[str, Any],
    ) -> Any:
        if operation != "get" and not key:
            raise ValueError("tool operation requires a key")
        if operation == "get":
            if not key:
                raise ValueError("get requires a key")
            return values.get(key)
        if operation == "set":
            if "value" not in arguments and "value" not in spec:
                raise ValueError("set requires a value")
            values[key] = arguments.get("value", spec.get("value"))
            return values[key]
        if operation == "increment":
            current = values.get(key, 0)
            delta = arguments.get("delta", spec.get("delta", 1))
            if not isinstance(current, (int, float)) or isinstance(current, bool):
                raise ValueError("increment target is not numeric")
            if not isinstance(delta, (int, float)) or isinstance(delta, bool):
                raise ValueError("increment delta is not numeric")
            values[key] = current + delta
            return values[key]
        if operation == "append":
            current = values.setdefault(key, [])
            if not isinstance(current, list):
                raise ValueError("append target is not a list")
            if "value" not in arguments and "value" not in spec:
                raise ValueError("append requires a value")
            current.append(arguments.get("value", spec.get("value")))
            return list(current)
        if operation == "delete":
            return values.pop(key, None)
        raise ValueError("registered tool operation is unsupported by the mock")

    def teardown(self, *, task_spec: JSONObject, state: dict) -> None:
        del task_spec
        state.pop("mock_tooluse", None)


def build_mock_backend(name: str) -> Any:
    """Return the domain-specific mock used by the default local registry."""

    factories = {
        "miaoda": MiaodaMockBackend,
        "swe": SweMockBackend,
        "ppt": PptMockBackend,
        "tooluse": ToolUseMockBackend,
    }
    try:
        return factories[name]()
    except KeyError as exc:
        raise ValueError(f"unknown mock backend domain: {name}") from exc
