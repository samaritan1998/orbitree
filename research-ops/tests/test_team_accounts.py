from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from research_ops.team.gateway import COOKIE, TeamConfig, create_team_app
from research_ops.team.security import WorkerTokenMiddleware
from research_ops.team.store import TeamStore

PASSWORD = "a long test password 123"
ORIGIN = "https://research.example.test"


@pytest.fixture
def team(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("test frontend")
    seen = []

    def transport(account):
        async def handle(request):
            seen.append((account["username"], request))

            async def body():
                yield json.dumps(
                    {"workspace": account["username"], "path": request.url.path}
                ).encode()

            return httpx.Response(200, content=body(), headers={"content-type": "application/json"})

        return httpx.MockTransport(handle)

    app = create_team_app(
        TeamConfig(tmp_path / "team", frontend, ORIGIN), transport_factory=transport
    )
    store = app.state.team_store
    for name in ("owner", "alice", "bobby"):
        store.add_account(name, name.title(), admin=name == "owner")
        store.activate(store.invitation(name, "cli"), PASSWORD)
    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        yield client, store, seen


def login(client, username="alice"):
    result = client.post("/api/team/login", json={"username": username, "password": PASSWORD})
    assert result.status_code == 200
    return result


def test_gateway_closed_without_session(team):
    client, _, _ = team
    assert client.get("/api/branches/private").status_code == 401
    assert client.get("/api/team/session").json() == {"enabled": True, "user": None}
    assert client.get("/").status_code == 200


def test_cookie_security_and_secret_redaction(team):
    client, _, _ = team
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    info = client.get("/api/team/session").json()
    assert info["user"]["username"] == "alice"
    assert "worker_token" not in str(info) and "password_hash" not in str(info)
    assert response.headers["cache-control"] == "no-store"


def test_proxy_uses_cookie_identity_not_client_headers_or_query(team):
    client, store, seen = team
    login(client)
    result = client.get(
        "/api/branches/foreign?user=bobby",
        headers={
            "Authorization": "Bearer forged",
            "X-Rops-User": "bobby",
        },
    )
    assert result.json()["workspace"] == "alice"
    account, upstream = seen[-1]
    assert account == "alice"
    assert upstream.headers["authorization"] == f"Bearer {store.account('alice')['worker_token']}"
    assert "cookie" not in upstream.headers and "x-rops-user" not in upstream.headers
    login(client, "bobby")
    assert client.get("/api/programs").json()["workspace"] == "bobby"


@pytest.mark.parametrize("path", ["/api/team/accounts", "/api/team/audit"])
def test_member_cannot_read_administration(team, path):
    client, _, _ = team
    login(client)
    assert client.get(path).status_code == 403


def test_member_cannot_invite_or_disable(team):
    client, _, _ = team
    login(client)
    assert client.post("/api/team/accounts/bobby/invite", json={}).status_code == 403
    assert client.put("/api/team/accounts/bobby", json={"enabled": False}).status_code == 403


def test_origin_checks_apply_to_login_logout_and_worker_writes(team):
    client, _, seen = team
    login(client)
    for path in ("/api/team/logout", "/api/branches/x/conversation", "/api/team/login"):
        assert (
            client.post(path, json={}, headers={"origin": "https://evil.test"}).status_code == 403
        )
    assert not seen


def test_logout_revokes_server_session(team):
    client, store, _ = team
    login(client)
    token = client.cookies.get(COOKIE)
    assert store.session(token)
    assert client.post("/api/team/logout", json={}).status_code == 200
    assert not store.session(token)
    assert client.get("/api/today").status_code == 401


def test_password_change_revokes_all_sessions(team):
    client, store, _ = team
    login(client)
    older = store.new_session("alice")
    response = client.post(
        "/api/team/password",
        json={
            "current_password": PASSWORD,
            "new_password": "changed long password 456",
        },
    )
    assert response.status_code == 200
    assert store.session(older) is None
    assert not store.check_password("alice", PASSWORD)


def test_admin_recovery_preserves_worker_identity(team):
    _, store, _ = team
    before = store.account("alice")
    token = store.new_session("alice")
    store.reset_login("alice")
    assert not store.session(token)
    assert not store.check_password("alice", PASSWORD)
    after = store.account("alice")
    assert after["worker_socket"] == before["worker_socket"]
    assert after["worker_token"] == before["worker_token"]
    store.activate(store.invitation("alice", "cli"), "new recovered password")
    assert store.check_password("alice", "new recovered password")


def test_disable_revokes_invites_and_existing_sessions(team):
    client, store, _ = team
    token = store.new_session("bobby")
    login(client, "owner")
    assert client.put("/api/team/accounts/bobby", json={"enabled": False}).status_code == 200
    assert store.session(token) is None
    assert not store.check_password("bobby", PASSWORD)
    assert client.put("/api/team/accounts/owner", json={"enabled": False}).status_code == 400


def test_single_use_invitation_and_hash_storage(team):
    client, store, _ = team
    store.add_account("carol", "Carol")
    login(client, "owner")
    response = client.post("/api/team/accounts/carol/invite", json={})
    token = response.json()["url"].split("#invite=")[1]
    assert (
        client.post("/api/team/activate", json={"token": token, "password": PASSWORD}).status_code
        == 200
    )
    assert (
        client.post("/api/team/activate", json={"token": token, "password": PASSWORD}).status_code
        == 400
    )
    with sqlite3.connect(store.path) as db:
        raw = " ".join(db.iterdump())
    assert token not in raw and PASSWORD not in raw


def test_login_rate_limit_and_invalid_credentials(team):
    client, _, _ = team
    for _ in range(10):
        assert (
            client.post(
                "/api/team/login", json={"username": "nobody", "password": "bad"}
            ).status_code
            == 401
        )
    assert (
        client.post("/api/team/login", json={"username": "nobody", "password": "bad"}).status_code
        == 429
    )


def test_expired_session_and_invitation(team):
    _, store, _ = team
    token = store.new_session("alice")
    store.add_account("carol", "Carol")
    invite = store.invitation("carol", "owner")
    with store.connect() as db:
        db.execute("UPDATE sessions SET expires_at=0")
        db.execute("UPDATE invites SET expires_at=0")
    assert store.session(token) is None
    with pytest.raises(ValueError, match="expired"):
        store.activate(invite, PASSWORD)


@pytest.mark.parametrize("name", ["../root", "Root", "-admin", "a/b", "a", "x" * 29])
def test_account_name_is_not_a_path(tmp_path, name):
    with pytest.raises(ValueError):
        TeamStore(tmp_path).add_account(name, "Test")


def test_private_worker_reads_files_and_ws_require_token():
    app = FastAPI()

    @app.get("/{path:path}")
    def content(path):
        return {"path": path}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("authorized")

    app.add_middleware(WorkerTokenMiddleware, token="private-token")
    with TestClient(app) as client:
        for path in ("/api/programs", "/api/files/preview", "/", "/docs"):
            assert client.get(path).status_code == 401
            assert (
                client.get(path, headers={"Authorization": "Bearer private-token"}).status_code
                == 200
            )
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws?token=private-token"),
        ):
            pass
        with client.websocket_connect(
            "/ws", headers={"Authorization": "Bearer private-token"}
        ) as ws:
            assert ws.receive_text() == "authorized"


def test_no_remote_plaintext_login(tmp_path):
    with pytest.raises(ValueError):
        TeamConfig(tmp_path, tmp_path, "http://198.51.100.1:8030")
    TeamConfig(tmp_path, tmp_path, "http://127.0.0.1:8030")
