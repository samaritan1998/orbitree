from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from websockets.asyncio.client import unix_connect

from .provisioning import AccountProvisioner, SetupBusy, SetupFailed
from .store import SESSION_TTL, TeamStore

COOKIE = "rops_team_session"
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "authorization",
    "cookie",
    "set-cookie",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}


@dataclass(frozen=True)
class TeamConfig:
    root: Path
    frontend_dist: Path
    public_origin: str
    provision_release: Path | None = None

    def __post_init__(self) -> None:
        url = urlsplit(self.public_origin)
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (url.scheme != "https" and not (local and url.scheme == "http")) or (
            url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or not url.hostname
        ):
            raise ValueError(
                "Use an HTTPS origin without a path; HTTP is allowed only on localhost"
            )

    @property
    def secure_cookie(self) -> bool:
        return self.public_origin.startswith("https:")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=28)
    password: str = Field(min_length=1, max_length=256)


class Activation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=256)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AccountStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(pattern=r"^[a-z][a-z0-9_]{2,26}$")
    display_name: str = Field(min_length=1, max_length=160)


def create_team_app(config: TeamConfig, *, transport_factory=None) -> FastAPI:
    store = TeamStore(config.root)
    provisioner = (
        AccountProvisioner(store, config.provision_release) if config.provision_release else None
    )
    clients: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for client in clients.values():
            await client.aclose()

    app = FastAPI(title="Research Ops accounts", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.team_store = store

    def authenticated(request) -> dict:
        account = store.session(request.cookies.get(COOKIE, ""))
        if not account:
            raise HTTPException(401, "Sign in to Research Ops")
        return account

    def admin(request) -> dict:
        account = authenticated(request)
        if account["role"] != "admin":
            raise HTTPException(403, "Administrator access is required")
        return account

    def set_cookie(response: Response, username: str) -> None:
        response.set_cookie(
            COOKIE,
            store.new_session(username),
            max_age=SESSION_TTL,
            httponly=True,
            secure=config.secure_cookie,
            samesite="strict",
            path="/",
        )

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != config.public_origin:
                return JSONResponse({"detail": "Origin verification failed"}, status_code=403)
            if request.url.path.startswith("/api/team/"):
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return JSONResponse({"detail": "JSON request required"}, status_code=415)
                # Bound bodies before Pydantic/Argon2 processing, including chunked requests.
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16_384:
                        return JSONResponse({"detail": "Request too large"}, status_code=413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ValueError)
    async def invalid_request(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(SetupBusy)
    async def setup_busy(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SetupFailed)
    async def setup_failed(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/api/team/session")
    def me(request: Request):
        account = store.session(request.cookies.get(COOKIE, ""))
        result = {"enabled": True, "user": store.public(account) if account else None}
        if account and account["role"] == "admin":
            result["can_create_accounts"] = provisioner is not None and os.geteuid() == 0
        return result

    @app.post("/api/team/login")
    def login(payload: Login, request: Request, response: Response):
        peer = request.client.host if request.client else "unknown"
        if not store.rate_limit(f"login-ip:{peer}", 60) or not store.rate_limit(
            f"login-user:{payload.username}",
            10,
        ):
            raise HTTPException(429, "Too many sign-in attempts. Retry in five minutes")
        if not store.check_password(payload.username, payload.password):
            raise HTTPException(401, "Account or password is incorrect")
        set_cookie(response, payload.username)
        return {"ok": True}

    @app.post("/api/team/activate")
    def activate(payload: Activation, request: Request, response: Response):
        peer = request.client.host if request.client else "unknown"
        if not store.rate_limit(f"activate:{peer}", 20):
            raise HTTPException(429, "Too many attempts. Retry in five minutes")
        username = store.activate(payload.token, payload.password)
        set_cookie(response, username)
        return {"ok": True}

    @app.post("/api/team/logout")
    def logout(request: Request, response: Response):
        store.logout(request.cookies.get(COOKIE, ""))
        response.delete_cookie(
            COOKIE, path="/", secure=config.secure_cookie, httponly=True, samesite="strict"
        )
        return {"ok": True}

    @app.post("/api/team/password")
    def password(payload: PasswordChange, request: Request, response: Response):
        account = authenticated(request)
        if not store.rate_limit(f"password:{account['username']}", 10):
            raise HTTPException(429, "Too many attempts. Retry in five minutes")
        store.change_password(account["username"], payload.current_password, payload.new_password)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/team/accounts")
    def accounts(request: Request):
        admin(request)
        return store.accounts()

    def account_provisioner(request: Request) -> tuple[dict, AccountProvisioner]:
        actor = admin(request)
        if provisioner is None or os.geteuid() != 0:
            raise HTTPException(503, "尚未配置用户实例模板，请联系管理员配置账号网关的 --release。")
        return actor, provisioner

    def invitation_result(username: str, actor: str) -> dict:
        token = store.invitation(username, actor)
        return {"url": f"{config.public_origin}/#invite={token}", "expires_in": 86400}

    @app.post("/api/team/accounts", status_code=201)
    def create_account(payload: AccountCreate, request: Request):
        actor, manager = account_provisioner(request)
        account = manager.create(payload.username, payload.display_name, actor["username"])
        return {"account": account, **invitation_result(payload.username, actor["username"])}

    @app.post("/api/team/accounts/{username}/setup")
    def retry_account_setup(username: str, request: Request):
        actor, manager = account_provisioner(request)
        account = manager.retry(username, actor["username"])
        return {"account": account, **invitation_result(username, actor["username"])}

    @app.post("/api/team/accounts/{username}/invite")
    def invite(username: str, request: Request):
        actor = admin(request)
        # Fragment is never transmitted in HTTP logs or Referer headers.
        return invitation_result(username, actor["username"])

    @app.put("/api/team/accounts/{username}")
    def enable(username: str, payload: AccountStatus, request: Request):
        actor = admin(request)
        store.set_enabled(username, payload.enabled, actor["username"])
        return {"ok": True}

    @app.get("/api/team/audit")
    def audit(request: Request):
        admin(request)
        with store.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
            ]

    def client_for(account: dict) -> httpx.AsyncClient:
        username = account["username"]
        if username not in clients:
            transport = (
                transport_factory(account)
                if transport_factory
                else httpx.AsyncHTTPTransport(
                    uds=account["worker_socket"],
                    retries=0,
                )
            )
            clients[username] = httpx.AsyncClient(
                transport=transport,
                base_url="http://worker",
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(120, connect=5),
            )
        return clients[username]

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        if path.startswith("team/"):
            raise HTTPException(404, "Account endpoint not found")
        account = await asyncio.to_thread(authenticated, request)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS and not key.lower().startswith("x-rops-")
        }
        headers["authorization"] = f"Bearer {account['worker_token']}"
        client = client_for(account)
        url = httpx.URL("http://worker" + request.url.path).copy_with(
            query=request.url.query.encode(),
        )
        try:
            upstream = await client.send(
                client.build_request(
                    request.method, url, headers=headers, content=request.stream()
                ),
                stream=True,
            )
        except httpx.HTTPError:
            raise HTTPException(
                503, "Your workspace is unavailable. Ask an administrator to start it"
            ) from None
        response_headers = {
            key: value for key, value in upstream.headers.items() if key.lower() not in HOP_HEADERS
        }
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    @app.websocket("/api/ws/branches/{branch_id}")
    @app.websocket("/api/ws/terminal")
    async def events(websocket: WebSocket, branch_id: str | None = None):
        token = websocket.cookies.get(COOKIE, "")
        account = await asyncio.to_thread(store.session, token)
        if not account or websocket.headers.get("origin") != config.public_origin:
            await websocket.close(code=4401)
            return
        query = dict(parse_qsl(websocket.url.query))
        query["token"] = account["worker_token"]
        path = f"branches/{branch_id}" if branch_id is not None else "terminal"
        uri = f"ws://worker/api/ws/{path}?{urlencode(query)}"
        try:
            async with unix_connect(
                account["worker_socket"],
                uri=uri,
                additional_headers={
                    "Authorization": f"Bearer {account['worker_token']}",
                    "Origin": config.public_origin,
                },
                open_timeout=10,
                max_size=8 * 1024 * 1024,
            ) as upstream:
                await websocket.accept()

                async def to_browser():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def to_worker():
                    while True:
                        await upstream.send(await websocket.receive_text())

                async def revoked():
                    while await asyncio.to_thread(store.session, token):
                        await asyncio.sleep(5)

                tasks = [asyncio.create_task(fn()) for fn in (to_browser, to_worker, revoked)]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1013, reason="Workspace connection unavailable")
            return
        with suppress(Exception):
            await websocket.close(code=1000)

    @app.get("/{path:path}")
    def frontend(path: str):
        root = config.frontend_dist.resolve()
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        if path.startswith(("assets/", "themes/")):
            raise HTTPException(404, "Asset not found")
        if not (root / "index.html").is_file():
            raise HTTPException(503, "Frontend has not been built")
        return FileResponse(root / "index.html")

    return app
