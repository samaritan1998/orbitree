"""Credential-safe policy gateway proxy used by the real Miaoda backend.

Datawheel's coding adapter does not expose a switch for returning token
logprobs. The ORS pod therefore provides a narrow authenticated proxy in
front of the policy gateway. It requests logprobs, forwards the response
unchanged, and stores only token bytes/logprobs plus hashes and correlation
IDs. Prompts, credentials, and full response text never enter the capture
file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .correlation import EpisodeCorrelationError, strip_episode_correlation


class PolicyRouteError(RuntimeError):
    """Raised when a trusted dynamic policy route is unavailable or invalid."""


def _normalize_upstream_url(value: Any, *, reject_loopback: bool) -> str:
    raw = str(value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise PolicyRouteError("policy upstream route must be an HTTP(S) origin")
    if reject_loopback and parts.hostname.casefold() in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise PolicyRouteError("policy upstream route must be externally reachable")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unattributed"
    filtered = "".join(
        char if (char.isalnum() or char in "._:-") else "_" for char in text
    )
    return filtered[:160] or "unattributed"


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _message_identity(value: Any) -> dict[str, Any]:
    """Return the stable OpenAI message fields used for trajectory joining."""

    message = value if isinstance(value, dict) else {}
    keys = ("role", "content", "name", "tool_call_id", "tool_calls", "function_call")
    return {key: message.get(key) for key in keys if key in message}


def _ids_sha256(values: list[int]) -> str:
    encoded = json.dumps(values, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class PolicyCaptureProxy:
    """Install a small OpenAI-compatible forwarding route on an ORS app."""

    def __init__(
        self,
        *,
        upstream_url: str | None = None,
        upstream_route_file: Path | None = None,
        capture_dir: Path,
        tokenizer_path: str | Path | None = None,
        chat_template_path: str | Path | None = None,
        prefix: str = "/policy",
        timeout_seconds: float = 3_600.0,
        force_logprobs: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if bool(str(upstream_url or "").strip()) == bool(upstream_route_file):
            raise ValueError(
                "configure exactly one policy upstream URL or route file"
            )
        self.upstream_url = (
            _normalize_upstream_url(upstream_url, reject_loopback=False)
            if upstream_url
            else None
        )
        if upstream_route_file is not None and not upstream_route_file.is_absolute():
            raise ValueError("policy upstream route file must be absolute")
        self.upstream_route_file = (
            upstream_route_file.resolve() if upstream_route_file else None
        )
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.prefix = "/" + str(prefix).strip().strip("/")
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self.force_logprobs = bool(force_logprobs)
        self.transport = transport
        self.tokenizer = (
            self._load_tokenizer(tokenizer_path, chat_template_path=chat_template_path)
            if tokenizer_path
            else None
        )
        self._write_lock = threading.Lock()

    @staticmethod
    def _load_tokenizer(
        path: str | Path,
        *,
        chat_template_path: str | Path | None = None,
    ) -> Any:
        try:
            from transformers import AutoTokenizer
        except ImportError:  # pragma: no cover - depends on the deployment image
            # The serving-side weight-version proxy may already be producing
            # v2 token captures. Keep the ORS control-plane proxy available
            # with its text-free v1 fallback when this image lacks optional
            # tokenizer dependencies; never fabricate token IDs here.
            return None
        tokenizer_path = Path(path).expanduser().resolve(strict=True)
        tokenizer = AutoTokenizer.from_pretrained(
            str(tokenizer_path), trust_remote_code=True
        )
        if chat_template_path is not None:
            explicit_template = Path(chat_template_path).expanduser().resolve(strict=True)
            tokenizer.chat_template = explicit_template.read_text(encoding="utf-8")
            return tokenizer
        template = tokenizer_path / "chat_template.jinja"
        if not getattr(tokenizer, "chat_template", None) and template.is_file():
            tokenizer.chat_template = template.read_text(encoding="utf-8")
        return tokenizer

    def _prompt_token_ids(self, payload: dict[str, Any]) -> list[int]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError("chat request has no messages")
        kwargs: dict[str, Any] = {
            "tokenize": True,
            "add_generation_prompt": True,
            "return_dict": False,
        }
        if isinstance(payload.get("tools"), list):
            kwargs["tools"] = payload["tools"]
        if isinstance(payload.get("chat_template_kwargs"), dict):
            kwargs.update(payload["chat_template_kwargs"])
        values = self.tokenizer.apply_chat_template(messages, **kwargs)
        if isinstance(values, dict):
            values = values.get("input_ids")
        if hasattr(values, "tolist"):
            values = values.tolist()
        if isinstance(values, list) and values and isinstance(values[0], list):
            values = values[0]
        if not isinstance(values, list) or not values:
            raise ValueError("chat template returned no prompt token IDs")
        return [int(value) for value in values]

    def _response_token_values(
        self, response: dict[str, Any]
    ) -> tuple[list[int], list[float]]:
        meta_info = response.get("meta_info")
        native = (
            meta_info.get("output_token_logprobs")
            if isinstance(meta_info, dict)
            else None
        )
        if isinstance(native, list) and native:
            token_ids: list[int] = []
            values: list[float] = []
            for item in native:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    raise ValueError("chat response has malformed native token logprobs")
                logprob = _finite(item[0])
                token_id = item[1]
                if (
                    logprob is None
                    or isinstance(token_id, bool)
                    or not isinstance(token_id, int)
                    or token_id < 0
                ):
                    raise ValueError("chat response has invalid native token logprobs")
                values.append(logprob)
                token_ids.append(token_id)
            return token_ids, values

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("chat response has no choice")
        logprobs = choices[0].get("logprobs")
        content = logprobs.get("content") if isinstance(logprobs, dict) else None
        if not isinstance(content, list) or not content:
            raise ValueError("chat response has no token logprobs")
        token_ids: list[int] = []
        values: list[float] = []
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("token"), str):
                raise ValueError("chat response has malformed token logprobs")
            encoded = self.tokenizer.encode(
                item["token"], add_special_tokens=False
            )
            if hasattr(encoded, "tolist"):
                encoded = encoded.tolist()
            if not isinstance(encoded, list) or len(encoded) != 1:
                raise ValueError("response token does not map to one tokenizer ID")
            logprob = _finite(item.get("logprob"))
            if logprob is None:
                raise ValueError("chat response contains a non-finite logprob")
            token_ids.append(int(encoded[0]))
            values.append(logprob)
        return token_ids, values

    def _resolve_upstream_url(self) -> str:
        if self.upstream_route_file is None:
            assert self.upstream_url is not None
            return self.upstream_url
        try:
            payload = json.loads(self.upstream_route_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PolicyRouteError("policy upstream route is unavailable") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("status") != "ready"
            or payload.get("protocol") != "slime-sglang-policy-route.v1"
        ):
            raise PolicyRouteError("policy upstream route has an unsupported schema")
        return _normalize_upstream_url(
            payload.get("upstream_url"), reject_loopback=True
        )

    def install(self, app: FastAPI) -> None:
        route = f"{self.prefix}/{{path:path}}"

        @app.api_route(route, methods=["GET", "POST"])
        async def proxy(request: Request, path: str) -> Response:
            return await self.handle(request, path)

    async def handle(self, request: Request, path: str) -> Response:
        raw = await request.body()
        payload: dict[str, Any] | None = None
        correlation_id: str | None = None
        outgoing = raw
        normalized_path = "/" + str(path).lstrip("/")
        if (
            request.method == "POST"
            and normalized_path.rstrip("/") == "/v1/chat/completions"
        ):
            try:
                decoded = json.loads(raw or b"{}")
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                payload = dict(decoded)
                try:
                    cleaned_messages, correlation_id = strip_episode_correlation(
                        payload.get("messages")
                    )
                except EpisodeCorrelationError:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "ambiguous managed episode correlation"},
                    )
                require_record_correlation = str(
                    request.query_params.get("mopd_require_record_correlation") or ""
                ).strip().casefold() in {"1", "true", "yes", "on"}
                if require_record_correlation and not correlation_id:
                    return JSONResponse(
                        status_code=422,
                        content={"error": "managed episode correlation is required"},
                    )
                if correlation_id:
                    payload["messages"] = cleaned_messages
                if self.force_logprobs:
                    payload["logprobs"] = True
                    payload["top_logprobs"] = 0
                outgoing = json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")
                ).encode()

        headers = {
            key: value
            for key, value in request.headers.items()
            if key.casefold()
            not in {
                "host",
                "content-length",
                "connection",
                "authorization",
                "x-api-key",
                "x-mopd-episode-id",
                "cookie",
            }
        }
        if correlation_id:
            headers["x-mopd-episode-id"] = correlation_id
        headers["content-length"] = str(len(outgoing))
        try:
            target = self._resolve_upstream_url() + normalized_path
        except PolicyRouteError:
            self._record_http_status(request, correlation_id, 503)
            return JSONResponse(
                status_code=503, content={"error": "policy route unavailable"}
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=False,
                transport=self.transport,
            ) as client:
                upstream = await client.request(
                    request.method,
                    target,
                    content=outgoing,
                    headers=headers,
                )
        except httpx.HTTPError:
            self._record_http_status(request, correlation_id, 502)
            return JSONResponse(
                status_code=502, content={"error": "policy upstream unavailable"}
            )

        body = upstream.content
        self._record_http_status(request, correlation_id, upstream.status_code)
        if payload is not None and upstream.status_code == 200:
            self._capture(
                payload,
                body,
                request,
                correlation_id=correlation_id,
            )
        response_headers: dict[str, str] = {}
        content_type = upstream.headers.get("content-type")
        if content_type:
            response_headers["content-type"] = content_type
        return Response(
            content=body,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    def _record_http_status(
        self,
        request: Request,
        correlation_id: str | None,
        status_code: int,
    ) -> None:
        if not correlation_id:
            return
        episode_id = _safe_id(correlation_id)
        record = {
            "schema_version": "mopd-policy-http.v1",
            "captured_at": datetime.now(UTC).isoformat(),
            "episode_id": episode_id,
            "request_id": _safe_id(request.headers.get("x-request-id")),
            "status_code": int(status_code),
        }
        destination = self.capture_dir / f"http-{episode_id}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(line)
            os.chmod(destination, 0o600)

    def _capture(
        self,
        request_payload: dict[str, Any],
        body: bytes,
        request: Request,
        *,
        correlation_id: str | None = None,
    ) -> None:
        try:
            response = json.loads(body)
        except (TypeError, ValueError):
            return
        if not isinstance(response, dict):
            return
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        episode_id = _safe_id(
            correlation_id
            or request.query_params.get("mopd_episode_id")
            or request.headers.get("x-mopd-episode-id")
            or request.headers.get("x-request-id")
        )
        version = response.get("weight_version")
        metadata = response.get("metadata")
        if not version and isinstance(metadata, dict):
            version = metadata.get("weight_version")
        choices0 = choices[0]
        record: dict[str, Any] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_id": str(uuid4()),
            "episode_id": episode_id,
            "request_id": _safe_id(request.headers.get("x-request-id")),
            "model": _safe_id(request_payload.get("model")),
            "response_id": _safe_id(response.get("id")),
            "weight_version": _safe_id(version),
            "finish_reason": choices0.get("finish_reason"),
            # These hashes let a batched Datawheel rollout assign captures to
            # the matching AgentOS trajectory without storing prompt/output
            # text. They are emitted for both capture schema versions.
            "prompt_sha256": _sha256(request_payload.get("messages")),
            "response_message_sha256": _sha256(
                _message_identity(choices0.get("message"))
            ),
        }
        if self.tokenizer is not None:
            try:
                prompt_ids = self._prompt_token_ids(request_payload)
                response_ids, behavior = self._response_token_values(response)
            except (TypeError, ValueError, OverflowError):
                return
            record.update(
                {
                    "schema_version": "mopd-policy-capture.v2",
                    "prompt_token_ids": prompt_ids,
                    "response_token_ids": response_ids,
                    "behavior_logprobs": behavior,
                    "prompt_token_count": len(prompt_ids),
                    "output_token_count": len(response_ids),
                    "prompt_tokens_sha256": _ids_sha256(prompt_ids),
                    "response_tokens_sha256": _ids_sha256(response_ids),
                }
            )
        else:
            logprobs = choices[0].get("logprobs")
            content = logprobs.get("content") if isinstance(logprobs, dict) else None
            records: list[dict[str, Any]] = []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    raw_bytes = item.get("bytes")
                    if not isinstance(raw_bytes, list):
                        continue
                    try:
                        encoded = base64.b64encode(
                            bytes(int(value) for value in raw_bytes)
                        ).decode("ascii")
                    except (TypeError, ValueError, OverflowError):
                        continue
                    value = _finite(item.get("logprob"))
                    if value is not None:
                        records.append({"bytes_b64": encoded, "logprob": value})
            record.update(
                {
                    "schema_version": "mopd-policy-capture.v1",
                    "output_token_count": len(records),
                    "logprobs_available": bool(records),
                    "tokens": records,
                }
            )
        usage = response.get("usage")
        if isinstance(usage, dict):
            record["usage"] = {
                key: int(value)
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, (int, float))
            }
        destination = self.capture_dir / f"capture-{episode_id}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            with destination.open("a", encoding="utf-8") as stream:
                stream.write(line)
            os.chmod(destination, 0o600)


def install_policy_capture_proxy(
    app: FastAPI,
    *,
    upstream_url: str | None = None,
    upstream_route_file: str | Path | None = None,
    capture_dir: str | Path,
    tokenizer_path: str | Path | None = None,
    chat_template_path: str | Path | None = None,
    prefix: str = "/policy",
    timeout_seconds: float = 3_600.0,
) -> PolicyCaptureProxy:
    proxy = PolicyCaptureProxy(
        upstream_url=upstream_url,
        upstream_route_file=(
            Path(upstream_route_file) if upstream_route_file else None
        ),
        capture_dir=Path(capture_dir),
        tokenizer_path=tokenizer_path,
        chat_template_path=chat_template_path,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
    )
    proxy.install(app)
    return proxy
