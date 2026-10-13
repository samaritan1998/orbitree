from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from research_ops.openreward.correlation import (
    episode_correlation_token,
    tag_miaoda_query,
)
from research_ops.openreward.policy_proxy import PolicyCaptureProxy, PolicyRouteError


class _Request:
    query_params = {"mopd_episode_id": "episode-1"}
    headers = {}


class _Tokenizer:
    def apply_chat_template(self, messages, **kwargs):
        assert messages == [{"role": "user", "content": "private prompt"}]
        assert kwargs["tokenize"] is True
        return [10, 11]

    def encode(self, value, *, add_special_tokens):
        assert add_special_tokens is False
        return {"O": [20], "K": [21]}[value]


def test_policy_capture_keeps_only_token_logprobs_and_hashes(tmp_path: Path) -> None:
    proxy = PolicyCaptureProxy(
        upstream_url="http://127.0.0.1:9",
        capture_dir=tmp_path,
    )
    body = json.dumps(
        {
            "id": "response-1",
            "weight_version": "iter-1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {"bytes": [79], "logprob": -0.1},
                            {"bytes": [75], "logprob": -0.2},
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode()
    proxy._capture(
        {"model": "policy", "messages": [{"role": "user", "content": "secret?"}]},
        body,
        _Request(),
    )
    output = tmp_path / "capture-episode-1.jsonl"
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["output_token_count"] == 2
    assert record["weight_version"] == "iter-1"
    assert "messages" not in record
    assert "secret?" not in output.read_text(encoding="utf-8")


def test_policy_capture_v2_keeps_exact_ids_without_text(tmp_path: Path) -> None:
    proxy = PolicyCaptureProxy(
        upstream_url="http://127.0.0.1:9",
        capture_dir=tmp_path,
    )
    proxy.tokenizer = _Tokenizer()
    body = json.dumps(
        {
            "id": "response-1",
            "weight_version": "iter-2",
            "choices": [
                {
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {"token": "O", "logprob": -0.1},
                            {"token": "K", "logprob": -0.2},
                        ]
                    },
                }
            ],
        }
    ).encode()
    proxy._capture(
        {
            "model": "policy",
            "messages": [{"role": "user", "content": "private prompt"}],
        },
        body,
        _Request(),
    )

    output = tmp_path / "capture-episode-1.jsonl"
    record = json.loads(output.read_text())
    assert record["schema_version"] == "mopd-policy-capture.v2"
    assert record["prompt_token_ids"] == [10, 11]
    assert record["response_token_ids"] == [20, 21]
    assert record["behavior_logprobs"] == [-0.1, -0.2]
    assert record["weight_version"] == "iter-2"
    assert "private prompt" not in output.read_text()


def test_policy_capture_prefers_sglang_native_token_ids(tmp_path: Path) -> None:
    proxy = PolicyCaptureProxy(
        upstream_url="http://127.0.0.1:9",
        capture_dir=tmp_path,
    )
    proxy.tokenizer = _Tokenizer()
    body = json.dumps(
        {
            "id": "response-native",
            "weight_version": "iter-native",
            "choices": [{"finish_reason": "stop"}],
            "meta_info": {
                "output_token_logprobs": [[-0.3, 1234, None], [-0.4, 5678, None]]
            },
        }
    ).encode()
    proxy._capture(
        {
            "model": "policy",
            "messages": [{"role": "user", "content": "private prompt"}],
        },
        body,
        _Request(),
    )

    record = json.loads((tmp_path / "capture-episode-1.jsonl").read_text())
    assert record["response_token_ids"] == [1234, 5678]
    assert record["behavior_logprobs"] == [-0.3, -0.4]


def test_policy_proxy_reloads_trainer_owned_route(tmp_path: Path) -> None:
    route = tmp_path / "policy-route.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ready",
                "protocol": "slime-sglang-policy-route.v1",
                "upstream_url": "http://198.51.100.2:30000",
            }
        )
    )
    proxy = PolicyCaptureProxy(
        upstream_route_file=route,
        capture_dir=tmp_path / "capture",
    )
    assert proxy._resolve_upstream_url() == "http://198.51.100.2:30000"

    route.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ready",
                "protocol": "slime-sglang-policy-route.v1",
                "upstream_url": "http://198.51.100.3:30001",
            }
        )
    )
    assert proxy._resolve_upstream_url() == "http://198.51.100.3:30001"


def test_policy_proxy_dynamic_route_fails_closed_on_loopback(tmp_path: Path) -> None:
    route = tmp_path / "policy-route.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ready",
                "protocol": "slime-sglang-policy-route.v1",
                "upstream_url": "http://127.0.0.1:30000",
            }
        )
    )
    proxy = PolicyCaptureProxy(
        upstream_route_file=route,
        capture_dir=tmp_path / "capture",
    )
    with pytest.raises(PolicyRouteError, match="externally reachable"):
        proxy._resolve_upstream_url()


def test_policy_proxy_requires_exactly_one_upstream_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PolicyCaptureProxy(capture_dir=tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        PolicyCaptureProxy(
            upstream_url="http://policy.test:30000",
            upstream_route_file=tmp_path / "route.json",
            capture_dir=tmp_path,
        )


def test_managed_policy_proxy_strips_marker_and_forwards_trusted_episode_id(
    tmp_path: Path,
) -> None:
    token = episode_correlation_token(
        batch_id="optimizer-1", episode_id="source-private-id", member_index=0
    )
    upstream_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        payload = json.loads(request.content)
        assert payload["messages"] == [{"role": "user", "content": "private prompt"}]
        assert payload["logprobs"] is True
        assert payload["top_logprobs"] == 0
        assert request.headers["x-mopd-episode-id"] == token
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "weight_version": "iter-7",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                        "logprobs": {
                            "content": [{"bytes": [100], "logprob": -0.25}]
                        },
                    }
                ],
            },
        )

    app = FastAPI()
    proxy = PolicyCaptureProxy(
        upstream_url="http://policy.test",
        capture_dir=tmp_path,
        transport=httpx.MockTransport(upstream),
    )
    proxy.install(app)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ors.test"
        ) as client:
            return await client.post(
                "/policy/v1/chat/completions",
                params={
                    "mopd_episode_id": "shared-group-id",
                    "mopd_require_record_correlation": "1",
                },
                json={
                    "model": "policy",
                    "messages": [
                        {
                            "role": "user",
                            "content": tag_miaoda_query("private prompt", token),
                        }
                    ],
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert len(upstream_requests) == 1
    capture = tmp_path / f"capture-{token}.jsonl"
    record = json.loads(capture.read_text())
    assert record["episode_id"] == token
    assert record["weight_version"] == "iter-7"
    assert "ORS_MOPD_EPISODE" not in capture.read_text()
    assert "private prompt" not in capture.read_text()
    http_record = json.loads((tmp_path / f"http-{token}.jsonl").read_text())
    assert http_record["episode_id"] == token
    assert http_record["status_code"] == 200
    assert "private prompt" not in json.dumps(http_record)


def test_managed_policy_proxy_rejects_request_without_record_marker(
    tmp_path: Path,
) -> None:
    upstream_calls = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={})

    app = FastAPI()
    proxy = PolicyCaptureProxy(
        upstream_url="http://policy.test",
        capture_dir=tmp_path,
        transport=httpx.MockTransport(upstream),
    )
    proxy.install(app)

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://ors.test"
        ) as client:
            return await client.post(
                "/policy/v1/chat/completions",
                params={"mopd_require_record_correlation": "1"},
                json={
                    "model": "policy",
                    "messages": [{"role": "user", "content": "private prompt"}],
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 422
    assert response.json() == {"error": "managed episode correlation is required"}
    assert upstream_calls == 0
