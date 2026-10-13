from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from research_ops.infoflow import (
    InfoflowConfigurationError,
    InfoflowDeliveryBlockedError,
    InfoflowMessageClient,
)


def test_infoflow_client_authenticates_and_reuses_token() -> None:
    environ = {
        "TEST_INFOFLOW_KEY": "app-key",
        "TEST_INFOFLOW_SECRET": "app-secret",
    }
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/auth/app_access_token"):
            assert body == {
                "app_key": "app-key",
                "app_secret": hashlib.md5(
                    b"app-secret", usedforsecurity=False
                ).hexdigest(),
            }
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "code": "ok",
                    "data": {"app_access_token": "runtime-token", "expire": 7200},
                },
            )
        assert request.headers["Authorization"] == "Bearer-runtime-token"
        assert request.headers["X-OP-ACCESS-CHANNEL"] == "skill"
        assert body["touser"] == "alice"
        assert body["msgtype"] == "text"
        return httpx.Response(200, json={"code": "ok", "data": {"msgkey": "msg-1"}})

    client = InfoflowMessageClient(
        base_url="https://infoflow.example",
        app_key_env="TEST_INFOFLOW_KEY",
        app_secret_env="TEST_INFOFLOW_SECRET",
        environ=environ,
        transport=httpx.MockTransport(handler),
    )

    assert client.configured is True
    assert client.send_text("alice", "First").message_id == "msg-1"
    assert client.send_text("alice", "Second").message_id == "msg-1"
    assert [request.url.path for request in calls].count(
        "/api/v1/auth/app_access_token"
    ) == 1


def test_infoflow_client_requires_referenced_credentials() -> None:
    client = InfoflowMessageClient(
        app_key_env="MISSING_INFOFLOW_KEY",
        app_secret_env="MISSING_INFOFLOW_SECRET",
        environ={},
    )

    assert client.configured is False
    with pytest.raises(InfoflowConfigurationError, match="MISSING_INFOFLOW_KEY"):
        client.send_text("alice", "Test")


def test_infoflow_client_reads_secure_skill_credentials_file(tmp_path) -> None:
    credentials = tmp_path / "infoflow.env"
    credentials.write_text(
        "INFOFLOW_AK=file-key\nINFOFLOW_SK='file-secret'\n",
        encoding="utf-8",
    )
    credentials.chmod(0o600)
    client = InfoflowMessageClient(environ={}, credentials_file=credentials)

    assert client.configured is True


def test_infoflow_client_rejects_insecure_credentials_file(tmp_path) -> None:
    credentials = tmp_path / "infoflow.env"
    credentials.write_text("INFOFLOW_AK=key\nINFOFLOW_SK=secret\n", encoding="utf-8")
    credentials.chmod(0o644)
    client = InfoflowMessageClient(environ={}, credentials_file=credentials)

    with pytest.raises(InfoflowConfigurationError, match="group or other"):
        _ = client.configured


def test_infoflow_client_rejects_nested_business_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/app_access_token"):
            return httpx.Response(
                200,
                json={"code": "ok", "data": {"app_access_token": "token"}},
            )
        return httpx.Response(
            200,
            json={"code": "ok", "data": {"code": 50010, "message": "not allowed"}},
        )

    client = InfoflowMessageClient(
        environ={"KEY": "key", "SECRET": "secret"},
        app_key_env="KEY",
        app_secret_env="SECRET",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="not allowed"):
        client.send_text("alice", "Test")


def test_infoflow_client_sends_group_text() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/auth/app_access_token"):
            return httpx.Response(
                200,
                json={"code": "ok", "data": {"app_access_token": "token"}},
            )
        body = json.loads(request.content)
        assert request.url.path == "/api/v1/robot/msg/groupmsgsend"
        assert request.headers["Authorization"] == "Bearer-token"
        assert body["message"]["header"]["toid"] == 12345678
        assert body["message"]["header"]["totype"] == "GROUP"
        assert body["message"]["body"] == [{"type": "TEXT", "content": "LTP running"}]
        return httpx.Response(200, json={"code": "ok", "data": {"msgId": "group-1"}})

    client = InfoflowMessageClient(
        environ={"KEY": "key", "SECRET": "secret"},
        app_key_env="KEY",
        app_secret_env="SECRET",
        transport=httpx.MockTransport(handler),
    )

    assert client.send_group_text("12345678", "LTP running").message_id == "group-1"
    assert len(calls) == 2


def test_infoflow_client_rejects_non_numeric_group_id() -> None:
    client = InfoflowMessageClient(environ={"KEY": "key", "SECRET": "secret"})

    with pytest.raises(ValueError, match="numeric"):
        client.send_group_text("group-12345678", "LTP running")


def test_infoflow_client_marks_missing_group_membership_as_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/app_access_token"):
            return httpx.Response(
                200,
                json={"code": "ok", "data": {"app_access_token": "token"}},
            )
        return httpx.Response(
            200,
            json={"code": "ok", "data": {"errcode": 40001, "errmsg": "group not follow"}},
        )

    client = InfoflowMessageClient(
        environ={"KEY": "key", "SECRET": "secret"},
        app_key_env="KEY",
        app_secret_env="SECRET",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(InfoflowDeliveryBlockedError, match="group not follow"):
        client.send_group_text("12345678", "LTP running")
