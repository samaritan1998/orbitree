from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from research_ops.services.web_terminal import TerminalError, WebTerminal


class TerminalSize(BaseModel):
    cols: int = Field(default=100, ge=20, le=500)
    rows: int = Field(default=30, ge=5, le=200)


def terminal_router(terminal: WebTerminal) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["terminal"])

    def authorize(request: Request):
        if not terminal.settings.web_terminal_enabled:
            raise HTTPException(503, "Web terminal is disabled")
        if not terminal.authenticated(request.headers):
            raise HTTPException(403, "Terminal is locked")
        if not terminal.origin_allowed(request.headers.get("origin"), request.headers):
            raise HTTPException(403, "Terminal origin is not allowed")

    @router.get("/terminal")
    async def terminal_status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await terminal.status(terminal.authenticated(request.headers))

    @router.post("/terminal/connect")
    async def connect(request: Request, response: Response):
        authorize(request)
        response.headers["Cache-Control"] = "no-store"
        return {"ticket": terminal.ticket(request.headers.get("origin", ""))}

    @router.post("/terminal/stop")
    async def stop(request: Request):
        authorize(request)
        await terminal.terminate()
        return await terminal.status(True)

    @router.post("/terminal/interrupt")
    async def interrupt(request: Request):
        authorize(request)
        await terminal.interrupt()
        return {"accepted": True}

    @router.websocket("/ws/terminal")
    async def terminal_stream(websocket: WebSocket):
        origin = websocket.headers.get("origin", "")
        if not terminal.settings.web_terminal_enabled or not terminal.origin_allowed(
            origin, websocket.headers
        ):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        client, tasks = None, []
        try:
            first = await asyncio.wait_for(websocket.receive_json(), 5)
            if not isinstance(first, dict) or not terminal.consume_ticket(
                str(first.get("ticket", "")), origin
            ):
                await websocket.close(code=4401)
                return
            size = TerminalSize.model_validate(first)
            client, queue, history = await terminal.attach(size.cols, size.rows)
            await websocket.send_json(
                {"type": "ready", "history": history, "terminal": await terminal.status(True)}
            )
            acknowledged = asyncio.Event()
            sequence = 0

            async def output():
                nonlocal sequence
                while True:
                    message = await queue.get()
                    if message["type"] == "output":
                        sequence += 1
                        message = {**message, "seq": sequence}
                        acknowledged.clear()
                    await asyncio.wait_for(websocket.send_json(message), 10)
                    if message["type"] == "output":
                        await asyncio.wait_for(acknowledged.wait(), 10)
                    if message["type"] in {"exit", "replaced", "overflow", "disconnected"}:
                        return

            async def incoming():
                while True:
                    message = await websocket.receive_json()
                    if not isinstance(message, dict) or client != terminal.client:
                        raise TerminalError("Terminal connection changed")
                    kind = message.get("type")
                    if kind == "input" and isinstance(message.get("data"), str):
                        await asyncio.wait_for(terminal.write(client, message["data"]), 5)
                    elif kind == "resize":
                        size = TerminalSize.model_validate(message)
                        terminal.resize(size.cols, size.rows)
                    elif kind == "ping":
                        terminal._emit({"type": "pong"})
                    elif kind == "ack":
                        if message.get("seq") == sequence:
                            acknowledged.set()
                    else:
                        raise TerminalError("Invalid terminal message")

            tasks = [asyncio.create_task(output()), asyncio.create_task(incoming())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except (TimeoutError, ValueError, TerminalError, OSError) as exc:
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "error", "message": str(exc)[:300]})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if client:
                terminal.detach(client)
            with contextlib.suppress(Exception):
                await websocket.close()

    return router
