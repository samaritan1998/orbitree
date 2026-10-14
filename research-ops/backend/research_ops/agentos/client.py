"""Small, credential-safe HTTP client for AgentOS/Datawheel.

Only the stable control-plane operations needed by Orbitree live here:
submit a Datawheel batch, observe it, resolve a result download, and fetch a
Miaoda trajectory.  The client intentionally does not know about the SDK or
about the application's private database/sandbox internals.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class AgentOSClientError(RuntimeError):
    """Base error for AgentOS protocol/configuration failures."""


class AgentOSConfigurationError(AgentOSClientError):
    """Raised when the endpoint or token reference is not usable."""


class AgentOSHTTPError(AgentOSClientError):
    """Raised for a non-successful AgentOS response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class AgentOSBatchSnapshot:
    """Normalized view of a Datawheel batch status response."""

    batch_task_id: str
    status: str
    status_counts: dict[str, int] = field(default_factory=dict)
    total_size: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_datawheel_base_url(value: str) -> str:
    """Normalize a host or API root to ``.../api/v1/datawheel``.

    Callers may provide a host, an ``/api/v1`` root, or the complete
    Datawheel root.  No default production endpoint is embedded here; the
    application must configure one explicitly.
    """

    base = value.strip().rstrip("/")
    if not base:
        raise AgentOSConfigurationError("AgentOS Datawheel endpoint is not configured")
    if base.endswith("/api/v1/datawheel"):
        return base
    if base.endswith("/api/v1"):
        return f"{base}/datawheel"
    return f"{base}/api/v1/datawheel"


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _extract_batch_id(value: Any) -> str | None:
    """Extract a batch id from the response shapes seen in Datawheel."""

    if isinstance(value, dict):
        for key in ("batch_task_id", "batchTaskId", "task_id", "taskId", "id"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                return str(candidate).strip()
        for key in ("data", "result", "task"):
            nested = _extract_batch_id(value.get(key))
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _extract_batch_id(item)
            if nested:
                return nested
    return None


def _request_secret_values(request: httpx.Request) -> set[str]:
    values: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        normalized = key.casefold().replace("-", "_")
        if normalized in {"api_key", "apikey", "authorization", "token"}:
            if isinstance(value, str) and value:
                values.add(value)
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child)

    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
        visit(json.loads(request.content.decode("utf-8")))
    return values


def _response_message(response: httpx.Response) -> str:
    """Build a bounded error message without echoing request credentials."""

    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = response.text
    text = str(payload)
    for header_name in ("Authorization", "X-Authorization"):
        authorization = response.request.headers.get(header_name)
        if authorization:
            text = text.replace(authorization, "<redacted>")
            if authorization.casefold().startswith("bearer "):
                text = text.replace(authorization.split(None, 1)[1], "<redacted>")
    for secret in _request_secret_values(response.request):
        text = text.replace(secret, "<redacted>")
    return text[:1_000]


class AgentOSClient:
    """Credential-safe synchronous HTTP adapter.

    The bearer token is resolved from ``token_env`` for every request rather
    than stored in settings, database rows, or task metadata.  ``transport``
    is injectable so tests can use ``httpx.MockTransport`` without touching a
    real AgentOS environment.
    """

    provider = "agentos/datawheel"

    def __init__(
        self,
        base_url: str,
        *,
        trajectory_base_url: str | None = None,
        token_env: str = "DATAWHEEL_TOKEN",
        timeout_seconds: float = 60.0,
        verify: bool = True,
        trust_env: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_datawheel_base_url(base_url)
        self.trajectory_base_url = normalize_datawheel_base_url(
            trajectory_base_url or base_url
        )
        self.token_env = token_env.strip() or "DATAWHEEL_TOKEN"
        self.timeout_seconds = timeout_seconds
        self.verify = verify
        self.trust_env = trust_env
        self.transport = transport

    def _token(self) -> str:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise AgentOSConfigurationError(
                f"AgentOS token is unavailable; configure environment variable {self.token_env}"
            )
        # The installed miaoda-agent-sdk stores ``datawheel_key`` as a full
        # ``Bearer <token>`` value, while the Orbitree adapter normally
        # expects the token body and adds the scheme below.  Accept both forms
        # so a transient SDK-config bridge cannot accidentally send a doubled
        # ``Bearer Bearer`` header.
        if token.casefold().startswith("bearer "):
            token = token.split(None, 1)[1].strip()
        if not token:
            raise AgentOSConfigurationError(
                f"AgentOS token is unavailable; configure environment variable {self.token_env}"
            )
        return token

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token()}",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        idempotency_key: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        try:
            with httpx.Client(
                base_url=base_url or self.base_url,
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=self._headers(idempotency_key=idempotency_key),
                )
        except httpx.HTTPError as exc:
            raise AgentOSClientError(f"AgentOS request failed: {type(exc).__name__}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise AgentOSHTTPError(response.status_code, _response_message(response))
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise AgentOSClientError("AgentOS returned a non-JSON response") from exc

    def submit_batch(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> AgentOSBatchSnapshot:
        """Submit one Datawheel batch and return its initial status."""

        response = self._request(
            "POST",
            "/agent/batch/new",
            json_body=dict(payload),
            idempotency_key=idempotency_key,
        )
        batch_task_id = _extract_batch_id(response)
        if not batch_task_id:
            raise AgentOSClientError("AgentOS submit response did not contain batch_task_id")
        raw = _as_mapping(response)
        status = str(raw.get("status") or "submitted")
        return AgentOSBatchSnapshot(batch_task_id=batch_task_id, status=status, raw=raw)

    def get_batch(self, batch_task_id: str) -> AgentOSBatchSnapshot:
        """Fetch one batch status from Datawheel."""

        identifier = str(batch_task_id).strip()
        if not identifier:
            raise ValueError("batch_task_id must not be empty")
        response = self._request(
            "GET",
            "/agent/batch/list",
            params={"batch_task_id": identifier},
        )
        raw_response = _as_mapping(response)
        data = raw_response.get("data")
        if isinstance(data, list):
            task = _as_mapping(data[0]) if data else {}
        elif isinstance(data, dict):
            task = data
        else:
            task = raw_response
        counts_raw = task.get("status_counts") or task.get("statusCounts") or {}
        counts = {
            str(key): int(value)
            for key, value in _as_mapping(counts_raw).items()
            if isinstance(value, (int, float))
        }
        total = task.get("total_size") or task.get("totalSize")
        total_size = int(total) if isinstance(total, (int, float)) else None
        return AgentOSBatchSnapshot(
            batch_task_id=identifier,
            status=str(task.get("status") or "unknown"),
            status_counts=counts,
            total_size=total_size,
            raw=task,
        )

    def list_batches(self, *, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        """List one Dashboard page without exposing request credentials."""

        if page < 1 or page_size < 1 or page_size > 1_000:
            raise ValueError("invalid AgentOS batch pagination")
        response = self._request(
            "GET",
            "/agent/batch/list",
            params={"page": int(page), "page_size": int(page_size)},
        )
        raw = _as_mapping(response)
        data = raw.get("data")
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, Mapping)]
        if isinstance(data, Mapping):
            for key in ("list", "items", "records", "tasks"):
                items = data.get(key)
                if isinstance(items, list):
                    return [dict(item) for item in items if isinstance(item, Mapping)]
        return []

    def get_dataset_records(self, dataset_name: str) -> list[dict[str, Any]]:
        """Fetch a named Datawheel dataset without persisting signed record fields.

        Dataset rows can contain short-lived replay or attachment URLs.  The
        adapter returns them to the immediate caller but intentionally does
        not cache the response or expose the raw envelope.
        """

        name = str(dataset_name).strip()
        if not name:
            raise ValueError("dataset_name must not be empty")
        response = self._request(
            "GET",
            "/agent/eval/datasets/data",
            params={"dataset_name": name},
        )
        data = _as_mapping(response).get("data")
        if not isinstance(data, list) or not data:
            raise AgentOSClientError("AgentOS dataset response did not contain records")
        records = [dict(item) for item in data if isinstance(item, Mapping)]
        if len(records) != len(data):
            raise AgentOSClientError("AgentOS dataset response contained invalid records")
        return records

    def list_batch_records(
        self,
        batch_task_id: str,
        *,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Return every record for one batch through the Datawheel JSON API.

        This avoids a signed BOS workbook download for consumers that only
        need record IDs and generated application IDs. The server-side filter
        is named after the record model's foreign-key field; using the more
        obvious ``batch_task_id`` silently returns the global record table.
        """

        identifier = str(batch_task_id).strip()
        if not identifier:
            raise ValueError("batch_task_id must not be empty")
        if page_size < 1 or page_size > 1_000:
            raise ValueError("invalid AgentOS record pagination")

        records: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                "/agent/record/list",
                params={
                    "agent_generate_batch_task_id": identifier,
                    "page": page,
                    "page_size": int(page_size),
                },
            )
            raw = _as_mapping(response)
            data = raw.get("data")
            if not isinstance(data, list):
                raise AgentOSClientError("AgentOS record list did not contain data")
            batch_records = [
                dict(item) for item in data if isinstance(item, Mapping)
            ]
            if len(batch_records) != len(data) or any(
                str(item.get("agent_generate_batch_task_id") or "").strip()
                != identifier
                for item in batch_records
            ):
                raise AgentOSClientError("AgentOS record list batch filter was not applied")
            records.extend(batch_records)

            total_raw = raw.get("total")
            total = int(total_raw) if isinstance(total_raw, (int, float)) else None
            if total is not None and len(records) >= total:
                if len(records) != total:
                    raise AgentOSClientError("AgentOS record list exceeded its total")
                return records
            if len(batch_records) < page_size:
                return records
            page += 1
            if page > 10_000:
                raise AgentOSClientError("AgentOS record pagination exceeded its limit")

    def get_result_download_url(self, batch_task_id: str) -> str:
        """Resolve the short-lived result URL without persisting or logging it."""

        response = self._request(
            "GET",
            "/agent/record/download",
            params={"batch_task_id": str(batch_task_id).strip()},
        )
        url = _as_mapping(response).get("url")
        if not isinstance(url, str) or not url.strip():
            raise AgentOSClientError("AgentOS download response did not contain a url")
        return url

    def download_result(self, batch_task_id: str, destination: Path) -> Path:
        """Download a batch result atomically without storing its signed URL."""

        signed_url = self.get_result_download_url(batch_task_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.part")
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client, client.stream(
                "GET",
                signed_url,
                headers={"Accept": "application/octet-stream"},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise AgentOSHTTPError(response.status_code, _response_message(response))
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        if chunk:
                            stream.write(chunk)
            if not temporary.exists() or temporary.stat().st_size == 0:
                raise AgentOSClientError("AgentOS result download was empty")
            os.replace(temporary, destination)
        except httpx.HTTPError as exc:
            raise AgentOSClientError(
                f"AgentOS result download failed: {type(exc).__name__}"
            ) from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def get_trajectory(self, app_id: str, *, environment: str = "evaluation") -> Any:
        """Fetch the canonical ``coding_messages_all`` trajectory for an app."""

        identifier = str(app_id).strip()
        if not identifier:
            raise ValueError("app_id must not be empty")
        return self._request(
            "GET",
            "/miaoda/parse/coding_messages_all",
            params={"app_id": identifier, "env": environment},
            base_url=self.trajectory_base_url,
        )

    def update_concurrency(self, batch_task_id: str, concurrency: int) -> Any:
        """Pause or resize a batch through the Datawheel control endpoint."""

        if concurrency < 0:
            raise ValueError("concurrency must be non-negative")
        return self._request(
            "POST",
            "/agent/batch/update_concurrency",
            json_body={"batch_task_id": str(batch_task_id).strip(), "concurrency": concurrency},
        )

    def resume_batch(self, batch_task_id: str) -> Any:
        """Resume scheduling pending records for a Datawheel batch."""

        identifier = str(batch_task_id).strip()
        if not identifier:
            raise ValueError("batch_task_id must not be empty")
        return self._request(
            "POST",
            "/agent/batch/resume",
            json_body={"batch_task_id": identifier},
        )

    def restart_records(
        self,
        record_ids: Sequence[str],
        *,
        skip_check: bool = False,
    ) -> Any:
        """Restart an explicit set of Datawheel records.

        Callers own failure classification and retry budgets. Keeping the
        record list mandatory prevents an empty request from being interpreted
        as a batch-wide retry by a future server implementation.
        """

        identifiers = [str(value).strip() for value in record_ids]
        if not identifiers or any(not value for value in identifiers):
            raise ValueError("record_ids must contain non-empty identifiers")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("record_ids must not contain duplicates")
        return self._request(
            "POST",
            "/agent/record/restart",
            json_body={"record_ids": identifiers, "skip_check": bool(skip_check)},
        )

    def terminate_batch(self, batch_task_id: str) -> Any:
        """Stop all active records in one batch while retaining its evidence."""

        identifier = str(batch_task_id).strip()
        if not identifier:
            raise ValueError("batch_task_id must not be empty")
        return self._request(
            "POST",
            "/agent/batch/terminate",
            json_body={"batch_task_id": identifier},
        )
