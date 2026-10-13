#!/usr/bin/env python3
"""Small SGLang proxy with an explicit deployment version.

Some SGLang images expose ``weight_version`` in generation responses but do
not expose the ``/get_weight_version`` and ``/update_weight_version`` admin
routes.  This proxy keeps that metadata at the serving boundary instead of
making the training adapter guess from a model name.  It forwards the native
``/generate`` contract unchanged, and adds the version to ``meta_info`` in
JSON responses.

An optional Miaoda capture mode runs beside the exact policy tokenizer.  It
stores prompt/response token IDs and behavior logprobs without retaining raw
messages or response text.  This avoids joining normalized AgentOS replay
messages back to the gateway request by a fragile text hash.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


class _State:
    def __init__(self, version: str) -> None:
        self.version = version or "default"
        self.lock = threading.Lock()

    def get(self) -> str:
        with self.lock:
            return self.version

    def set(self, version: str) -> str:
        value = str(version or "").strip()
        if not value or value == "default":
            raise ValueError("new_version must be concrete")
        with self.lock:
            self.version = value
            return value


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _safe_id(value: object) -> str:
    text = str(value or "").strip()
    filtered = "".join(
        char if (char.isalnum() or char in "._:-") else "_" for char in text
    )
    return filtered[:160] or "unattributed"


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _ids_sha256(values: list[int]) -> str:
    encoded = json.dumps(values, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PolicyTokenCapture:
    """Persist a text-free, trainable token view of one chat completion."""

    def __init__(self, capture_dir: Path, tokenizer: object) -> None:
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.tokenizer = tokenizer
        self._write_lock = threading.Lock()

    @classmethod
    def from_tokenizer_path(
        cls,
        capture_dir: Path,
        tokenizer_path: str,
        chat_template_path: Path | None = None,
    ) -> PolicyTokenCapture:
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:  # pragma: no cover - exercised in LTP image
            raise RuntimeError(
                "policy token capture requires transformers in the serving image"
            ) from exc
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path,
            trust_remote_code=True,
        )
        if chat_template_path is not None:
            explicit_template = chat_template_path.expanduser().resolve(strict=True)
            tokenizer.chat_template = explicit_template.read_text(encoding="utf-8")
            return cls(capture_dir, tokenizer)
        # HF conversion keeps Qwen3.5's template as a standalone Jinja file.
        # Some serving-image Transformers versions do not attach that file to
        # ``tokenizer.chat_template`` automatically, even though SGLang loads
        # it for /v1/chat/completions.  Use the exact adjacent template so the
        # capture prompt IDs match the serving prompt rather than inventing a
        # fallback format.
        template_path = Path(tokenizer_path) / "chat_template.jinja"
        if not getattr(tokenizer, "chat_template", None) and template_path.is_file():
            tokenizer.chat_template = template_path.read_text(encoding="utf-8")
        return cls(capture_dir, tokenizer)

    def _prompt_ids(self, payload: dict[str, object]) -> list[int]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError("chat request has no messages")
        kwargs: dict[str, object] = {
            "tokenize": True,
            "add_generation_prompt": True,
            "return_dict": False,
        }
        tools = payload.get("tools")
        if isinstance(tools, list):
            kwargs["tools"] = tools
        template_kwargs = payload.get("chat_template_kwargs")
        if isinstance(template_kwargs, dict):
            kwargs.update(template_kwargs)
        values = self.tokenizer.apply_chat_template(messages, **kwargs)  # type: ignore[attr-defined]
        if isinstance(values, dict) and "input_ids" in values:
            values = values["input_ids"]
        if hasattr(values, "tolist"):
            values = values.tolist()
        if (
            isinstance(values, list)
            and values
            and isinstance(values[0], list)
            and all(isinstance(item, int) for item in values[0])
        ):
            values = values[0]
        if not isinstance(values, list) or not values:
            raise ValueError("chat template returned no prompt token IDs")
        return [int(value) for value in values]

    def _response_values(
        self, response: dict[str, object]
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
            if not isinstance(item, dict):
                raise ValueError("chat response has malformed token logprobs")
            token = item.get("token")
            if not isinstance(token, str):
                raise ValueError("chat response token text is unavailable in memory")
            encoded = self.tokenizer.encode(  # type: ignore[attr-defined]
                token,
                add_special_tokens=False,
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

    def capture(
        self,
        *,
        request_body: bytes | None,
        response_body: bytes,
        request_path: str,
        request_headers: object,
    ) -> bool:
        """Write a bounded v2 record; never serialize request/response text."""

        try:
            payload = json.loads(request_body or b"{}")
            response = json.loads(response_body)
            if not isinstance(payload, dict) or not isinstance(response, dict):
                print("[policy-token-capture] skipped stage=json_shape", flush=True)
                return False
        except (TypeError, ValueError, json.JSONDecodeError):
            print("[policy-token-capture] skipped stage=json_parse", flush=True)
            return False
        try:
            prompt_ids = self._prompt_ids(payload)
        except (TypeError, ValueError) as exc:
            print(
                "[policy-token-capture] skipped stage=prompt_tokenize "
                f"error_type={type(exc).__name__}",
                flush=True,
            )
            return False
        try:
            response_ids, behavior = self._response_values(response)
        except (TypeError, ValueError) as exc:
            print(
                "[policy-token-capture] skipped stage=response_tokenize "
                f"error_type={type(exc).__name__}",
                flush=True,
            )
            return False
        parsed = urlsplit(request_path)
        query = dict(
            item.split("=", 1) if "=" in item else (item, "")
            for item in parsed.query.split("&")
            if item
        )
        header_get = getattr(request_headers, "get", lambda *_: None)
        episode_id = _safe_id(
            query.get("mopd_episode_id")
            or header_get("x-mopd-episode-id")
            or header_get("x-request-id")
        )
        choices = response.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        metadata = response.get("metadata")
        weight_version = response.get("weight_version")
        if not weight_version and isinstance(metadata, dict):
            weight_version = metadata.get("weight_version")
        record = {
            "schema_version": "mopd-policy-capture.v2",
            "captured_at": datetime.now(UTC).isoformat(),
            "capture_id": str(uuid4()),
            "episode_id": episode_id,
            "request_id": _safe_id(header_get("x-request-id")),
            "model": _safe_id(payload.get("model")),
            "response_id": _safe_id(response.get("id")),
            "weight_version": _safe_id(weight_version),
            "prompt_token_ids": prompt_ids,
            "response_token_ids": response_ids,
            "behavior_logprobs": behavior,
            "prompt_token_count": len(prompt_ids),
            "output_token_count": len(response_ids),
            "prompt_tokens_sha256": _ids_sha256(prompt_ids),
            "response_tokens_sha256": _ids_sha256(response_ids),
            "finish_reason": (
                first_choice.get("finish_reason")
                if isinstance(first_choice, dict)
                else None
            ),
        }
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
        print(
            "[policy-token-capture] wrote "
            f"episode={episode_id} prompt_tokens={len(prompt_ids)} "
            f"response_tokens={len(response_ids)}",
            flush=True,
        )
        return True


def _inject_version(path: str, body: bytes, version: str) -> tuple[bytes, str]:
    """Inject a version into JSON without touching token/logprob fields."""

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return body, "application/octet-stream"
    if not isinstance(payload, dict):
        return body, "application/json"
    if path.rstrip("/") == "/generate":
        meta = payload.get("meta_info")
        if not isinstance(meta, dict):
            meta = {}
            payload["meta_info"] = meta
        meta["weight_version"] = version
    else:
        payload["weight_version"] = version
    return _json_bytes(payload), "application/json"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _upstream(self) -> tuple[str, int, str]:
        server = self.server
        host = server.upstream_host  # type: ignore[attr-defined]
        port = server.upstream_port  # type: ignore[attr-defined]
        return host, port, self.path

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, method: str) -> None:
        server = self.server
        state = server.state  # type: ignore[attr-defined]
        if self.path.rstrip("/") == "/get_weight_version" and method == "GET":
            self._send(200, _json_bytes({"weight_version": state.get()}), "application/json")
            return
        if self.path.rstrip("/") == "/update_weight_version" and method == "POST":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw or b"{}")
                version = state.set(str(payload.get("new_version", "")))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, _json_bytes({"error": str(exc)}), "application/json")
                return
            self._send(200, _json_bytes({"weight_version": version}), "application/json")
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        host, port, path = self._upstream()
        capture = getattr(server, "policy_token_capture", None)
        normalized_path = path.split("?", 1)[0].rstrip("/")
        if capture is not None and method == "POST" and normalized_path == "/v1/chat/completions":
            try:
                capture_payload = json.loads(body or b"{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                capture_payload = None
            if isinstance(capture_payload, dict):
                capture_payload["logprobs"] = True
                capture_payload["top_logprobs"] = 0
                body = _json_bytes(capture_payload)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(host, port, timeout=3600)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            status = response.status
            content_type = response.getheader("Content-Type", "application/json")
        except (OSError, http.client.HTTPException) as exc:
            self._send(502, _json_bytes({"error": str(exc)}), "application/json")
            return
        finally:
            connection.close()
        if status == 200 and path.split("?", 1)[0].rstrip("/") in {
            "/generate",
            "/v1/chat/completions",
        }:
            response_body, content_type = _inject_version(
                path.split("?", 1)[0], response_body, state.get()
            )
        if (
            status == 200
            and capture is not None
            and normalized_path == "/v1/chat/completions"
        ):
            capture.capture(
                request_body=body,
                response_body=response_body,
                request_path=path,
                request_headers=self.headers,
            )
        self._send(status, response_body, content_type)

    def do_GET(self) -> None:  # noqa: N802
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._forward("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward("HEAD")

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the serving job log bounded and avoid echoing request bodies.
        print("[weight-version-proxy] " + (fmt % args), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="SGLang base URL")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--initial-version", default="default")
    parser.add_argument("--capture-dir", type=Path)
    parser.add_argument("--tokenizer-path")
    parser.add_argument(
        "--chat-template",
        type=Path,
        help="Exact serving chat template used to reconstruct capture prompt IDs",
    )
    args = parser.parse_args()
    if bool(args.capture_dir) != bool(args.tokenizer_path):
        raise SystemExit("--capture-dir and --tokenizer-path must be provided together")
    if args.chat_template and not args.capture_dir:
        raise SystemExit("--chat-template requires --capture-dir and --tokenizer-path")
    parsed = urlsplit(args.upstream if "://" in args.upstream else f"http://{args.upstream}")
    if parsed.scheme != "http" or not parsed.hostname:
        raise SystemExit("only an http upstream with a hostname is supported")
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), _Handler)
    server.upstream_host = parsed.hostname  # type: ignore[attr-defined]
    server.upstream_port = parsed.port or 80  # type: ignore[attr-defined]
    server.state = _State(args.initial_version)  # type: ignore[attr-defined]
    server.policy_token_capture = (  # type: ignore[attr-defined]
        PolicyTokenCapture.from_tokenizer_path(
            args.capture_dir,
            args.tokenizer_path,
            args.chat_template,
        )
        if args.capture_dir
        else None
    )
    print(
        f"weight-version proxy listening on {args.listen_host}:{args.listen_port} "
        f"-> {parsed.hostname}:{server.upstream_port}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
