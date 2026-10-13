"""Direct AgentOS EntryPoint HTTP adapter for online application rollouts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from .client import (
    AgentOSClientError,
    AgentOSConfigurationError,
    AgentOSHTTPError,
    _response_message,
)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sign_workspace_jwt(secret: str, user_id: str, now: datetime | None = None) -> str:
    """Sign the short-lived workspace JWT used by ``empty_create``."""

    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "clientUserId": user_id,
        "iat": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exp": (issued_at + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    encoded_header = _base64url(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _base64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode('ascii')}.{_base64url(signature)}"


def _normalize_base_url(value: str, label: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise AgentOSConfigurationError(f"AgentOS {label} endpoint is not configured")
    return normalized


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested_terminal_state(value: Any) -> str | None:
    """Find the last AgentOS/A2A terminal state in a decoded event."""

    if isinstance(value, dict):
        state = value.get("agent_state") or value.get("agentState")
        if isinstance(state, str) and state:
            return state
        status = value.get("status")
        if isinstance(status, dict):
            status_state = status.get("state")
            if isinstance(status_state, str) and status_state:
                return status_state
        for nested in reversed(list(value.values())):
            found = _nested_terminal_state(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in reversed(value):
            found = _nested_terminal_state(nested)
            if found:
                return found
    return None


def _iter_sse_json(
    response: httpx.Response,
    *,
    deadline: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Decode JSON data fields from an SSE response, including double prefixes."""

    data_lines: list[str] = []

    def flush() -> Iterator[dict[str, Any]]:
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        while raw.startswith("data:"):
            raw = raw[5:].strip()
        if not raw or raw.lower() == "[done]":
            return
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentOSClientError("AgentOS EntryPoint returned invalid SSE JSON") from exc
        if isinstance(decoded, dict):
            yield decoded

    for line in response.iter_lines():
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not line:
            yield from flush()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    yield from flush()


@dataclass(frozen=True)
class AgentOSApplication:
    user_id: str
    app_id: str
    conversation_id: str


@dataclass(frozen=True)
class AgentOSStreamResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    terminal_state: str | None = None


class AgentOSRolloutPhaseError(AgentOSClientError):
    """A rollout phase reached a terminal state other than the accepted states."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        application: AgentOSApplication,
        result: AgentOSStreamResult,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.application = application
        self.result = result


@dataclass(frozen=True)
class AgentOSFirstCodingRollout:
    application: AgentOSApplication
    prd: AgentOSStreamResult
    coding: AgentOSStreamResult


@dataclass(frozen=True)
class AgentOSEndToEndRollout:
    application: AgentOSApplication
    requirement: AgentOSStreamResult
    coding: AgentOSStreamResult


class AgentOSEntrypointClient:
    """Run one Miaoda application directly through workspace-be and EntryPoint.

    The workspace JWT secret is resolved from an environment variable for each
    application creation. It is never retained in the client or returned in an
    artifact. Datawheel credentials are not involved in this single-rollout path.
    """

    provider = "agentos/entrypoint"

    def __init__(
        self,
        *,
        workspace_base_url: str,
        entrypoint_base_url: str,
        user_id: str,
        jwt_secret_env: str = "AGENTOS_WORKSPACE_JWT_KEY",
        timeout_seconds: float = 600.0,
        verify: bool = True,
        trust_env: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.workspace_base_url = _normalize_base_url(workspace_base_url, "workspace")
        self.entrypoint_base_url = _normalize_base_url(entrypoint_base_url, "EntryPoint")
        self.user_id = user_id.strip()
        if not self.user_id:
            raise AgentOSConfigurationError("AgentOS user_id is not configured")
        self.jwt_secret_env = jwt_secret_env.strip() or "AGENTOS_WORKSPACE_JWT_KEY"
        self.timeout_seconds = timeout_seconds
        self.verify = verify
        self.trust_env = trust_env
        self.transport = transport

    def _jwt_secret(self) -> str:
        secret = os.environ.get(self.jwt_secret_env, "").strip()
        if not secret:
            raise AgentOSConfigurationError(
                "AgentOS workspace JWT secret is unavailable; configure environment variable "
                f"{self.jwt_secret_env}"
            )
        return secret

    def _client(self, *, base_url: str | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=base_url or self.workspace_base_url,
            timeout=self.timeout_seconds,
            verify=self.verify,
            trust_env=self.trust_env,
            transport=self.transport,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 200 or response.status_code >= 300:
            raise AgentOSHTTPError(response.status_code, _response_message(response))

    def create_application(self) -> AgentOSApplication:
        jwt_token = _sign_workspace_jwt(self._jwt_secret(), self.user_id)
        try:
            with self._client() as client:
                response = client.post(
                    "/innerapi/v1/conversation/empty_create",
                    params={"whitApp": "true"},
                    headers={
                        "Content-Type": "application/json",
                        "X-Authorization": f"Bearer {jwt_token}",
                    },
                )
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS application creation failed: {type(exc).__name__}"
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise AgentOSClientError("AgentOS application creation returned non-JSON") from exc
        body = _mapping(payload)
        if body.get("status") not in (0, "0", None):
            raise AgentOSClientError("AgentOS application creation returned a business error")
        data = _mapping(body.get("data"))
        app_id = str(data.get("appId") or data.get("app_id") or "").strip()
        conversation_id = str(
            data.get("conversationId") or data.get("conversation_id") or ""
        ).strip()
        if not app_id or not conversation_id:
            raise AgentOSClientError("AgentOS application creation omitted app or conversation id")
        return AgentOSApplication(
            user_id=str(data.get("createdBy") or self.user_id),
            app_id=app_id,
            conversation_id=conversation_id,
        )

    def get_agent_url(self) -> str:
        try:
            with self._client(base_url=self.entrypoint_base_url) as client:
                response = client.get("/.well-known/agent.json")
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS agent-card request failed: {type(exc).__name__}"
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise AgentOSClientError("AgentOS agent card returned non-JSON") from exc
        agent_url = _mapping(payload).get("url")
        if not isinstance(agent_url, str) or not agent_url.strip():
            raise AgentOSClientError("AgentOS agent card omitted its A2A url")
        return agent_url.strip()

    def stream_message(
        self,
        application: AgentOSApplication,
        *,
        parts: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> AgentOSStreamResult:
        message_id = str(uuid4())
        request_body = {
            "id": message_id,
            "jsonrpc": "2.0",
            "method": "message/stream",
            "params": {
                "configuration": {"acceptedOutputModes": ["text/plain", "video/mp4"]},
                "message": {
                    "kind": "message",
                    "messageId": message_id,
                    "metadata": dict(metadata),
                    "parts": [dict(part) for part in parts],
                    "role": "user",
                },
            },
        }
        request_id = uuid4().hex
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "Trace": request_id,
            "X-Miaoda-Session-Id": application.conversation_id,
            "X-Miaoda-User-Id": application.user_id,
            "X-Miaoda-App-Id": application.app_id,
            "X-Miaoda-A2A-Standard-Mode": "true",
        }
        try:
            with self._client() as client, client.stream(
                "POST",
                self.get_agent_url(),
                headers=headers,
                json=request_body,
            ) as response:
                self._raise_for_status(response)
                events = []
                terminal = None
                for event in _iter_sse_json(response):
                    events.append(event)
                    terminal = _nested_terminal_state(event)
                    result = _mapping(event.get("result"))
                    if result.get("final") is True or terminal in {
                        "awaiting_user_input",
                        "completed",
                        "error",
                        "failed",
                        "finished",
                    }:
                        break
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS EntryPoint stream failed: {type(exc).__name__}"
            ) from exc
        if terminal is None:
            for event in reversed(events):
                terminal = _nested_terminal_state(event)
                if terminal:
                    break
        return AgentOSStreamResult(events=events, terminal_state=terminal)

    def _post_chat_message(
        self,
        application: AgentOSApplication,
        *,
        token: str,
        parts: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        include_app_id: bool,
    ) -> str:
        request_id = uuid4().hex
        message_id = str(uuid4())
        base_metadata = dict(metadata)
        if include_app_id:
            base_metadata["appId"] = application.app_id
        payload = {
            "jsonrpc": "2.0",
            "method": "message/send",
            "id": request_id,
            "params": {
                "message": {
                    "parts": [dict(part) for part in parts],
                    "kind": "message",
                    "messageId": message_id,
                    "role": "user",
                    "contextId": application.conversation_id,
                    "metadata": {},
                    "taskId": "",
                    "lang": "zh",
                },
                "metadata": base_metadata,
            },
        }
        try:
            with self._client() as client:
                response = client.post(
                    "/api/v1/conversation/chat",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "X-Request-Id": request_id,
                        "Trace": request_id,
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS chat submission failed: {type(exc).__name__}"
            ) from exc
        self._raise_for_status(response)
        try:
            body = _mapping(response.json())
        except (TypeError, ValueError) as exc:
            raise AgentOSClientError("AgentOS chat submission returned non-JSON") from exc
        result = _mapping(body.get("result"))
        status = _mapping(result.get("status"))
        if str(status.get("state") or "").lower() == "failed":
            raise AgentOSClientError("AgentOS chat submission returned a failed state")
        return request_id

    def _stream_chat_trajectory(
        self,
        application: AgentOSApplication,
        *,
        token: str,
        request_id: str,
    ) -> AgentOSStreamResult:
        try:
            with self._client() as client, client.stream(
                "GET",
                "/api/v1/conversation/trajectory",
                params={"appId": application.app_id},
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {token}",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Request-Id": request_id,
                    "Trace": request_id,
                },
            ) as response:
                self._raise_for_status(response)
                events: list[dict[str, Any]] = []
                terminal: str | None = None
                for event in _iter_sse_json(
                    response,
                    deadline=time.monotonic() + self.timeout_seconds,
                ):
                    events.append(event)
                    result = _mapping(event.get("result"))
                    result_metadata = _mapping(result.get("metadata"))
                    if result_metadata.get("isHistoryMessage"):
                        continue
                    agent_state = str(
                        result_metadata.get("agent_state")
                        or result_metadata.get("agentState")
                        or ""
                    ).strip()
                    status_state = str(
                        _mapping(result.get("status")).get("state") or ""
                    ).strip()
                    if agent_state in {
                        "awaiting_user_input",
                        "error",
                        "finished",
                        "rejected",
                        "stopped",
                    }:
                        terminal = agent_state
                        break
                    if status_state in {
                        "completed",
                        "failed",
                        "input-required",
                        "input_required",
                    }:
                        terminal = status_state
                        break
                    if result.get("final") is True:
                        terminal = agent_state or status_state or "completed"
                        break
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS chat trajectory failed: {type(exc).__name__}"
            ) from exc
        if terminal is None:
            for event in reversed(events):
                terminal = _nested_terminal_state(event)
                if terminal:
                    break
        return AgentOSStreamResult(events=events, terminal_state=terminal)

    def chat_round(
        self,
        application: AgentOSApplication,
        *,
        token: str,
        parts: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
        include_app_id: bool,
    ) -> AgentOSStreamResult:
        request_id = self._post_chat_message(
            application,
            token=token,
            parts=parts,
            metadata=metadata,
            include_app_id=include_app_id,
        )
        return self._stream_chat_trajectory(
            application,
            token=token,
            request_id=request_id,
        )

    def end_to_end_rollout(
        self,
        query: str,
        *,
        coding_config: Mapping[str, Any],
        skill_ids: Sequence[str] = (),
        query_mode: str = "deep_mode",
        default_agent: str = "AdaProAgent",
    ) -> AgentOSEndToEndRollout:
        requirement_text = query.strip()
        if not requirement_text:
            raise ValueError("query must not be empty")
        application = self.create_application()
        token = self._mock_login_token()
        selected_skills = [str(item).strip() for item in skill_ids if str(item).strip()]
        base_metadata: dict[str, Any] = {
            "defaultAgent": default_agent,
            "agentConfig": {},
            "runtime": "miaoda",
        }
        if selected_skills:
            base_metadata["skillList"] = selected_skills
        requirement = self.chat_round(
            application,
            token=token,
            parts=[{"kind": "text", "text": requirement_text}],
            metadata=base_metadata,
            include_app_id=False,
        )
        if requirement.terminal_state not in {
            "awaiting_user_input",
            "completed",
            "finished",
            "input-required",
            "input_required",
            "stopped",
        }:
            raise AgentOSRolloutPhaseError(
                "AgentOS raw-query phase did not reach a valid terminal state: "
                f"{requirement.terminal_state}",
                phase="requirement",
                application=application,
                result=requirement,
            )

        coding_metadata = {
            **base_metadata,
            "userConfirmation": {"type": "generateApp"},
            "queryMode": query_mode,
            "codingConfig": dict(coding_config),
        }
        coding = self.chat_round(
            application,
            token=token,
            parts=[{"kind": "text", "text": "立即创作"}],
            metadata=coding_metadata,
            include_app_id=True,
        )
        if coding.terminal_state not in {"completed", "finished", "stopped"}:
            raise AgentOSRolloutPhaseError(
                f"AgentOS E2E coding phase did not finish: {coding.terminal_state}",
                phase="coding",
                application=application,
                result=coding,
            )
        return AgentOSEndToEndRollout(
            application=application,
            requirement=requirement,
            coding=coding,
        )

    def first_coding_rollout(
        self,
        query: str,
        *,
        coding_config: Mapping[str, Any],
        prd: str | None = None,
        replay_url: str | None = None,
        skill_ids: Sequence[str] = (),
        state_input: Mapping[str, Any] | None = None,
        query_mode: str = "deep_mode",
    ) -> AgentOSFirstCodingRollout:
        requirement = query.strip()
        if not requirement:
            raise ValueError("query must not be empty")
        fixed_prd = (prd or requirement).strip()
        if not fixed_prd:
            raise ValueError("prd must not be empty")
        application = self.create_application()
        replay = (replay_url or "").strip()
        if replay:
            # Match AgenticFirstCodingPipeline's replay-url lane exactly. The
            # signed URL is request-scoped and must not be persisted by callers.
            state = {"query": requirement, "replay_url": replay}
            state.update(dict(state_input or {}))
        else:
            state = {
                "app_type": "Web",
                "behavior": "generate_app",
                "has_code_template": "NONE",
                "has_research": False,
                "is_generated": False,
                "lang_env": "zh-CN",
                "lang_user": "Chinese",
                "prd": fixed_prd,
                "query": requirement,
                "task": fixed_prd,
                **dict(state_input or {}),
            }
        selected_skills = [str(item).strip() for item in skill_ids if str(item).strip()]
        first_text = (
            "[IMPORTANT] You MUST call PRDAgent to generate PRD before proceeding with any app "
            "generation.\n\n--------------------------------\n# User Requirement\n"
            f"{requirement}\n--------------------------------"
        )
        common: dict[str, Any] = {
            "runtime": "miaoda",
            "defaultAgent": "AdaProCodeactAgent",
            "is_user_input": True,
            "appStatus": "not_generate",
            "hasAgent": False,
            "cur_version_last_event_id": -1,
            "supportSupabase": "true",
            "codingConfig": dict(coding_config),
        }
        if selected_skills:
            common["skillList"] = selected_skills
        prd_metadata = {
            **common,
            "prd_replay_for_test": True,
            "StateInput": state,
            "firstMessage": first_text,
        }
        prd = self.stream_message(
            application,
            parts=[{"kind": "text", "metadata": {}, "text": first_text}],
            metadata=prd_metadata,
        )
        if prd.terminal_state not in {"awaiting_user_input", "finished", "completed"}:
            raise AgentOSRolloutPhaseError(
                f"AgentOS PRD phase did not reach a valid terminal state: {prd.terminal_state}",
                phase="prd",
                application=application,
                result=prd,
            )

        coding_metadata = {
            **common,
            "userConfirmation": {"type": "generateApp"},
            "subscription_plan": "free",
            "queryMode": query_mode,
        }
        coding = self.stream_message(
            application,
            parts=[
                {"kind": "text", "text": "立即创作"},
                {
                    "kind": "data",
                    "data": {
                        "type": "action",
                        "actions": [
                            {
                                "type": "button",
                                "label": "立即创作",
                                "value": "立即创作",
                                "event": {"name": "generateApp"},
                            }
                        ],
                    },
                },
            ],
            metadata=coding_metadata,
        )
        if coding.terminal_state not in {"finished", "completed"}:
            raise AgentOSRolloutPhaseError(
                f"AgentOS coding phase did not finish: {coding.terminal_state}",
                phase="coding",
                application=application,
                result=coding,
            )
        return AgentOSFirstCodingRollout(application=application, prd=prd, coding=coding)

    def _mock_login_token(self) -> str:
        try:
            with self._client() as client:
                response = client.post(
                    "/wechatminiprogramapi/v1/access/bduss/login/mock",
                    params={"userId": self.user_id},
                    headers={"Content-Type": "application/json"},
                    json={},
                )
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS mock login failed: {type(exc).__name__}"
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise AgentOSClientError("AgentOS mock login returned non-JSON") from exc
        token = _mapping(_mapping(payload).get("data")).get("token")
        if not isinstance(token, str) or not token.strip():
            raise AgentOSClientError("AgentOS mock login omitted its token")
        return token.strip()

    def get_conversation_trajectory(
        self,
        app_id: str,
        *,
        last_event_id: int = 0,
        max_wait_seconds: float = 60.0,
    ) -> AgentOSStreamResult:
        identifier = app_id.strip()
        if not identifier:
            raise ValueError("app_id must not be empty")
        token = self._mock_login_token()
        bounded_wait = max(1.0, min(max_wait_seconds, self.timeout_seconds))
        try:
            with httpx.Client(
                base_url=self.workspace_base_url,
                timeout=httpx.Timeout(self.timeout_seconds, read=bounded_wait),
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client, client.stream(
                "GET",
                "/api/v1/conversation/trajectory",
                params={"appId": identifier, "lastEventId": last_event_id},
                headers={
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {token}",
                    "Cache-Control": "no-cache",
                },
            ) as response:
                self._raise_for_status(response)
                events = list(
                    _iter_sse_json(
                        response,
                        deadline=time.monotonic() + bounded_wait,
                    )
                )
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS trajectory request failed: {type(exc).__name__}"
            ) from exc
        terminal = None
        for event in reversed(events):
            terminal = _nested_terminal_state(event)
            if terminal:
                break
        return AgentOSStreamResult(events=events, terminal_state=terminal)
