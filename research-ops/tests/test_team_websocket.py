import asyncio
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect
from test_team_accounts import COOKIE, ORIGIN, login
from test_team_accounts import team as team_fixture

team = team_fixture


@pytest.mark.parametrize("target", ["branches/test", "terminal"])
def test_live_proxy_routes_and_revokes(team, target):
    client, store, _ = team
    account = store.account("alice")
    from pathlib import Path

    socket = Path(account["worker_socket"])
    socket.parent.mkdir(parents=True)
    worker = FastAPI()

    @worker.websocket("/api/ws/branches/{branch_id}")
    @worker.websocket("/api/ws/terminal")
    async def events(ws: WebSocket, branch_id: str = "terminal"):
        assert ws.headers["authorization"] == f"Bearer {account['worker_token']}"
        assert ws.query_params["token"] == account["worker_token"]
        assert ws.headers["origin"] == ORIGIN
        await ws.accept()
        await ws.send_text("alice:" + branch_id)
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass

    server = uvicorn.Server(uvicorn.Config(worker, uds=str(socket), log_level="error"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    try:
        for _ in range(1000):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        login(client)
        token = client.cookies.get(COOKIE)
        with client.websocket_connect(
            ORIGIN.replace("https:", "wss:") + "/api/ws/" + target,
            headers={"origin": ORIGIN},
        ) as ws:
            assert ws.receive_text() == "alice:" + target.split("/")[-1]
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
            store.logout(token)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_websocket_rejects_wrong_origin_and_missing_cookie(team):
    client, _, _ = team
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/api/ws/branches/test",
            headers={"origin": ORIGIN},
        ),
    ):
        pass
    login(client)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/api/ws/branches/test",
            headers={"origin": "https://evil.example"},
        ),
    ):
        pass
