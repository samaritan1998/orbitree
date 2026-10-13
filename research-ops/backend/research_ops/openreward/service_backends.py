"""Credential-safe HTTP bridges for externally hosted ORS runtimes.

The public OpenReward server owns sessions, tool schemas, SSE, rewards and
termination.  Domain runtimes (a SWE sandbox, an Office/PPT service, or a
stateful tool registry) often already exist behind an internal HTTP API.  This
module adapts those services to the small :class:`EnvironmentBackend` contract
without baking any endpoint or credential into Research Ops.

All endpoint and credential *values* are resolved from environment variables
at request time.  Only their variable names live in the versioned backend
contract.  Remote services return the same JSON shape as ``BackendResult``;
that keeps provider-specific payloads behind a sidecar and makes the ORS
surface identical for Miaoda, SWE, PPT and ToolUse.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .adapters import BackendResult
from .environment import sensitive_task_key, visible_task_value
from .types import ImageBlock, JSONObject, TextBlock, ToolSpec

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
_OPERATION = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)$")
class RemoteBackendError(RuntimeError):
    """Base error for a remote ORS runtime bridge."""


class RemoteBackendConfigurationError(RemoteBackendError):
    """Raised when a runtime reference is absent or structurally invalid."""


class RemoteBackendProtocolError(RemoteBackendError):
    """Raised when a runtime does not implement the documented JSON contract."""


class RemoteBackendTaskError(RemoteBackendError):
    """A recoverable action rejection returned by a domain runtime."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class HttpOperation:
    """One method/path pair from a backend contract."""

    method: str
    path: str

    @classmethod
    def parse(cls, value: str | HttpOperation) -> HttpOperation:
        if isinstance(value, cls):
            return value
        match = _OPERATION.fullmatch(str(value).strip())
        if match is None:
            raise RemoteBackendConfigurationError(
                "remote operation must look like 'POST /v1/resource'"
            )
        return cls(method=match.group(1), path=match.group(2))

    def render(self, values: Mapping[str, Any]) -> str:
        safe_values = {
            str(key): quote(str(value), safe="")
            for key, value in values.items()
            if value is not None and str(value).strip()
        }
        try:
            return self.path.format_map(safe_values)
        except (KeyError, ValueError) as exc:
            raise RemoteBackendConfigurationError(
                "remote operation requires a missing path identifier"
            ) from exc


@dataclass(frozen=True)
class RemoteServiceConfig:
    """Runtime-neutral HTTP service configuration for one ORS domain.

    ``auth_header_envs`` maps an HTTP header name to an environment-variable
    name.  ``Authorization`` values are emitted as Bearer tokens; all other
    headers are emitted verbatim.  Values are never retained in ORS state.
    """

    environment: str
    base_url_env: str
    create_operation: str | HttpOperation
    action_operations: Mapping[str, str | HttpOperation]
    destroy_operation: str | HttpOperation
    auth_header_envs: Mapping[str, str] = field(default_factory=dict)
    tools_operation: str | HttpOperation | None = None
    dynamic_action_operation: str | HttpOperation | None = None
    terminal_actions: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 120.0
    verify_tls: bool = True
    trust_env: bool = False

    def __post_init__(self) -> None:
        environment = str(self.environment).strip().casefold()
        if not environment:
            raise RemoteBackendConfigurationError("remote environment name is empty")
        if not _ENV_NAME.fullmatch(str(self.base_url_env)):
            raise RemoteBackendConfigurationError(
                "remote base_url_env must be an environment-variable name"
            )
        if self.timeout_seconds <= 0:
            raise RemoteBackendConfigurationError("remote timeout must be positive")
        headers: dict[str, str] = {}
        for header, env_name in self.auth_header_envs.items():
            normalized_header = str(header).strip()
            normalized_env = str(env_name).strip()
            if not _HEADER_NAME.fullmatch(normalized_header):
                raise RemoteBackendConfigurationError("remote auth header name is invalid")
            if not _ENV_NAME.fullmatch(normalized_env):
                raise RemoteBackendConfigurationError(
                    "remote auth references must be environment-variable names"
                )
            headers[normalized_header] = normalized_env
        actions: dict[str, HttpOperation] = {}
        for action, operation in self.action_operations.items():
            normalized_action = str(action).strip()
            if not normalized_action:
                raise RemoteBackendConfigurationError("remote action name is empty")
            actions[normalized_action] = HttpOperation.parse(operation)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "auth_header_envs", headers)
        object.__setattr__(self, "action_operations", actions)
        object.__setattr__(self, "create_operation", HttpOperation.parse(self.create_operation))
        object.__setattr__(self, "destroy_operation", HttpOperation.parse(self.destroy_operation))
        if self.tools_operation is not None:
            object.__setattr__(
                self,
                "tools_operation",
                HttpOperation.parse(self.tools_operation),
            )
        if self.dynamic_action_operation is not None:
            object.__setattr__(
                self,
                "dynamic_action_operation",
                HttpOperation.parse(self.dynamic_action_operation),
            )
        object.__setattr__(
            self,
            "terminal_actions",
            frozenset(str(item).strip() for item in self.terminal_actions if str(item).strip()),
        )


@dataclass(frozen=True)
class RemoteORSConfig:
    """Configuration for proxying one already-ORS-compatible environment."""

    environment: str
    remote_environment: str
    base_url_env: str
    auth_header_envs: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 300.0
    verify_tls: bool = True
    trust_env: bool = False

    def __post_init__(self) -> None:
        environment = str(self.environment).strip().casefold()
        remote_environment = str(self.remote_environment).strip().casefold()
        if not environment or not remote_environment:
            raise RemoteBackendConfigurationError("ORS proxy environment name is empty")
        if not _ENV_NAME.fullmatch(str(self.base_url_env)):
            raise RemoteBackendConfigurationError(
                "ORS proxy base_url_env must be an environment-variable name"
            )
        if self.timeout_seconds <= 0:
            raise RemoteBackendConfigurationError("ORS proxy timeout must be positive")
        headers: dict[str, str] = {}
        for header, env_name in self.auth_header_envs.items():
            normalized_header = str(header).strip()
            normalized_env = str(env_name).strip()
            if not _HEADER_NAME.fullmatch(normalized_header):
                raise RemoteBackendConfigurationError("ORS proxy auth header name is invalid")
            if not _ENV_NAME.fullmatch(normalized_env):
                raise RemoteBackendConfigurationError(
                    "ORS proxy auth references must be environment-variable names"
                )
            headers[normalized_header] = normalized_env
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "remote_environment", remote_environment)
        object.__setattr__(self, "auth_header_envs", headers)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _remote_task_id(task_spec: Mapping[str, Any]) -> str:
    for key in ("task_id", "episode_id", "case_id", "id", "record_id"):
        value = str(task_spec.get(key) or "").strip()
        if value:
            return value
    safe = visible_task_value(task_spec)
    canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"task-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


def _session_id(payload: Mapping[str, Any]) -> str:
    candidates = (payload, _mapping(payload.get("result")), _mapping(payload.get("data")))
    for candidate in candidates:
        for key in ("session_id", "sandbox_id", "workspace_id", "id"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
    raise RemoteBackendProtocolError("remote create response omitted a session identifier")


def _safe_response_value(value: Any) -> Any:
    """Drop credential-like response fields before they enter ORS metadata."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_response_value(child)
            for key, child in value.items()
            if not sensitive_task_key(key)
        }
    if isinstance(value, list):
        return [_safe_response_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_response_value(item) for item in value]
    return value


class RemoteServiceBackend:
    """Bridge one task-scoped HTTP runtime into an ORS environment backend.

    Runtime request contract:

    * create: ``{environment, task_id, task_spec}``
    * action: ``{environment, session_id, task_id, action, input}``
    * destroy: ``{environment, session_id, task_id}``

    Action responses must be either a ``BackendResult`` object or
    ``{"output": <BackendResult>}``.  A create response must expose one of
    ``session_id``, ``sandbox_id``, ``workspace_id`` or ``id`` and may include
    a ``tools`` array containing standard ORS ``ToolSpec`` objects.
    """

    def __init__(
        self,
        config: RemoteServiceConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.environ = os.environ if environ is None else environ
        self.transport = transport

    def _base_url(self) -> str:
        value = str(self.environ.get(self.config.base_url_env, "")).strip().rstrip("/")
        if not value:
            raise RemoteBackendConfigurationError(
                f"remote runtime reference is missing: {self.config.base_url_env}"
            )
        if not value.startswith(("http://", "https://")):
            raise RemoteBackendConfigurationError("remote runtime URL must use HTTP or HTTPS")
        return value

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        for header, env_name in self.config.auth_header_envs.items():
            value = str(self.environ.get(env_name, "")).strip()
            if not value:
                raise RemoteBackendConfigurationError(
                    f"remote credential reference is missing: {env_name}"
                )
            if header.casefold() == "authorization" and not value.casefold().startswith(
                ("bearer ", "basic ")
            ):
                value = f"Bearer {value}"
            headers[header] = value
        return headers

    def _redact(self, value: Any) -> Any:
        secrets = [
            str(self.environ.get(env_name, "")).strip()
            for env_name in self.config.auth_header_envs.values()
        ]
        secrets = [item for item in secrets if item]
        safe = _safe_response_value(value)

        def visit(item: Any) -> Any:
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "<redacted>")
                return item
            if isinstance(item, Mapping):
                return {str(key): visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            return item

        return visit(safe)

    async def _request(
        self,
        operation: HttpOperation,
        *,
        identifiers: Mapping[str, Any],
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = operation.render(identifiers)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url(),
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_tls,
                trust_env=self.config.trust_env,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    operation.method,
                    path,
                    headers=self._headers(),
                    json=dict(body) if body is not None else None,
                )
        except httpx.HTTPError as exc:
            raise RemoteBackendError(
                f"remote runtime request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            if response.status_code in {400, 409, 422}:
                detail = "the runtime rejected this tool input"
                try:
                    error_payload = self._redact(response.json())
                except (TypeError, ValueError):
                    error_payload = None
                if isinstance(error_payload, Mapping):
                    raw_detail = (
                        error_payload.get("detail")
                        or error_payload.get("error")
                        or error_payload.get("message")
                    )
                    if raw_detail is not None:
                        detail = str(raw_detail).strip()[:1000] or detail
                raise RemoteBackendTaskError(response.status_code, detail)
            raise RemoteBackendError(
                f"remote runtime returned HTTP {response.status_code}"
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RemoteBackendProtocolError("remote runtime returned non-JSON") from exc
        if not isinstance(payload, Mapping):
            raise RemoteBackendProtocolError("remote runtime returned a non-object")
        return self._redact(payload)

    @staticmethod
    def _identifiers(state: Mapping[str, Any], *, tool_name: str | None = None) -> dict[str, str]:
        session_id = str(state.get("_ors_remote_session_id") or "").strip()
        task_id = str(state.get("_ors_remote_task_id") or "").strip()
        return {
            "session_id": session_id,
            "sandbox_id": session_id,
            "workspace_id": session_id,
            "task_id": task_id,
            "tool_name": str(tool_name or "").strip(),
        }

    async def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        del secrets
        task_id = _remote_task_id(task_spec)
        identifiers = {"task_id": task_id}
        response = await self._request(
            self.config.create_operation,
            identifiers=identifiers,
            body={
                "environment": self.config.environment,
                "task_id": task_id,
                "task_spec": visible_task_value(task_spec),
            },
        )
        state["_ors_remote_session_id"] = _session_id(response)
        state["_ors_remote_task_id"] = task_id
        tools = response.get("tools")
        if isinstance(tools, Sequence) and not isinstance(tools, (str, bytes)):
            state["_ors_remote_tools"] = [dict(item) for item in tools if isinstance(item, Mapping)]

    async def list_tools(
        self,
        *,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> list[ToolSpec]:
        del task_spec
        raw_tools = state.get("_ors_remote_tools")
        if raw_tools is None and self.config.tools_operation is not None:
            response = await self._request(
                self.config.tools_operation,
                identifiers=self._identifiers(state),
            )
            raw_tools = response.get("tools", [])
            if isinstance(raw_tools, Sequence) and not isinstance(raw_tools, (str, bytes)):
                state["_ors_remote_tools"] = [
                    dict(item) for item in raw_tools if isinstance(item, Mapping)
                ]
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
            return []
        result: list[ToolSpec] = []
        for item in raw_tools:
            if not isinstance(item, Mapping):
                continue
            try:
                result.append(ToolSpec.model_validate(item))
            except ValueError as exc:
                raise RemoteBackendProtocolError(
                    "remote runtime returned an invalid tool specification"
                ) from exc
        return result

    async def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult:
        del task_spec
        operation = self.config.action_operations.get(action)
        dynamic = operation is None
        if dynamic:
            operation = self.config.dynamic_action_operation
        if operation is None:
            raise KeyError(action)
        if not state.get("_ors_remote_session_id"):
            raise RemoteBackendProtocolError("remote runtime session is not initialized")
        tool_name = str(payload.get("tool_name") or action).strip()
        body_input: JSONObject
        if dynamic:
            body_input = {
                "tool_name": tool_name,
                "arguments": dict(payload),
            }
        else:
            body_input = dict(payload)
        identifiers = self._identifiers(state, tool_name=tool_name)
        try:
            response = await self._request(
                operation,
                identifiers=identifiers,
                body={
                    "environment": self.config.environment,
                    "session_id": identifiers["session_id"],
                    "task_id": identifiers["task_id"],
                    "action": action,
                    "input": visible_task_value(body_input),
                },
            )
        except RemoteBackendTaskError as exc:
            return BackendResult(
                blocks=[
                    TextBlock(
                        text=(
                            "Tool call rejected by the sandbox: "
                            f"{exc.detail}. Correct the action and retry."
                        )
                    )
                ],
                reward=None,
                finished=False,
                metadata={
                    "runtime": "remote-service",
                    "action": action,
                    "tool_error": True,
                    "status_code": exc.status_code,
                },
            )
        output = response.get("output", response)
        if not isinstance(output, Mapping):
            raise RemoteBackendProtocolError("remote action response omitted output")
        normalized = self._normalize_output(output, action=action)
        if action in self.config.terminal_actions and "finished" not in output:
            normalized.finished = True
        return normalized

    @staticmethod
    def _normalize_output(output: Mapping[str, Any], *, action: str) -> BackendResult:
        raw = dict(output)
        if "blocks" not in raw:
            text = next(
                (
                    raw.get(key)
                    for key in ("text", "stdout", "content", "message")
                    if raw.get(key) is not None
                ),
                "",
            )
            blocks: list[dict[str, Any]] = []
            if text != "":
                blocks.append(TextBlock(text=str(text)).model_dump(mode="json"))
            images = raw.get("images")
            if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
                for image in images:
                    if isinstance(image, Mapping):
                        blocks.append(ImageBlock.model_validate(image).model_dump(mode="json"))
            raw["blocks"] = blocks or [TextBlock(text="").model_dump(mode="json")]
        raw.setdefault("finished", False)
        raw.setdefault("reward", None)
        raw.setdefault("metadata", {"action": action})
        allowed = {key: raw[key] for key in ("blocks", "reward", "finished", "metadata")}
        try:
            return BackendResult.model_validate(allowed)
        except ValueError as exc:
            raise RemoteBackendProtocolError(
                "remote action response is not a valid BackendResult"
            ) from exc

    async def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec
        session_id = str(state.get("_ors_remote_session_id") or "").strip()
        try:
            if session_id:
                identifiers = self._identifiers(state)
                await self._request(
                    self.config.destroy_operation,
                    identifiers=identifiers,
                    body={
                        "environment": self.config.environment,
                        "session_id": identifiers["session_id"],
                        "task_id": identifiers["task_id"],
                    },
                )
        finally:
            state.pop("_ors_remote_session_id", None)
            state.pop("_ors_remote_task_id", None)
            state.pop("_ors_remote_tools", None)


class RemoteORSBackend:
    """Proxy an OpenReward environment behind the unified Research Ops ORS."""

    replace_environment_tools = True

    def __init__(
        self,
        config: RemoteORSConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.environ = os.environ if environ is None else environ
        self.transport = transport

    def _base_url(self) -> str:
        value = str(self.environ.get(self.config.base_url_env, "")).strip().rstrip("/")
        if not value:
            raise RemoteBackendConfigurationError(
                f"remote ORS reference is missing: {self.config.base_url_env}"
            )
        if not value.startswith(("http://", "https://")):
            raise RemoteBackendConfigurationError("remote ORS URL must use HTTP or HTTPS")
        return value

    def _headers(
        self,
        *,
        session_id: str | None = None,
        accept: str = "application/json",
    ) -> dict[str, str]:
        headers = {"Accept": accept, "Content-Type": "application/json"}
        if session_id:
            headers["X-Session-ID"] = session_id
        for header, env_name in self.config.auth_header_envs.items():
            value = str(self.environ.get(env_name, "")).strip()
            if not value:
                raise RemoteBackendConfigurationError(
                    f"remote ORS credential reference is missing: {env_name}"
                )
            if header.casefold() == "authorization" and not value.casefold().startswith(
                ("bearer ", "basic ")
            ):
                value = f"Bearer {value}"
            headers[header] = value
        return headers

    def _redact(self, value: Any) -> Any:
        secrets = [
            str(self.environ.get(env_name, "")).strip()
            for env_name in self.config.auth_header_envs.values()
        ]
        secrets = [item for item in secrets if item]
        safe = _safe_response_value(value)

        def visit(item: Any) -> Any:
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "<redacted>")
                return item
            if isinstance(item, Mapping):
                return {str(key): visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            return item

        return visit(safe)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        session_id: str | None = None,
        body: Mapping[str, Any] | None = None,
        sse: bool = False,
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url(),
                timeout=self.config.timeout_seconds,
                verify=self.config.verify_tls,
                trust_env=self.config.trust_env,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=self._headers(
                        session_id=session_id,
                        accept="text/event-stream" if sse else "application/json",
                    ),
                    json=dict(body) if body is not None else None,
                )
        except httpx.HTTPError as exc:
            raise RemoteBackendError(
                f"remote ORS request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RemoteBackendError(f"remote ORS returned HTTP {response.status_code}")
        if sse and "event-stream" in response.headers.get("content-type", ""):
            return self._redact(self._parse_sse(response.text))
        if not response.content:
            return {}
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RemoteBackendProtocolError("remote ORS returned non-JSON") from exc
        if not isinstance(payload, (Mapping, list)):
            raise RemoteBackendProtocolError(
                "remote ORS returned an unsupported JSON value"
            )
        return self._redact(dict(payload) if isinstance(payload, Mapping) else payload)

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any]:
        event = "message"
        data_lines: list[str] = []
        chunks: list[str] = []
        result: dict[str, Any] | None = None

        def flush() -> None:
            nonlocal event, data_lines, result
            if not data_lines:
                event = "message"
                return
            raw = "\n".join(data_lines)
            data_lines = []
            candidates: list[str] = []
            if event == "chunk":
                chunks.append(raw)
            elif event in {"end", "message"}:
                candidates.append(raw)
                if chunks:
                    candidates.append("".join(chunks) + raw)
            elif event == "error":
                raise RemoteBackendError("remote ORS SSE stream returned an error")
            for candidate in candidates:
                try:
                    decoded = json.loads(candidate)
                except (TypeError, ValueError):
                    continue
                if isinstance(decoded, Mapping):
                    result = dict(decoded)
                    break
            event = "message"

        for line in body.splitlines():
            if not line:
                flush()
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush()
        if result is None and chunks:
            try:
                decoded = json.loads("".join(chunks))
            except (TypeError, ValueError) as exc:
                raise RemoteBackendProtocolError(
                    "remote ORS SSE stream omitted a terminal result"
                ) from exc
            if isinstance(decoded, Mapping):
                result = dict(decoded)
        if result is None:
            raise RemoteBackendProtocolError(
                "remote ORS SSE stream omitted a terminal result"
            )
        return result

    @staticmethod
    def _session_id(state: Mapping[str, Any]) -> str:
        session_id = str(state.get("_ors_proxy_session_id") or "").strip()
        if not session_id:
            raise RemoteBackendProtocolError("remote ORS session is not initialized")
        return session_id

    async def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        del secrets
        session_id = str(uuid4())
        response = await self._request(
            "POST",
            "/create",
            session_id=session_id,
            body={
                "env_name": self.config.remote_environment,
                "task_spec": visible_task_value(task_spec),
            },
        )
        if not isinstance(response, Mapping) or str(response.get("sid") or "") != session_id:
            raise RemoteBackendProtocolError(
                "remote ORS create response omitted matching sid"
            )
        state["_ors_proxy_session_id"] = session_id

    async def get_prompt(
        self,
        *,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del task_spec
        response = await self._request(
            "GET",
            f"/{self.config.remote_environment}/prompt",
            session_id=self._session_id(state),
        )
        if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
            raise RemoteBackendProtocolError("remote ORS prompt is not an array")
        return [dict(item) for item in response if isinstance(item, Mapping)]

    async def list_tools(
        self,
        *,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> list[ToolSpec]:
        del task_spec
        raw_tools = state.get("_ors_proxy_tools")
        if raw_tools is None:
            response = await self._request(
                "GET",
                f"/{self.config.remote_environment}/task_tools",
                session_id=self._session_id(state),
            )
            if not isinstance(response, Mapping):
                raise RemoteBackendProtocolError(
                    "remote ORS tools response is not an object"
                )
            raw_tools = response.get("tools", [])
            if isinstance(raw_tools, Sequence) and not isinstance(raw_tools, (str, bytes)):
                state["_ors_proxy_tools"] = [
                    dict(item) for item in raw_tools if isinstance(item, Mapping)
                ]
                raw_tools = state["_ors_proxy_tools"]
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
            raise RemoteBackendProtocolError("remote ORS tools field is not an array")
        try:
            return [ToolSpec.model_validate(item) for item in raw_tools]
        except ValueError as exc:
            raise RemoteBackendProtocolError(
                "remote ORS returned an invalid tool specification"
            ) from exc

    async def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult:
        remote_task_id = _remote_task_id(task_spec)
        response = await self._request(
            "POST",
            f"/{self.config.remote_environment}/call",
            session_id=self._session_id(state),
            body={
                "name": action,
                "input": visible_task_value(payload),
            },
            sse=True,
        )
        if not isinstance(response, Mapping):
            raise RemoteBackendProtocolError("remote ORS call response is not an object")
        if response.get("ok") is not True:
            raise RemoteBackendError("remote ORS tool call failed")
        output = response.get("output")
        if not isinstance(output, Mapping):
            raise RemoteBackendProtocolError("remote ORS call response omitted output")
        normalized = RemoteServiceBackend._normalize_output(output, action=action)
        metadata = dict(normalized.metadata or {})
        metadata.setdefault("runtime", "openreward-proxy")
        metadata.setdefault("remote_environment", self.config.remote_environment)
        metadata.setdefault("remote_task_id", remote_task_id)
        source = _mapping(task_spec.get("source"))
        revision = str(source.get("revision") or "").strip()
        if revision:
            metadata.setdefault("source_revision", revision)
        if normalized.reward is not None:
            metadata.setdefault("reward_source", "remote-ors")
        normalized.metadata = metadata
        return normalized

    async def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec
        session_id = str(state.get("_ors_proxy_session_id") or "").strip()
        try:
            if session_id:
                await self._request("POST", "/delete", session_id=session_id)
        finally:
            state.pop("_ors_proxy_session_id", None)
            state.pop("_ors_proxy_tools", None)


class SweSandboxBackend(RemoteServiceBackend):
    """Named bridge for a disposable SWE sandbox service."""


class PptOfficeBackend(RemoteServiceBackend):
    """Named bridge for an Office/SlideForge-compatible PPT service."""


class ToolRegistryBackend(RemoteServiceBackend):
    """Named bridge for a resettable stateful ToolUse registry."""
