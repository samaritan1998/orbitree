from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class EventHub:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, branch_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[branch_id].add(websocket)

    async def disconnect(self, branch_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(branch_id)
            if connections is None:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(branch_id, None)

    async def publish(self, branch_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections.get(branch_id, ()))
        stale: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(event)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(branch_id, websocket)
