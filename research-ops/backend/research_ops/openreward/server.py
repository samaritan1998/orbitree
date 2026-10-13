"""Small FastAPI/SSE server implementing the Open Reward Standard."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import inspect
import json
import os
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from .environment import ORSEnvironment, maybe_await
from .types import (
    CreateRequest,
    GetTaskRequest,
    HealthResponse,
    ListTasksRequest,
    NumTasksRequest,
    RunToolError,
    RunToolSuccess,
    SessionResponse,
    TaskRangeRequest,
    ToolCallRequest,
)


@dataclass
class _Session:
    sid: str
    env_name: str
    environment: ORSEnvironment
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task_results: dict[str, dict[str, Any]] = field(default_factory=dict)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {_json(data)}\n\n"


def _sse_raw(event: str, data: str) -> str:
    safe = str(data).replace("\r", "").replace("\n", "")
    return f"event: {event}\ndata: {safe}\n\n"


def _decode_secrets_header(value: str | None) -> dict[str, Any]:
    """Decode the optional ORS ``X-Secrets`` envelope without logging it."""

    if not value:
        return {}
    try:
        decoded = json.loads(base64.b64decode(value).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid X-Secrets header") from exc
    if not isinstance(decoded, Mapping):
        raise HTTPException(status_code=400, detail="invalid X-Secrets header")
    result: dict[str, Any] = {}
    for key, item in decoded.items():
        if isinstance(item, Mapping) and "value" in item:
            result[str(key)] = item["value"]
        else:
            result[str(key)] = item
    return result


def _split_payload(item: Any) -> dict[str, str]:
    if isinstance(item, str):
        split_type = item if item in {"train", "validation", "test"} else "validation"
        return {"name": item, "type": split_type}
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json"))
    if isinstance(item, Mapping):
        name = str(item.get("name") or "")
        split_type = str(item.get("type") or "validation")
        return {"name": name, "type": split_type}
    raise TypeError(f"unsupported split type: {type(item).__name__}")


class ORSServer:
    """Session manager and route factory for one ORS deployment."""

    def __init__(
        self,
        environments: Mapping[str, type[ORSEnvironment]],
        *,
        session_ttl_seconds: float = 900.0,
        api_key_env: str | None = None,
    ) -> None:
        if not environments:
            raise ValueError("at least one ORS environment is required")
        self.environments = {
            str(name).strip().lower(): value for name, value in environments.items()
        }
        for name, env_cls in self.environments.items():
            if not inspect.isclass(env_cls) or not issubclass(env_cls, ORSEnvironment):
                raise TypeError(f"environment {name!r} is not an ORSEnvironment class")
            if not env_cls.list_tools():
                raise ValueError(f"environment {name!r} has no shared tools")
            if not env_cls.list_splits():
                raise ValueError(f"environment {name!r} has no splits")
        self.session_ttl_seconds = max(float(session_ttl_seconds), 1.0)
        self.api_key_env = str(api_key_env or "").strip() or None
        self._reserved: set[str] = set()
        self._sessions: dict[str, _Session] = {}
        self._sessions_lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start background session cleanup (safe to call more than once)."""

        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap())

    async def stop(self) -> None:
        """Stop cleanup and tear down every live environment session."""

        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        async with self._sessions_lock:
            sessions = list(self._sessions)
        for sid in sessions:
            await self._delete(sid)

    def _environment_class(self, name: str | None) -> type[ORSEnvironment]:
        if name is None or not str(name).strip():
            return next(iter(self.environments.values()))
        env_cls = self.environments.get(str(name).strip().lower())
        if env_cls is None:
            raise HTTPException(status_code=404, detail=f"unknown environment {name!r}")
        return env_cls

    async def _get_session(self, sid: str | None) -> _Session:
        identifier = str(sid or "").strip()
        if not identifier:
            raise HTTPException(status_code=400, detail="X-Session-ID header is required")
        async with self._sessions_lock:
            session = self._sessions.get(identifier)
        if session is None:
            raise HTTPException(status_code=404, detail="active environment not found")
        session.last_activity = time.monotonic()
        return session

    async def _delete(self, sid: str) -> None:
        async with self._sessions_lock:
            session = self._sessions.pop(sid, None)
            self._reserved.discard(sid)
        if session is not None:
            with suppress(Exception):
                await maybe_await(session.environment.teardown())

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, max(self.session_ttl_seconds / 3.0, 1.0)))
            now = time.monotonic()
            async with self._sessions_lock:
                stale = [
                    sid
                    for sid, session in self._sessions.items()
                    if now - session.last_activity > self.session_ttl_seconds
                ]
            for sid in stale:
                await self._delete(sid)

    def create_app(self, *, title: str = "Research Ops OpenReward") -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            await self.start()
            try:
                yield
            finally:
                await self.stop()

        app = FastAPI(title=title, version="0.1.0", lifespan=lifespan)
        app.state.ors_server = self

        @app.middleware("http")
        async def authenticate(request: Request, call_next: Any) -> Any:
            if self.api_key_env and request.url.path not in {"/health", "/version"}:
                expected = os.environ.get(self.api_key_env, "").strip()
                if not expected:
                    return JSONResponse(
                        status_code=503,
                        content={"detail": "OpenReward credential is unavailable"},
                    )
                provided = request.headers.get("X-API-Key", "").strip()
                authorization = request.headers.get("Authorization", "").strip()
                if not provided and authorization.casefold().startswith("bearer "):
                    provided = authorization[7:].strip()
                if not provided or not hmac.compare_digest(provided, expected):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Unauthorized"},
                    )
            return await call_next(request)

        @app.exception_handler(ValueError)
        async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

        @app.get("/health", response_model=HealthResponse)
        async def health() -> HealthResponse:
            return HealthResponse()

        @app.get("/version")
        async def version() -> dict[str, str]:
            return {"version": "0.1.0", "protocol": "ors-http"}

        @app.get("/list_environments")
        async def list_environments() -> list[str]:
            return list(self.environments)

        @app.get("/{env_name}/tools")
        async def list_tools(env_name: str) -> dict[str, Any]:
            env_cls = self._environment_class(env_name)
            return {"tools": [item.model_dump(mode="json") for item in env_cls.list_tools()]}

        @app.get("/{env_name}/splits")
        async def list_splits(env_name: str) -> list[dict[str, Any]]:
            env_cls = self._environment_class(env_name)
            return [_split_payload(item) for item in env_cls.list_splits()]

        @app.post("/{env_name}/tasks")
        async def list_tasks(env_name: str, payload: ListTasksRequest) -> dict[str, Any]:
            env_cls = self._environment_class(env_name)
            tasks = await maybe_await(env_cls.list_tasks(payload.split))
            return {"tasks": [dict(item) for item in tasks], "env_name": env_cls.name()}

        @app.post("/{env_name}/num_tasks")
        async def num_tasks(env_name: str, payload: NumTasksRequest) -> dict[str, int]:
            env_cls = self._environment_class(env_name)
            return {"num_tasks": int(await env_cls.num_tasks(payload.split))}

        @app.post("/{env_name}/task")
        async def get_task(env_name: str, payload: GetTaskRequest) -> dict[str, Any]:
            env_cls = self._environment_class(env_name)
            try:
                task = await env_cls.get_task(payload.split, payload.index)
            except (IndexError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"task": task, "env_name": env_cls.name()}

        @app.post("/{env_name}/task_range")
        async def task_range(env_name: str, payload: TaskRangeRequest) -> dict[str, Any]:
            env_cls = self._environment_class(env_name)
            try:
                tasks = await env_cls.get_task_range(payload.split, payload.start, payload.stop)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"tasks": tasks, "env_name": env_cls.name()}

        @app.post("/create_session")
        async def create_session(request: Request) -> Any:
            sid = str(uuid4())
            async with self._sessions_lock:
                self._reserved.add(sid)
            if "text/event-stream" in request.headers.get("accept", ""):
                async def stream():
                    # The official async SDK uses this resumable SSE form;
                    # the HTTP reference clients commonly use JSON instead.
                    yield _sse_raw("task_id", sid)
                    yield _sse("end", {"sid": sid})

                return StreamingResponse(
                    stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            return SessionResponse(sid=sid)

        @app.post("/create", response_model=SessionResponse)
        async def create(
            payload: CreateRequest,
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
            x_secrets: str | None = Header(default=None, alias="X-Secrets"),
        ) -> SessionResponse:
            sid = str(x_session_id or "").strip()
            if not sid:
                raise HTTPException(status_code=400, detail="X-Session-ID header is required")
            async with self._sessions_lock:
                if sid in self._sessions:
                    raise HTTPException(status_code=400, detail="session already exists")
                # Accept a caller-supplied UUID for compatibility with clients
                # that skip /create_session, while still preventing accidental
                # reuse of an active/reserved ID.
                if sid in self._reserved:
                    self._reserved.remove(sid)
            env_cls = self._environment_class(payload.env_name)
            if payload.task_spec is not None:
                task_spec = dict(payload.task_spec)
            else:
                assert payload.split is not None and payload.index is not None
                try:
                    task_spec = await env_cls.get_task(payload.split, payload.index)
                except (IndexError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                secrets = _decode_secrets_header(x_secrets)
                # Explicit JSON body values win over the transport envelope;
                # neither is ever serialized into a response or task metadata.
                secrets.update(payload.secrets)
                environment = env_cls(task_spec=task_spec, secrets=secrets)
                await maybe_await(environment.setup())
            except HTTPException:
                raise
            except Exception as exc:
                # Do not include exception strings: adapters may accidentally
                # include a provider URL or secret in their error message.
                raise HTTPException(
                    status_code=500,
                    detail=f"environment setup failed: {type(exc).__name__}",
                ) from exc
            async with self._sessions_lock:
                self._sessions[sid] = _Session(
                    sid=sid,
                    env_name=env_cls.name().lower(),
                    environment=environment,
                )
            return SessionResponse(sid=sid)

        @app.post("/ping")
        async def ping(
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> dict[str, str]:
            await self._get_session(x_session_id)
            return {"status": "ok"}

        @app.post("/delete")
        async def delete(
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> SessionResponse:
            session = await self._get_session(x_session_id)
            await self._delete(session.sid)
            return SessionResponse(sid=session.sid)

        @app.post("/delete_session")
        async def delete_session(
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> SessionResponse:
            identifier = str(x_session_id or "").strip()
            if not identifier:
                raise HTTPException(status_code=400, detail="X-Session-ID header is required")
            await self._delete(identifier)
            return SessionResponse(sid=identifier)

        @app.get("/{env_name}/task_tools")
        async def task_tools(
            env_name: str,
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> dict[str, Any]:
            session = await self._get_session(x_session_id)
            self._assert_session_env(session, env_name)
            tools = await session.environment.all_tools()
            return {"tools": [item.model_dump(mode="json") for item in tools]}

        @app.get("/{env_name}/prompt")
        async def prompt(
            env_name: str,
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> list[dict[str, Any]]:
            # ORS resolves the environment from the session for prompt calls;
            # retain that behavior and only use env_name for route matching.
            del env_name
            session = await self._get_session(x_session_id)
            blocks = await session.environment.get_prompt()
            return [block.model_dump(mode="json") for block in blocks]

        @app.post("/{env_name}/call")
        async def call(
            env_name: str,
            payload: ToolCallRequest,
            x_session_id: str | None = Header(default=None, alias="X-Session-ID"),
        ) -> StreamingResponse:
            session = await self._get_session(x_session_id)
            self._assert_session_env(session, env_name)
            task_id = payload.task_id or str(uuid4())

            async def stream():
                yield _sse_raw("task_id", task_id)
                async with session.lock:
                    body = session.task_results.get(task_id)
                    if body is None:
                        try:
                            output = await session.environment.call_tool(
                                payload.name,
                                payload.input,
                                task_id=task_id,
                            )
                            body = RunToolSuccess(output=output).model_dump(mode="json")
                        except KeyError:
                            body = RunToolError(
                                error=f"{payload.name!r} is not a valid tool",
                                reason="not_found",
                            ).model_dump(mode="json")
                        except (TypeError, ValueError) as exc:
                            body = RunToolError(
                                error=f"tool input validation failed: {type(exc).__name__}",
                                reason="input_validation",
                            ).model_dump(mode="json")
                        except Exception as exc:
                            # Keep provider details out of the protocol response.
                            body = RunToolError(
                                error=f"tool execution failed: {type(exc).__name__}"
                            ).model_dump(mode="json")
                        session.task_results[task_id] = body
                yield _sse("end", body)

            return StreamingResponse(
                stream(),
                status_code=status.HTTP_200_OK,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return app

    @staticmethod
    def _assert_session_env(session: _Session, env_name: str) -> None:
        if session.env_name != str(env_name).strip().lower():
            raise HTTPException(status_code=404, detail="environment mismatch")


def create_openreward_app(
    environments: Mapping[str, type[ORSEnvironment]],
    *,
    session_ttl_seconds: float = 900.0,
    api_key_env: str | None = None,
    title: str = "Research Ops OpenReward",
) -> FastAPI:
    """Create a standalone ORS-compatible FastAPI application."""

    return ORSServer(
        environments,
        session_ttl_seconds=session_ttl_seconds,
        api_key_env=api_key_env,
    ).create_app(title=title)
