from __future__ import annotations

import secrets

from starlette.responses import JSONResponse


class WorkerTokenMiddleware:
    """Protect reads, files, static resources and WebSockets, not only mutations."""

    def __init__(self, app, token: str) -> None:
        if not token:
            raise ValueError("Private worker token is required")
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
        if not secrets.compare_digest(supplied, self.expected):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
            else:
                await JSONResponse({"detail": "Unauthorized"}, status_code=401)(
                    scope, receive, send
                )
            return
        await self.app(scope, receive, send)
