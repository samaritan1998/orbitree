#!/usr/bin/env python3
"""Real AgentOS-backed canary runtime for the ORS SWE/PPT/ToolUse bridges.

This is intentionally a small sidecar, not a replacement for Harbor/SWE-ReX
or SlideForge.  SWE and PPT sessions are backed by the evaluation AgentOS
sandbox supplied by ``miaoda-agent-sdk``.  ToolUse is an in-process registry
whose state is reset for every session and whose verifier is executed on the
terminal call.  The sidecar speaks the versioned HTTP contract consumed by
``research_ops.openreward.service_backends.RemoteServiceBackend``.

The SDK is loaded from the environment in which this script is launched.  In
the current canary setup that is the inspected miaoda-agent-sdk virtualenv;
the SDK reads its bearer/JWT material from its own configuration and this
script never prints or persists those values.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

try:  # Imported lazily by the command in normal use, but useful for typing.
    from miaoda_agent_sdk.core.sandbox import SandboxClient
except Exception:  # pragma: no cover - this process must run in the SDK env.
    SandboxClient = Any  # type: ignore[misc,assignment]


ENVIRONMENT = os.environ.get("AGENTOS_ENV", "evaluation").strip() or "evaluation"
RUNTIME_USER_ID = os.environ.get("AGENTOS_RUNTIME_USER_ID", "").strip()
AUTH_TOKEN = os.environ.get("ORS_CANARY_RUNTIME_TOKEN", "").strip()
MAX_COMMAND_SECONDS = int(os.environ.get("ORS_CANARY_MAX_COMMAND_SECONDS", "120"))

if MAX_COMMAND_SECONDS <= 0:
    raise RuntimeError("ORS_CANARY_MAX_COMMAND_SECONDS must be positive")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_path(value: Any, *, default: str) -> str:
    raw = _text(value) or default
    path = PurePosixPath(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=400, detail="workspace paths must be absolute")
    return str(path)


def _output(result: Any) -> str:
    return _text(getattr(result, "output", ""))


def _exit_code(result: Any) -> int:
    try:
        return int(getattr(result, "exit_code", 0))
    except (TypeError, ValueError):
        return 1


def _backend_result(
    *,
    text: str = "",
    reward: float | None = None,
    finished: bool = False,
    metadata: Mapping[str, Any] | None = None,
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "text": text,
        "reward": reward,
        "finished": finished,
        "metadata": dict(metadata or {}),
    }
    if images:
        result["images"] = images
    return {"output": result}


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "increment",
            "description": "Increase a numeric task state value.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "delta": {"type": "number"},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_value",
            "description": "Set a numeric task state value.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    ]


PPT_GENERATOR = r"""from pathlib import Path
import json
from pptx import Presentation

spec = json.loads(Path("/workspace/ors_ppt_spec.json").read_text(encoding="utf-8"))
slides = spec.get("slides") or [{"title": "OpenReward canary", "body": spec.get("text", "")}]
prs = Presentation()
for item in slides:
    item = item if isinstance(item, dict) else {"body": str(item)}
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = str(item.get("title", ""))
    slide.placeholders[1].text = str(item.get("body", item.get("content", "")))
output = str(spec.get("output_path") or "/workspace/deliverable.pptx")
Path(output).parent.mkdir(parents=True, exist_ok=True)
prs.save(output)
print(json.dumps({"path": output, "slides": len(prs.slides)}))
"""

PPT_READER = r"""from pathlib import Path
import json
from pptx import Presentation

path = Path(__import__("sys").argv[1])
prs = Presentation(str(path))
slides = []
for slide in prs.slides:
    texts = [shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)]
    slides.append(texts)
print(json.dumps({"path": str(path), "slides": len(prs.slides), "texts": slides,
                  "bytes": path.stat().st_size}, ensure_ascii=False))
"""


@dataclass
class Session:
    domain: str
    task_id: str
    task_spec: dict[str, Any]
    client: Any | None = None
    state: dict[str, Any] = field(default_factory=dict)


SESSIONS: dict[str, Session] = {}
LOCK = asyncio.Lock()
app = FastAPI(title="OpenReward AgentOS canary runtime")


def _check_auth(authorization: str | None) -> None:
    if not AUTH_TOKEN:
        return
    expected = f"Bearer {AUTH_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _new_sandbox() -> Any:
    if not RUNTIME_USER_ID:
        raise HTTPException(
            status_code=503,
            detail="AGENTOS_RUNTIME_USER_ID is not configured",
        )
    try:
        client = await asyncio.to_thread(
            SandboxClient,
            env=ENVIRONMENT,
            user_id=RUNTIME_USER_ID,
            retry=1,
            timeout=max(120, MAX_COMMAND_SECONDS),
        )
        await client.create()
        return client
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"AgentOS sandbox create failed: {type(exc).__name__}"
        ) from exc


async def _destroy_sandbox(client: Any | None) -> None:
    if client is None:
        return
    try:
        await client.delete()
    finally:
        with contextlib.suppress(Exception):
            client.close()


async def _run(client: Any, command: str, *, timeout: int | None = None) -> Any:
    limit = min(max(1, int(timeout or MAX_COMMAND_SECONDS)), MAX_COMMAND_SECONDS)
    try:
        return await client.run(command, timeout=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"AgentOS command failed: {type(exc).__name__}"
        ) from exc


async def _create_session(
    domain: str, task_id: str, task_spec: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    domain = domain.casefold()
    visible = dict(task_spec)
    session_id = f"ors-{domain}-{secrets.token_hex(10)}"
    session = Session(domain=domain, task_id=task_id, task_spec=visible)

    if domain in {"swe", "ppt"}:
        session.client = await _new_sandbox()
        if domain == "swe":
            root = "/workspace/ors_swe_task"
            await session.client.write(
                f"{root}/calc.py",
                "def add(a, b):\n    return a - b\n",
                create_dirs=True,
            )
            await session.client.write(
                f"{root}/test_calc.py",
                "import unittest\nfrom calc import add\n\n"
                "class TestCalc(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                create_dirs=True,
            )
            session.state["root"] = root
        else:
            await session.client.write(
                "/workspace/ors_ppt_make.py", PPT_GENERATOR, create_dirs=True
            )
            await session.client.write("/workspace/ors_ppt_read.py", PPT_READER, create_dirs=True)
            session.state["default_path"] = "/workspace/deliverable.pptx"
    else:
        initial = visible.get("initial_state")
        session.state["values"] = dict(initial) if isinstance(initial, Mapping) else {"count": 0}
        session.state["target"] = dict(visible.get("target_state") or {"count": 2})

    async with LOCK:
        SESSIONS[session_id] = session
    response: dict[str, Any] = {"session_id": session_id, "id": session_id}
    if domain == "tooluse":
        response["tools"] = _tool_specs()
    return session_id, response


async def _get_session(session_id: str, domain: str) -> Session:
    async with LOCK:
        session = SESSIONS.get(session_id)
    if session is None or session.domain != domain.casefold():
        raise HTTPException(status_code=404, detail="session not found")
    return session


async def _swe_action(session: Session, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    client = session.client
    assert client is not None
    if action == "bash":
        command = _text(payload.get("command"))
        if not command:
            raise HTTPException(status_code=400, detail="command is required")
        result = await _run(client, command)
        return _backend_result(
            text=_output(result),
            metadata={"runtime": "agentos-sandbox", "exit_code": _exit_code(result)},
        )
    if action == "read_file":
        path = _safe_path(payload.get("path"), default="/workspace/ors_swe_task/calc.py")
        try:
            content = await client.read(path)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"AgentOS read failed: {type(exc).__name__}"
            ) from exc
        return _backend_result(text=content, metadata={"runtime": "agentos-sandbox", "path": path})
    if action == "write_file":
        path = _safe_path(payload.get("path"), default="/workspace/ors_swe_task/calc.py")
        try:
            await client.write(path, _text(payload.get("content")), create_dirs=True)
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"AgentOS write failed: {type(exc).__name__}"
            ) from exc
        return _backend_result(
            text=f"wrote {path}", metadata={"runtime": "agentos-sandbox", "path": path}
        )
    if action == "submit":
        root = _text(session.state.get("root")) or "/workspace/ors_swe_task"
        result = await _run(
            client,
            f"python -m unittest discover -s {shlex.quote(root)} -p 'test_*.py' -q",
        )
        code = _exit_code(result)
        return _backend_result(
            text=_output(result),
            reward=1.0 if code == 0 else 0.0,
            finished=True,
            metadata={"runtime": "agentos-sandbox", "verifier": "unittest", "exit_code": code},
        )
    raise HTTPException(status_code=400, detail="unknown SWE action")


def _ppt_spec(content: str, path: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = {"text": content}
    if isinstance(parsed, list):
        parsed = {"slides": parsed}
    if not isinstance(parsed, Mapping):
        parsed = {"text": str(parsed)}
    spec = dict(parsed)
    spec["output_path"] = path
    return spec


async def _ppt_render(client: Any, path: str) -> tuple[Any, list[dict[str, Any]]]:
    result = await _run(
        client,
        "mkdir -p /workspace/ors_ppt_render && "
        "libreoffice --headless --convert-to pdf --outdir /workspace/ors_ppt_render "
        f"{shlex.quote(path)} "
        ">/tmp/ors_lo.out 2>/tmp/ors_lo.err && "
        "pdftoppm -png /workspace/ors_ppt_render/" + shlex.quote(PurePosixPath(path).stem) + ".pdf "
        "/workspace/ors_ppt_render/slide >/tmp/ors_ppm.out 2>/tmp/ors_ppm.err",
    )
    if _exit_code(result) != 0:
        return result, []
    listing = await _run(client, "find /workspace/ors_ppt_render -name 'slide-*.png' -print | sort")
    image_paths = [line.strip() for line in _output(listing).splitlines() if line.strip()]
    images: list[dict[str, Any]] = []
    for image_path in image_paths[:8]:
        try:
            raw = await client.download(image_path)
        except Exception:
            continue
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > 2_000_000:
            continue
        images.append({"data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"})
    return result, images


async def _ppt_action(session: Session, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    client = session.client
    assert client is not None
    path = _safe_path(payload.get("path"), default="/workspace/deliverable.pptx")
    if action == "ppt_write":
        spec = _ppt_spec(_text(payload.get("content")), path)
        await client.write(
            "/workspace/ors_ppt_spec.json", json.dumps(spec, ensure_ascii=False), create_dirs=True
        )
        result = await _run(client, "python /workspace/ors_ppt_make.py")
        return _backend_result(
            text=_output(result),
            metadata={"runtime": "agentos-sandbox", "exit_code": _exit_code(result), "path": path},
        )
    if action == "ppt_read":
        result = await _run(client, f"python /workspace/ors_ppt_read.py {shlex.quote(path)}")
        return _backend_result(
            text=_output(result),
            metadata={"runtime": "agentos-sandbox", "exit_code": _exit_code(result), "path": path},
        )
    if action == "render":
        result, images = await _ppt_render(client, path)
        return _backend_result(
            text=_output(result) or "rendered",
            images=images,
            metadata={"runtime": "agentos-sandbox", "image_count": len(images), "path": path},
        )
    if action == "submit_deliverable":
        result, images = await _ppt_render(client, path)
        read = await _run(client, f"python /workspace/ors_ppt_read.py {shlex.quote(path)}")
        try:
            info = json.loads(_output(read).splitlines()[-1])
        except (ValueError, IndexError):
            info = {}
        min_slides = int(session.task_spec.get("min_slides", 1) or 1)
        valid = (
            _exit_code(result) == 0
            and int(info.get("slides", 0) or 0) >= min_slides
            and bool(images)
        )
        return _backend_result(
            text=json.dumps(
                {"slides": info.get("slides"), "images": len(images)}, ensure_ascii=False
            ),
            reward=1.0 if valid else 0.0,
            finished=True,
            metadata={
                "runtime": "agentos-sandbox",
                "verifier": "libreoffice+pdftoppm",
                "valid": valid,
                "path": path,
            },
            images=images,
        )
    raise HTTPException(status_code=400, detail="unknown PPT action")


async def _tool_action(session: Session, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    values = session.state.setdefault("values", {})
    target = session.state.setdefault("target", {"count": 2})
    if action == "invoke_tool":
        name = _text(payload.get("tool_name"))
        args = payload.get("arguments")
        args = dict(args) if isinstance(args, Mapping) else {}
        key = _text(args.get("key")) or "count"
        if name == "increment":
            values[key] = float(values.get(key, 0)) + float(args.get("delta", 1))
        elif name == "set_value":
            values[key] = float(args.get("value", 0))
        else:
            raise HTTPException(status_code=400, detail="unknown registered tool")
        if values[key].is_integer():
            values[key] = int(values[key])
        return _backend_result(
            text=json.dumps(values, ensure_ascii=False),
            metadata={"runtime": "stateful-tool-registry", "tool": name},
        )
    if action == "get_state":
        return _backend_result(
            text=json.dumps(values, ensure_ascii=False),
            metadata={"runtime": "stateful-tool-registry"},
        )
    if action == "submit":
        valid = all(values.get(key) == expected for key, expected in target.items())
        return _backend_result(
            text=json.dumps(values, ensure_ascii=False),
            reward=1.0 if valid else 0.0,
            finished=True,
            metadata={
                "runtime": "stateful-tool-registry",
                "verifier": "target_state",
                "valid": valid,
            },
        )
    raise HTTPException(status_code=400, detail="unknown ToolUse action")


async def _delete_session(session_id: str, domain: str) -> None:
    async with LOCK:
        session = SESSIONS.pop(session_id, None)
    if session is None or session.domain != domain.casefold():
        return
    await _destroy_sandbox(session.client)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "runtime": "agentos-canary", "domains": ["swe", "ppt", "tooluse"]}


@app.post("/v1/sandboxes")
async def create_sandbox(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    task_id = _text(body.get("task_id")) or "swe-canary"
    _, response = await _create_session("swe", task_id, body.get("task_spec") or {})
    response["sandbox_id"] = response["session_id"]
    return JSONResponse(response)


@app.post("/v1/sandboxes/{sandbox_id}/exec")
async def swe_exec(
    sandbox_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(sandbox_id, "swe")
    return JSONResponse(
        await _swe_action(session, _text(body.get("action")), body.get("input") or {})
    )


@app.post("/v1/sandboxes/{sandbox_id}/files/read")
async def swe_read(
    sandbox_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(sandbox_id, "swe")
    return JSONResponse(await _swe_action(session, "read_file", body.get("input") or {}))


@app.post("/v1/sandboxes/{sandbox_id}/files/write")
async def swe_write(
    sandbox_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(sandbox_id, "swe")
    return JSONResponse(await _swe_action(session, "write_file", body.get("input") or {}))


@app.post("/v1/sandboxes/{sandbox_id}/verify")
async def swe_verify(
    sandbox_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(sandbox_id, "swe")
    return JSONResponse(await _swe_action(session, "submit", body.get("input") or {}))


@app.delete("/v1/sandboxes/{sandbox_id}")
async def swe_delete(
    sandbox_id: str, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    await _delete_session(sandbox_id, "swe")
    return JSONResponse({"deleted": True})


@app.post("/v1/presentations")
async def create_presentation(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    task_id = _text(body.get("task_id")) or "ppt-canary"
    _, response = await _create_session("ppt", task_id, body.get("task_spec") or {})
    response["workspace_id"] = response["session_id"]
    return JSONResponse(response)


@app.post("/v1/presentations/read")
async def ppt_read(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(_text(body.get("session_id")), "ppt")
    return JSONResponse(await _ppt_action(session, "ppt_read", body.get("input") or {}))


@app.post("/v1/presentations/write")
async def ppt_write(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(_text(body.get("session_id")), "ppt")
    return JSONResponse(await _ppt_action(session, "ppt_write", body.get("input") or {}))


@app.post("/v1/presentations/render")
async def ppt_render(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(_text(body.get("session_id")), "ppt")
    return JSONResponse(await _ppt_action(session, "render", body.get("input") or {}))


@app.post("/v1/presentations/verify")
async def ppt_verify(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(_text(body.get("session_id")), "ppt")
    return JSONResponse(await _ppt_action(session, "submit_deliverable", body.get("input") or {}))


@app.delete("/v1/presentations/{session_id}")
async def ppt_delete(
    session_id: str, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    await _delete_session(session_id, "ppt")
    return JSONResponse({"deleted": True})


@app.post("/v1/tasks/{task_id}/sessions")
async def create_tool_session(
    task_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    _, response = await _create_session("tooluse", task_id, body.get("task_spec") or {})
    return JSONResponse(response)


@app.get("/v1/tasks/{task_id}/tools")
async def tool_registry(
    task_id: str, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    return JSONResponse({"tools": _tool_specs()})


@app.post("/v1/sessions/{session_id}/tools/{tool_name}")
async def invoke_tool(
    session_id: str,
    tool_name: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(session_id, "tooluse")
    incoming = dict(body.get("input") or {})
    incoming.setdefault("tool_name", tool_name)
    return JSONResponse(await _tool_action(session, "invoke_tool", incoming))


@app.get("/v1/sessions/{session_id}/state")
async def tool_state(
    session_id: str, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    session = await _get_session(session_id, "tooluse")
    return JSONResponse(await _tool_action(session, "get_state", {}))


@app.post("/v1/sessions/{session_id}/verify")
async def tool_verify(
    session_id: str, request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    body = await request.json()
    session = await _get_session(session_id, "tooluse")
    return JSONResponse(await _tool_action(session, "submit", body.get("input") or {}))


@app.delete("/v1/sessions/{session_id}")
async def tool_delete(
    session_id: str, authorization: str | None = Header(default=None)
) -> JSONResponse:
    _check_auth(authorization)
    await _delete_session(session_id, "tooluse")
    return JSONResponse({"deleted": True})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
