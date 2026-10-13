"""Optional real-service backends for the ORS domain adapters.

The generic adapters do not assume any internal service.  This module contains
the first concrete bridge for the HTTP AgentOS client already present in this
repository.  It is intentionally opt-in: constructing it requires an
explicitly configured ``AgentOSEntrypointClient`` and therefore cannot affect
local mock tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from research_ops.agentos.entrypoint import (
    AgentOSEntrypointClient,
    AgentOSStreamResult,
)

from .adapters import BackendResult
from .types import JSONObject, TextBlock

_TERMINAL_STATES = {
    "completed",
    "finished",
    "error",
    "failed",
    "stopped",
    "rejected",
}


def _safe_visible_text(events: Sequence[Mapping[str, Any]], *, limit: int = 4_000) -> str:
    """Extract bounded visible strings without copying credential-like fields."""

    ignored = {
        "token",
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "signed_url",
        "replay_url",
    }
    values: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if sum(len(item) for item in values) >= limit:
            return
        if key.casefold() in ignored:
            return
        if isinstance(value, str):
            text = value.strip()
            if text:
                values.append(text)
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child, key)

    for event in events:
        visit(event)
        if sum(len(item) for item in values) >= limit:
            break
    joined = "\n".join(values)
    return joined[:limit]


def _stream_result_output(result: AgentOSStreamResult, *, finished: bool) -> BackendResult:
    text = _safe_visible_text(result.events)
    if not text:
        text = f"AgentOS state: {result.terminal_state or 'unknown'}"
    return BackendResult(
        blocks=[TextBlock(text=text)],
        reward=None,
        finished=finished,
        metadata={
            "provider": "agentos/entrypoint",
            "terminal_state": result.terminal_state,
            "event_count": len(result.events),
        },
    )


class MiaodaAgentOSBackend:
    """Bridge an existing HTTP AgentOS EntryPoint client into ``miaoda_step``.

    ``whole_rollout=True`` is a migration fallback for the current two-phase
    AgentOS API: the first ORS action launches one complete E2E rollout and the
    episode terminates.  It is useful to validate task plumbing, but it is not
    a per-turn online MOPD environment.

    The default continuation mode creates one AgentOS application per ORS
    session and sends each ``miaoda_step``/``submit`` action through
    ``chat_round``.  The short-lived login token remains in process memory only
    and is deleted during teardown; it is never put in ORS metadata.
    """

    def __init__(
        self,
        client: AgentOSEntrypointClient,
        *,
        coding_config: Mapping[str, Any] | None = None,
        skill_ids: Sequence[str] = (),
        query_mode: str = "deep_mode",
        default_agent: str = "AdaProAgent",
        whole_rollout: bool = False,
    ) -> None:
        self.client = client
        self.coding_config = dict(coding_config or {})
        self.skill_ids = tuple(str(item).strip() for item in skill_ids if str(item).strip())
        self.query_mode = query_mode
        self.default_agent = default_agent
        self.whole_rollout = whole_rollout

    async def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        del task_spec, secrets
        if self.whole_rollout:
            return
        application = await asyncio.to_thread(self.client.create_application)
        # This method resolves the credential from the client's configured
        # runtime endpoint.  Keep it only in the session-local state.
        token = await asyncio.to_thread(self.client._mock_login_token)
        state["_miaoda_application"] = application
        state["_miaoda_token"] = token

    async def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult:
        query = str(task_spec.get("query") or task_spec.get("prompt") or "").strip()
        if not query:
            raise ValueError("Miaoda task requires query")

        if self.whole_rollout:
            if state.get("_miaoda_finished"):
                return BackendResult(
                    blocks=[TextBlock(text="Miaoda rollout already finished")],
                    finished=True,
                    metadata={"provider": "agentos/entrypoint", "mode": "whole_rollout"},
                )
            rollout = await asyncio.to_thread(
                self.client.end_to_end_rollout,
                query,
                coding_config=self.coding_config,
                skill_ids=self.skill_ids,
                query_mode=self.query_mode,
                default_agent=self.default_agent,
            )
            state["_miaoda_finished"] = True
            return _stream_result_output(
                rollout.coding,
                finished=True,
            )

        application = state.get("_miaoda_application")
        token = state.get("_miaoda_token")
        if application is None or not isinstance(token, str) or not token:
            raise RuntimeError("Miaoda continuation session is not initialized")
        if action == "submit":
            message = str(payload.get("result") or "立即创作").strip() or "立即创作"
        else:
            message = str(payload.get("message") or "").strip()
            if not message:
                raise ValueError("miaoda_step requires a non-empty message")
        metadata: dict[str, Any] = {
            "runtime": "miaoda",
            "defaultAgent": self.default_agent,
            "queryMode": self.query_mode,
        }
        if self.skill_ids:
            metadata["skillList"] = list(self.skill_ids)
        raw_metadata = payload.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata["actionMetadata"] = dict(raw_metadata)
        result = await asyncio.to_thread(
            self.client.chat_round,
            application,
            token=token,
            parts=[{"kind": "text", "text": message}],
            metadata=metadata,
            include_app_id=True,
        )
        finished = result.terminal_state in _TERMINAL_STATES
        if action == "submit" and result.terminal_state is None:
            finished = True
        return _stream_result_output(result, finished=finished)

    async def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec
        state.pop("_miaoda_token", None)
        state.pop("_miaoda_application", None)


class CallbackBackend:
    """Adapt a user-supplied callable to the common backend contract.

    This is useful for internal SWE/PPT/ToolUse services whose SDK or RPC
    client is not part of this repository yet.  The callback receives exactly
    the same keyword arguments as :meth:`EnvironmentBackend.execute`.
    """

    def __init__(self, callback: Any) -> None:
        if not callable(callback):
            raise TypeError("callback backend requires a callable")
        self.callback = callback

    def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> Any:
        del task_spec, secrets, state

    def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> Any:
        return self.callback(
            action=action,
            payload=payload,
            task_spec=task_spec,
            state=state,
        )

    def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec, state
