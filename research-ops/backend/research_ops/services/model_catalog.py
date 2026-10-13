from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from research_ops.codex.app_server import AppServerCodexAdapter
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.identity import CodexIdentityService, ExecutionPreferences
from research_ops.settings import Settings

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


async def _ignore_event(*_args: Any) -> None:
    pass


def _file_version(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return None


class CodexModelCatalog:
    """Read-only, identity-isolated model discovery without loading a thread."""

    ttl_seconds = 300

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.identities = CodexIdentityService(settings)
        self._cache: dict[str, tuple[str, float, dict[str, Any]]] = {}
        self._locks = {identity: asyncio.Lock() for identity in ("api", "chatgpt", "oneapi")}

    def _selection(self, identity: str) -> tuple[dict[str, Any], str]:
        ExecutionPreferences(identity=identity)
        self.identities.ensure_ready(identity)
        # The model only seeds the process config. Discovery never starts a turn
        # and does not save this model as a Branch preference.
        execution = self.identities.snapshot(
            {
                "identity": identity,
                "model": self.settings.oneapi_model
                if identity == "oneapi"
                else self.settings.codex_model or "gpt-6-astra",
            }
        )
        home = self.identities.personal_home if identity == "chatgpt" else self.settings.codex_home
        version = json.dumps(
            [
                execution,
                _file_version(home / "auth.json"),
                _file_version(home / "config.toml"),
                _file_version(self.identities.api_auth_path) if identity == "api" else None,
                hashlib.sha256((self.settings.oneapi_api_key or "").encode()).hexdigest()
                if identity == "oneapi"
                else None,
            ],
            sort_keys=True,
        )
        return execution, version

    async def list_models(self, identity: str, *, refresh: bool = False) -> dict[str, Any]:
        execution, version = await asyncio.to_thread(self._selection, identity)
        previous = self._cache.get(identity)
        async with self._locks[identity]:
            cached = self._cache.get(identity)
            if (
                cached
                and cached[0] == version
                and time.monotonic() - cached[1] < self.ttl_seconds
                and (not refresh or cached is not previous)
            ):
                return copy.deepcopy(cached[2])
            rows = await self._fetch(execution)
            result = {
                "identity": identity,
                "models": rows,
                "allow_custom": identity in {"api", "oneapi"},
            }
            self._cache[identity] = (version, time.monotonic(), result)
            return copy.deepcopy(result)

    async def _fetch(self, execution: dict[str, Any]) -> list[dict[str, Any]]:
        if execution["identity"] == "oneapi":
            return await self._fetch_oneapi(execution)
        settings = self.settings.model_copy(update={"codex_request_timeout_seconds": 15})
        adapter = AppServerCodexAdapter(
            settings,
            event_handler=_ignore_event,
            request_handler=_ignore_event,
            launch=self.identities.launch(execution),
        )
        models: dict[str, dict[str, Any]] = {}
        try:
            async with asyncio.timeout(25):
                await adapter.start()
                cursor = None
                seen: set[str] = set()
                for _ in range(10):
                    page = await adapter.list_models(cursor=cursor)
                    for row in page.get("data", []):
                        if not isinstance(row, dict) or row.get("hidden"):
                            continue
                        model = row.get("model")
                        if not isinstance(model, str) or not model.strip() or len(model) > 160:
                            continue
                        model = model.strip()
                        raw_efforts = row.get("supportedReasoningEfforts")
                        if not isinstance(raw_efforts, list):
                            raw_efforts = []
                        efforts = list(
                            dict.fromkeys(
                                entry["reasoningEffort"]
                                for entry in raw_efforts
                                if isinstance(entry, dict)
                                and isinstance(entry.get("reasoningEffort"), str)
                                and entry.get("reasoningEffort") in EFFORTS
                            )
                        )
                        default = row.get("defaultReasoningEffort")
                        name = row.get("displayName")
                        models[model] = {
                            "id": model,
                            "name": name.strip()[:160]
                            if isinstance(name, str) and name.strip()
                            else model,
                            "reasoning_efforts": efforts,
                            "default_reasoning_effort": default if default in efforts else None,
                            "is_default": row.get("isDefault") is True,
                        }
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise ValueError("Invalid model catalog pagination")
                    seen.add(cursor)
                else:
                    raise ValueError("Model catalog pagination limit exceeded")
            if not models:
                raise ValueError("No visible models returned")
            return list(models.values())
        except (TimeoutError, CodexUnavailable, TypeError, ValueError, OSError) as exc:
            # Provider errors can contain headers or auth details; keep them out
            # of the HTTP response and preserve the original exception locally.
            raise CodexUnavailable(
                "Unable to load models for this identity. Check login and network, then retry."
            ) from exc
        finally:
            await adapter.stop()

    async def _fetch_oneapi(self, execution: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            async with (
                httpx.AsyncClient(
                    timeout=15,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "GET",
                    f"{execution['base_url']}/models",
                    headers={"Authorization": f"Bearer {self.settings.oneapi_api_key}"},
                ) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("Model catalog request failed")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise ValueError("Model catalog exceeds size limit")
            payload = json.loads(content)
            models: dict[str, dict[str, Any]] = {}
            for row in payload.get("data", []):
                model = row.get("id") if isinstance(row, dict) else None
                if not isinstance(model, str) or not model.strip() or len(model) > 160:
                    continue
                model = model.strip()
                models[model] = {
                    "id": model,
                    "name": model,
                    # /models identifies available models, not their reasoning capabilities.
                    "reasoning_efforts": [],
                    "default_reasoning_effort": None,
                    "is_default": model == self.settings.oneapi_model,
                }
            if not models:
                raise ValueError("No models returned")
            return sorted(models.values(), key=lambda row: (not row["is_default"], row["id"]))
        except (httpx.HTTPError, TypeError, ValueError, AttributeError) as exc:
            raise CodexUnavailable(
                "Unable to load OneAPI models. Check credentials and network, then retry."
            ) from exc
