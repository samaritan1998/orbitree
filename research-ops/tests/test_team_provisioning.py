from __future__ import annotations

import fcntl
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from research_ops.team import provisioning
from research_ops.team.gateway import TeamConfig, create_team_app
from research_ops.team.store import TeamStore

ORIGIN = "https://research.example.test"
PASSWORD = "a long test password 123"


@pytest.fixture
def setup_team(tmp_path, monkeypatch):
    root = tmp_path / "team"
    release = tmp_path / "release"
    calls = []
    monkeypatch.setattr(provisioning, "validate_release", lambda path: path)
    monkeypatch.setattr(provisioning.os, "geteuid", lambda: 0)
    monkeypatch.setattr(provisioning, "provision", lambda *args: calls.append(("provision", args)))
    monkeypatch.setattr(provisioning, "worker_running", lambda *args: False)
    monkeypatch.setattr(provisioning, "start_worker", lambda *args: calls.append(("start", args)))
    monkeypatch.setattr(
        provisioning,
        "wait_for_worker",
        lambda account: calls.append(("health", account["username"])),
    )
    app = create_team_app(TeamConfig(root, release / "web/dist", ORIGIN, release))
    store = app.state.team_store
    for name in ("owner", "alice"):
        store.add_account(name, name.title(), admin=name == "owner")
        store.activate(store.invitation(name, "cli"), PASSWORD)
    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        assert (
            client.post(
                "/api/team/login", json={"username": "owner", "password": PASSWORD}
            ).status_code
            == 200
        )
        yield client, store, calls


def create(client, **extra):
    return client.post(
        "/api/team/accounts", json={"username": "carol", "display_name": " Carol ", **extra}
    )


def test_web_create_isolated_account(setup_team):
    client, store, calls = setup_team
    result = create(client)
    assert result.status_code == 201, result.text
    account = result.json()["account"]
    assert account["role"] == "member"
    assert account["display_name"] == "Carol"
    assert not account["activated"]
    assert account["setup"] == {"status": "ready", "error": None}
    assert "worker_token" not in result.text and "password_hash" not in result.text
    assert calls[0][1][-1] == []  # No shared paths or inherited model identities.
    assert [call[0] for call in calls] == ["provision", "start", "health"]
    assert client.get("/api/team/session").json()["can_create_accounts"]
    with store.connect() as db:
        actions = [
            tuple(row) for row in db.execute("SELECT actor,action FROM audit WHERE subject='carol'")
        ]
    assert ("owner", "account.created") in actions
    assert ("owner", "account.setup.ready") in actions
    token = result.json()["url"].split("#invite=")[1]
    assert (
        client.post("/api/team/activate", json={"token": token, "password": PASSWORD}).status_code
        == 200
    )
    assert (
        client.post("/api/team/activate", json={"token": token, "password": PASSWORD}).status_code
        == 400
    )


def test_create_member_and_origin_denied(setup_team):
    client, store, calls = setup_team
    assert (
        client.post(
            "/api/team/accounts",
            json={"username": "carol", "display_name": "Carol"},
            headers={"origin": "https://evil.test"},
        ).status_code
        == 403
    )
    client.post("/api/team/login", json={"username": "alice", "password": PASSWORD})
    assert create(client).status_code == 403
    assert client.post("/api/team/accounts/carol/setup", json={}).status_code == 403
    assert not store.account("carol") and not calls


@pytest.mark.parametrize(
    "extra",
    [
        {"username": "../root"},
        {"username": "a"},
        {"display_name": "  "},
        {"admin": True},
        {"release": "/root"},
        {"shared_read": ["/root"]},
        {"command": "anything"},
    ],
)
def test_create_input_boundary(setup_team, extra):
    client, store, calls = setup_team
    assert create(client, **extra).status_code == 422
    assert not calls and not store.account("carol")


def test_duplicate_create_never_reprovisions(setup_team):
    client, store, calls = setup_team
    assert create(client).status_code == 201
    token = store.account("carol")["worker_token"]
    calls.clear()
    result = create(client)
    assert result.status_code == 409
    assert not calls and store.account("carol")["worker_token"] == token


def test_setup_failure_and_retry(setup_team, monkeypatch):
    client, store, calls = setup_team
    monkeypatch.setattr(
        provisioning,
        "wait_for_worker",
        lambda account: (_ for _ in ()).throw(TimeoutError("private internals")),
    )
    failed = create(client)
    assert failed.status_code == 503
    assert "private internals" not in failed.text
    assert store.setup("carol")["status"] == "error"
    assert client.post("/api/team/accounts/carol/invite", json={}).status_code == 400
    listing = client.get("/api/team/accounts").json()
    assert next(a for a in listing if a["username"] == "carol")["setup"]["error"]
    original = store.account("carol")["worker_token"]
    monkeypatch.setattr(provisioning, "worker_running", lambda *args: True)
    monkeypatch.setattr(provisioning, "wait_for_worker", lambda account: None)
    calls.clear()
    recovered = client.post("/api/team/accounts/carol/setup", json={})
    assert recovered.status_code == 200
    assert recovered.json()["account"]["setup"]["status"] == "ready"
    assert store.account("carol")["worker_token"] == original
    assert [call[0] for call in calls] == ["provision"]


def test_busy_setup_is_not_duplicated(setup_team):
    client, store, calls = setup_team
    lock = store.root / "control/carol.setup.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert create(client).status_code == 409
    assert not calls and not store.account("carol")


def test_setup_restart_recovers_pending(setup_team):
    client, store, _ = setup_team
    store.add_account("carol", "Carol", actor="owner", setup=True)
    store.set_setup("carol", "provisioning", "owner")
    result = client.post("/api/team/accounts/carol/setup", json={})
    assert result.status_code == 200
    assert store.setup("carol")["status"] == "ready"


def test_setup_never_changes_activated_user(setup_team):
    client, store, calls = setup_team
    store.add_account("carol", "Carol", setup=True)
    store.set_setup("carol", "ready", "owner")
    store.activate(store.invitation("carol", "owner"), PASSWORD)
    assert client.post("/api/team/accounts/carol/setup", json={}).status_code == 400
    assert not calls


def test_create_requires_configured_template(tmp_path):
    app = create_team_app(TeamConfig(tmp_path, tmp_path, ORIGIN))
    store = app.state.team_store
    store.add_account("owner", "Owner", admin=True)
    store.activate(store.invitation("owner", "cli"), PASSWORD)
    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        client.post("/api/team/login", json={"username": "owner", "password": PASSWORD})
        assert not client.get("/api/team/session").json()["can_create_accounts"]
        assert create(client).status_code == 503
        assert not store.account("carol")


def test_worker_health_uses_private_socket(monkeypatch):
    requests = []

    def transport(*, uds):
        assert uds == "/private/alice/run/api.sock"

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "ok"})

        return httpx.MockTransport(handle)

    monkeypatch.setattr(provisioning.httpx, "HTTPTransport", transport)
    provisioning.wait_for_worker(
        {"worker_socket": "/private/alice/run/api.sock", "worker_token": "worker-private"}
    )
    assert requests[0].headers["authorization"] == "Bearer worker-private"


def test_invitation_blocked_until_setup_ready(tmp_path):
    store = TeamStore(tmp_path)
    store.add_account("alice", "Alice", setup=True)
    for status in ("pending", "provisioning", "starting", "error"):
        store.set_setup("alice", status, "owner")
        with pytest.raises(ValueError, match="not ready"):
            store.invitation("alice", "owner")
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM invites").fetchone()[0] == 0
    assert "worker_token" not in json.dumps(store.accounts())
