from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from scripts.teacher_score_gateway import TOKEN_SENTINEL, create_app


def test_teacher_score_gateway_preserves_token_scoring_payload(monkeypatch) -> None:
    seen: list[dict] = []

    async def fake_post(self, url, *, json):
        del self
        seen.append({"url": url, "json": json})
        return httpx.Response(
            200,
            json={"meta_info": {"input_token_logprobs": [[None, 1], [-0.2, 2]]}},
        )

    async def fake_get(self, url):
        del self, url
        return httpx.Response(200, json={"ready": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    app = create_app(
        upstream_url="http://127.0.0.1:14334/generate",
        model_name="teacher-base",
        timeout_seconds=10,
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "teacher-base",
                "input_ids": [1, 2],
                "sampling_params": {"max_new_tokens": 0},
                "return_logprob": True,
                "logprob_start_len": 0,
            },
        )

    assert response.status_code == 200
    assert response.json()["meta_info"]["input_token_logprobs"][-1] == [-0.2, 2]
    assert seen == [
        {
            "url": "http://127.0.0.1:14334/generate",
            "json": {
                "input_ids": [1, 2],
                "sampling_params": {"max_new_tokens": 0},
                "return_logprob": True,
                "logprob_start_len": 0,
            },
        }
    ]


def test_teacher_score_gateway_rejects_wrong_model() -> None:
    app = create_app(
        upstream_url="http://127.0.0.1:14334/generate",
        model_name="teacher-base",
        timeout_seconds=10,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "other", "input_ids": [1]},
        )
    assert response.status_code == 400


def test_teacher_score_gateway_accepts_standard_chat_sentinel(monkeypatch) -> None:
    seen: list[dict] = []

    async def fake_post(self, url, *, json):
        del self
        seen.append({"url": url, "json": json})
        return httpx.Response(
            200,
            json={"meta_info": {"id": "sid", "input_token_logprobs": [[None, 1], [-0.4, 2]]}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    app = create_app(
        upstream_url="http://127.0.0.1:14334/generate",
        model_name="teacher-base",
        timeout_seconds=10,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "teacher-base",
                "messages": [{"role": "user", "content": TOKEN_SENTINEL + "[1,2]"}],
                "max_tokens": 0,
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["logprobs"]["token_logprobs"] == [None, -0.4]
    assert seen[0]["json"]["input_ids"] == [1, 2]
    assert seen[0]["json"]["sampling_params"]["max_new_tokens"] == 0
    assert seen[0]["json"]["return_logprob"] is True
