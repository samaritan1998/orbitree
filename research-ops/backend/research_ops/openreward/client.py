"""Dependency-light ORS HTTP client used by local smoke tests and adapters."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import httpx


class ORSClientError(RuntimeError):
    """Raised when an ORS request or SSE tool call fails."""


def _safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = response.text
    text = str(body)
    for key in ("authorization", "token", "api_key", "apikey"):
        # Error payloads should not normally contain these, but avoid echoing
        # a configured value if a proxy reflects request headers.
        value = response.request.headers.get(key) or response.request.headers.get(
            key.title()
        )
        if value:
            text = text.replace(value, "<redacted>")
    return text[:1000]


class ORSClient:
    """Synchronous client for the documented ORS HTTP/SSE contract."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 120.0,
        token_env: str | None = None,
        verify: bool = True,
        trust_env: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = str(base_url).strip().rstrip("/")
        if not normalized:
            raise ValueError("ORS base_url must not be empty")
        self.base_url = normalized
        self.timeout_seconds = timeout_seconds
        self.token_env = str(token_env or "").strip() or None
        self.verify = verify
        self.trust_env = trust_env
        self.transport = transport

    def _headers(self, *, sid: str | None = None, accept: str | None = None) -> dict[str, str]:
        headers = {"Accept": accept or "application/json"}
        if sid:
            headers["X-Session-ID"] = sid
        if self.token_env:
            token = os.environ.get(self.token_env, "").strip()
            if not token:
                raise ORSClientError(
                    "ORS credential is unavailable; configure environment variable "
                    f"{self.token_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        sid: str | None = None,
        json_body: Any = None,
        accept: str | None = None,
    ) -> Any:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method,
                    path,
                    headers=self._headers(sid=sid, accept=accept),
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise ORSClientError(f"ORS request failed: {type(exc).__name__}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ORSClientError(
                f"ORS request returned HTTP {response.status_code}: {_safe_error(response)}"
            )
        if accept == "text/event-stream":
            return response
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise ORSClientError("ORS returned a non-JSON response") from exc

    def health(self) -> dict[str, Any]:
        return dict(self._request("GET", "/health"))

    def list_environments(self) -> list[str]:
        return [str(item) for item in self._request("GET", "/list_environments")]

    def list_tools(self, env_name: str, *, sid: str | None = None) -> list[dict[str, Any]]:
        path = f"/{env_name}/task_tools" if sid else f"/{env_name}/tools"
        body = self._request("GET", path, sid=sid)
        return [dict(item) for item in body.get("tools", [])]

    def list_splits(self, env_name: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._request("GET", f"/{env_name}/splits")]

    def list_tasks(self, env_name: str, split: str) -> list[dict[str, Any]]:
        body = self._request(
            "POST",
            f"/{env_name}/tasks",
            json_body={"split": split},
        )
        return [dict(item) for item in body.get("tasks", [])]

    def num_tasks(self, env_name: str, split: str) -> int:
        body = self._request(
            "POST",
            f"/{env_name}/num_tasks",
            json_body={"split": split},
        )
        return int(body.get("num_tasks", 0))

    def get_task(self, env_name: str, split: str, index: int) -> dict[str, Any]:
        body = self._request(
            "POST",
            f"/{env_name}/task",
            json_body={"split": split, "index": index},
        )
        return dict(body["task"])

    def get_task_range(
        self,
        env_name: str,
        split: str,
        *,
        start: int | None = None,
        stop: int | None = None,
    ) -> list[dict[str, Any]]:
        body = self._request(
            "POST",
            f"/{env_name}/task_range",
            json_body={"split": split, "start": start, "stop": stop},
        )
        return [dict(item) for item in body.get("tasks", [])]

    def create_session(self) -> str:
        return str(self._request("POST", "/create_session")["sid"])

    def create(
        self,
        sid: str,
        *,
        env_name: str,
        task_spec: Mapping[str, Any] | None = None,
        split: str | None = None,
        index: int | None = None,
        secrets: Mapping[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"env_name": env_name, "secrets": dict(secrets or {})}
        if task_spec is not None:
            body["task_spec"] = dict(task_spec)
        else:
            if split is None or index is None:
                raise ValueError("create requires task_spec or split and index")
            body.update({"split": split, "index": int(index)})
        return str(self._request("POST", "/create", sid=sid, json_body=body)["sid"])

    def prompt(self, env_name: str, sid: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"/{env_name}/prompt", sid=sid))

    def ping(self, sid: str) -> dict[str, Any]:
        return dict(self._request("POST", "/ping", sid=sid))

    def delete(self, sid: str) -> str:
        return str(self._request("POST", "/delete", sid=sid)["sid"])

    def delete_session(self, sid: str) -> str:
        return str(self._request("POST", "/delete_session", sid=sid)["sid"])

    def call(
        self,
        env_name: str,
        sid: str,
        name: str,
        input: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Call a tool and reconstruct the ORS SSE response."""

        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.post(
                    f"/{env_name}/call",
                    headers=self._headers(sid=sid, accept="text/event-stream"),
                    json={"name": name, "input": dict(input or {}), "task_id": task_id},
                )
                if response.status_code < 200 or response.status_code >= 300:
                    raise ORSClientError(
                        "ORS tool call returned HTTP "
                        f"{response.status_code}: {_safe_error(response)}"
                    )
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and "event-stream" not in content_type:
                    return dict(response.json())
                return self._parse_sse(response.iter_lines())
        except ORSClientError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ORSClientError(f"ORS tool call failed: {type(exc).__name__}") from exc

    @staticmethod
    def _parse_sse(lines: Any) -> dict[str, Any]:
        current_event = "message"
        data_lines: list[str] = []
        task_id: str | None = None
        chunks: list[str] = []
        result: dict[str, Any] | None = None

        def flush() -> None:
            nonlocal current_event, data_lines, task_id, result
            if not data_lines:
                current_event = "message"
                return
            raw = "\n".join(data_lines)
            data_lines = []
            if current_event == "task_id":
                with suppress(ValueError, TypeError):
                    decoded = json.loads(raw)
                    task_id = str(decoded)
                if task_id is None:
                    task_id = raw
            elif current_event in {"chunk", "end"}:
                if current_event == "chunk":
                    chunks.append(raw)
                else:
                    if chunks:
                        chunks.append(raw)
                        raw = "".join(chunks)
                    try:
                        result = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise ORSClientError("ORS returned invalid SSE JSON") from exc
            elif current_event == "error":
                raise ORSClientError(raw)
            current_event = "message"

        for line in lines:
            if line == "":
                flush()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        flush()
        if result is None:
            raise ORSClientError("ORS SSE response did not contain an end event")
        if task_id is not None and isinstance(result, dict):
            result.setdefault("task_id", task_id)
        return result

    def session(
        self,
        env_name: str,
        *,
        task_spec: Mapping[str, Any] | None = None,
        split: str | None = None,
        index: int | None = None,
        secrets: Mapping[str, Any] | None = None,
    ) -> ORSClientSession:
        sid = self.create_session()
        try:
            self.create(
                sid,
                env_name=env_name,
                task_spec=task_spec,
                split=split,
                index=index,
                secrets=secrets,
            )
        except Exception:
            with suppress(Exception):
                self.delete_session(sid)
            raise
        return ORSClientSession(self, env_name, sid)


class ORSClientSession:
    """Context-manager wrapper for one ORS episode."""

    def __init__(self, client: ORSClient, env_name: str, sid: str) -> None:
        self.client = client
        self.env_name = env_name
        self.sid = sid
        self._closed = False

    def prompt(self) -> list[dict[str, Any]]:
        return self.client.prompt(self.env_name, self.sid)

    def tools(self) -> list[dict[str, Any]]:
        return self.client.list_tools(self.env_name, sid=self.sid)

    def call(
        self,
        name: str,
        input: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return self.client.call(
            self.env_name,
            self.sid,
            name,
            input,
            task_id=task_id,
        )

    def ping(self) -> dict[str, Any]:
        return self.client.ping(self.sid)

    def close(self) -> None:
        if not self._closed:
            with suppress(ORSClientError):
                self.client.delete(self.sid)
            self._closed = True

    def __enter__(self) -> ORSClientSession:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
