#!/usr/bin/env python3
"""Expose SGLang prompt scoring through the shared OpenAI gateway path."""

from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

TOKEN_SENTINEL = "__openreward_input_ids__:"


def _extract_input_ids(body: dict[str, Any]) -> list[int]:
    direct = body.get("input_ids")
    if isinstance(direct, list):
        return direct
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return []
    content = messages[0].get("content") if isinstance(messages[0], dict) else None
    if not isinstance(content, str) or not content.startswith(TOKEN_SENTINEL):
        return []
    try:
        decoded = json.loads(content[len(TOKEN_SENTINEL) :])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid OpenReward token sentinel") from exc
    return decoded if isinstance(decoded, list) else []


def create_app(*, upstream_url: str, model_name: str, timeout_seconds: float) -> FastAPI:
    client: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal client
        client = httpx.AsyncClient(timeout=timeout_seconds, trust_env=False)
        try:
            yield
        finally:
            await client.aclose()
            client = None

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        assert client is not None
        try:
            response = await client.get(upstream_url.rsplit("/", 1)[0] + "/health")
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail="teacher upstream unavailable") from exc
        if response.status_code != 200:
            raise HTTPException(status_code=503, detail="teacher upstream unhealthy")
        return {"ready": True, "model": model_name}

    @app.post("/v1/chat/completions")
    async def score(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        requested_model = body.get("model")
        if requested_model is not None and requested_model != model_name:
            raise HTTPException(status_code=400, detail="teacher model mismatch")
        input_ids = _extract_input_ids(body)
        if (
            not isinstance(input_ids, list)
            or not input_ids
            or any(not isinstance(item, int) or isinstance(item, bool) for item in input_ids)
        ):
            raise HTTPException(
                status_code=400,
                detail="input_ids must be a non-empty integer list",
            )

        # The public gateway accepts the OpenAI chat schema, while SGLang's
        # raw scorer expects sampling_params and return_logprob. Keep the
        # legacy direct-input_ids path intact for local probes, and translate
        # standard chat requests at this adapter boundary.
        if isinstance(body.get("input_ids"), list):
            payload = dict(body)
            payload.pop("model", None)
            payload.pop("messages", None)
            payload["input_ids"] = input_ids
        else:
            sampling_params = body.get("sampling_params")
            sampling_params = (
                {} if not isinstance(sampling_params, dict) else dict(sampling_params)
            )
            if "temperature" in body:
                sampling_params["temperature"] = body["temperature"]
            if "top_p" in body:
                sampling_params["top_p"] = body["top_p"]
            if "max_tokens" in body:
                sampling_params["max_new_tokens"] = body["max_tokens"]
            sampling_params.setdefault("max_new_tokens", 0)
            sampling_params.setdefault("skip_special_tokens", False)
            payload = {
                "input_ids": input_ids,
                "sampling_params": sampling_params,
                "return_logprob": True,
                "logprob_start_len": body.get("logprob_start_len", 0),
            }
        assert client is not None
        try:
            upstream = await client.post(upstream_url, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="teacher scoring request failed") from exc
        if upstream.status_code != 200:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type", "application/json"),
            )
        try:
            raw = upstream.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="teacher returned invalid JSON") from exc
        meta_info = raw.get("meta_info") if isinstance(raw, dict) else None
        records = meta_info.get("input_token_logprobs") if isinstance(meta_info, dict) else None
        token_logprobs = []
        for record in records or []:
            if isinstance(record, dict):
                token_logprobs.append(record.get("logprob", record.get("log_prob")))
            elif isinstance(record, list) and record:
                token_logprobs.append(record[0])
            else:
                token_logprobs.append(None)
        response_body = {
            "id": (meta_info or {}).get("id", "openreward-teacher-score"),
            "object": "chat.completion",
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "logprobs": {"token_logprobs": token_logprobs},
                    "finish_reason": "stop",
                }
            ],
            "meta_info": meta_info or {},
        }
        return Response(content=json.dumps(response_body), media_type="application/json")

    return app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()
    app = create_app(
        upstream_url=args.upstream_url,
        model_name=args.model_name,
        timeout_seconds=args.timeout_seconds,
    )
    uvicorn.run(app, host=args.listen_host, port=args.listen_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
