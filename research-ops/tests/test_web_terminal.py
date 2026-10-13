import asyncio
import shutil
from pathlib import Path

import pytest
from research_ops.services.web_terminal import TerminalError, WebTerminal, terminal_key


@pytest.fixture
def terminal(settings, tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required for real PTY integration tests")
    settings.web_terminal_enabled = True
    settings.web_terminal_cwd = tmp_path
    settings.web_terminal_shell = "/bin/sh"
    settings.infoflow_public_base_url = "https://ops.example"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROPS_ONEAPI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    return WebTerminal(settings)


async def output_until(queue, expected, seconds=3):
    text = ""
    async with asyncio.timeout(seconds):
        while expected not in text:
            message = await queue.get()
            if message["type"] == "output":
                text += message["data"]
    return text


def test_unlock_is_private_and_not_generated_by_web_status(terminal):
    assert not terminal.authenticated({})
    assert terminal_key(terminal.settings) is None
    key = terminal_key(terminal.settings, create=True)
    path = terminal.settings.runtime_data_dir / "web-terminal/unlock.key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert terminal_key(terminal.settings, create=True) == key
    assert terminal.authenticated({"x-rops-terminal-key": key})
    assert not terminal.authenticated({"x-rops-terminal-key": "wrong"})
    assert not terminal.origin_allowed("https://evil.example", {})
    assert terminal.origin_allowed("https://ops.example", {})
    assert not terminal.origin_allowed(None, {})


def test_one_time_tickets_are_origin_bound_and_credentials_not_in_shell(terminal):
    ticket = terminal.ticket("https://ops.example")
    assert not terminal.consume_ticket(ticket, "https://evil.example")
    ticket = terminal.ticket("https://ops.example")
    assert terminal.consume_ticket(ticket, "https://ops.example")
    assert not terminal.consume_ticket(ticket, "https://ops.example")
    env = terminal.environment()
    assert "ROPS_ONEAPI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == str(terminal.cwd)


@pytest.mark.asyncio
async def test_real_shell_resize_interrupt_and_service_restart(terminal):
    second = None
    try:
        client, queue, _ = await terminal.attach(100, 24)
        await terminal.write(client, "stty size\r")
        assert "24 100" in await output_until(queue, "24 100")
        terminal.resize(80, 31)
        await terminal.write(client, "stty size\r")
        assert "31 80" in await output_until(queue, "31 80")
        await terminal.write(client, "export ROPS_PTY_TEST=survived; sleep 30\r")
        await output_until(queue, "sleep 30")
        await terminal.interrupt()
        await terminal.write(client, "printf '%s\\n' \"$ROPS_PTY_TEST\"\r")
        assert "survived" in await output_until(queue, "survived")
        await terminal.close()
        second = WebTerminal(terminal.settings)
        assert (await second.status(True))["active"]
        client, queue, _ = await second.attach(90, 25)
        await second.write(client, "printf '%s\\n' \"$ROPS_PTY_TEST\"\r")
        assert "survived" in await output_until(queue, "survived")
        await second.terminate()
        assert not (await second.status(True))["active"]
    finally:
        await (second or terminal).terminate()
        await terminal.close()


@pytest.mark.asyncio
async def test_second_browser_takes_control_without_a_second_shell(terminal):
    try:
        first, queue, _ = await terminal.attach(100, 24)
        pid = terminal.process.pid
        second, _, _ = await terminal.attach(100, 24)
        assert first != second
        assert terminal.process.pid == pid
        events = []
        while not queue.empty():
            events.append(queue.get_nowait()["type"])
        assert "replaced" in events
        with pytest.raises(TerminalError, match="no longer controls"):
            await terminal.write(first, "should not run\r")
        with pytest.raises(TerminalError, match="too large"):
            await terminal.write(second, "x" * 17000)
        terminal.detach(first)
        assert terminal.client == second
    finally:
        await terminal.terminate()


@pytest.mark.asyncio
async def test_no_shell_for_disabled_or_unauthenticated_requests(terminal):
    status = await terminal.status(False)
    assert status["requires_unlock"]
    assert "cwd" not in status
    assert terminal.process is None
    terminal.settings.web_terminal_enabled = False
    with pytest.raises(TerminalError, match="disabled"):
        await terminal.attach(100, 24)
    assert terminal.process is None


def test_private_key_rejects_symlinks_and_public_permissions(terminal):
    terminal_key(terminal.settings, create=True)
    path = terminal.settings.runtime_data_dir / "web-terminal/unlock.key"
    path.chmod(0o644)
    with pytest.raises(TerminalError, match="private"):
        terminal_key(terminal.settings)
    path.unlink()
    path.symlink_to(Path(__file__))
    with pytest.raises(OSError):
        terminal_key(terminal.settings)


def test_stream_queue_is_bounded(terminal):
    terminal.client = "client"
    terminal.queue = asyncio.Queue(maxsize=2)
    queue = terminal.queue
    terminal._emit({"type": "output", "data": "one"})
    terminal._emit({"type": "output", "data": "two"})
    terminal._emit({"type": "output", "data": "three"})
    assert queue.get_nowait() == {"type": "overflow"}
    assert terminal.client is None
    assert terminal.queue is None


@pytest.mark.asyncio
async def test_keystroke_echoes_are_batched_before_backpressure(terminal):
    terminal.client = "client"
    terminal.queue = asyncio.Queue(maxsize=64)
    for _ in range(200):
        terminal._buffer_output("x")
    assert terminal.queue.empty()
    message = await asyncio.wait_for(terminal.queue.get(), 1)
    assert message == {"type": "output", "data": "x" * 200}
    assert terminal.client == "client"
    assert terminal.flush_handle is None
    terminal._buffer_output("last output")
    terminal._emit({"type": "exit", "code": 0})
    assert terminal.queue.get_nowait()["data"] == "last output"
    assert terminal.queue.get_nowait()["type"] == "exit"


def test_api_rejects_missing_key_bad_origin_and_ticket_reuse(client, settings):
    from starlette.websockets import WebSocketDisconnect

    terminal = client.app.state.rops.web_terminal
    settings.web_terminal_enabled = True
    settings.infoflow_public_base_url = "https://ops.example"
    key = terminal_key(settings, create=True)
    headers = {"x-rops-terminal-key": key, "origin": "https://ops.example"}
    assert client.get("/api/terminal").json()["requires_unlock"]
    assert client.post("/api/terminal/connect").status_code == 403
    assert client.post("/api/terminal/stop").status_code == 403
    assert client.post("/api/terminal/interrupt").status_code == 403
    assert (
        client.post(
            "/api/terminal/connect", headers={**headers, "origin": "https://evil.example"}
        ).status_code
        == 403
    )
    token = client.post("/api/terminal/connect", headers=headers).json()["ticket"]
    assert terminal.consume_ticket(token, "https://ops.example")
    with client.websocket_connect(
        "/api/ws/terminal", headers={"origin": "https://ops.example"}
    ) as ws:
        ws.send_json({"ticket": token, "cols": 80, "rows": 24})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert terminal.process is None
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/ws/terminal", headers={"origin": "https://evil.example"}),
    ):
        pass
