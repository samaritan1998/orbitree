from __future__ import annotations

import json
from typing import Any

from research_ops.settings import Settings

from .app_server import AppServerCodexAdapter
from .base import CodexAdapter, CodexEventHandler, CodexRequestHandler
from .cli import CliCodexAdapter
from .identity import CodexIdentityService


class IdentityCodexPool:
    """Each frozen identity/model selection owns its own App Server and credentials."""

    def __init__(
        self,
        settings: Settings,
        *,
        event_handler: CodexEventHandler,
        request_handler: CodexRequestHandler,
    ) -> None:
        self.settings = settings
        self.identities = CodexIdentityService(settings)
        self.event_handler = event_handler
        self.request_handler = request_handler
        self._adapters: dict[str, CodexAdapter] = {}
        self._requests: dict[str, tuple[CodexAdapter, str]] = {}

    def for_execution(self, execution: dict[str, Any]) -> CodexAdapter:
        key = json.dumps(execution, sort_keys=True)
        if key not in self._adapters:
            settings = self.settings.model_copy(
                update={
                    "codex_model": execution["model"],
                    "codex_reasoning_effort": execution["reasoning_effort"],
                    "codex_model_provider": execution["provider"],
                }
            )
            adapter_type = (
                AppServerCodexAdapter if settings.codex_backend == "app-server" else CliCodexAdapter
            )
            namespace = str(len(self._adapters))

            async def handle_request(request_id: str, method: str, params: dict[str, Any]) -> None:
                routed_id = f"identity-{namespace}:{request_id}"
                self._requests[routed_id] = (self._adapters[key], request_id)
                await self.request_handler(routed_id, method, params)

            self._adapters[key] = adapter_type(
                settings,
                event_handler=self.event_handler,
                request_handler=handle_request,
                launch=self.identities.launch(execution),
            )
        return self._adapters[key]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.for_execution(self.identities.snapshot()), name)

    async def start(self) -> None:
        # Start lazily: displaying the workspace must not require either login.
        pass

    async def stop(self) -> None:
        for adapter in tuple(self._adapters.values()):
            await adapter.stop()
        self._adapters.clear()
        self._requests.clear()

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        adapter, original_id = self._requests.pop(request_id)
        await adapter.respond(original_id, result)


def adapter_for_execution(adapter: CodexAdapter, execution: dict[str, Any]) -> CodexAdapter:
    select = getattr(adapter, "for_execution", None)
    return select(execution) if callable(select) and execution else adapter
