"""Isolated real-PTY browser fixture. Never uses the production database or AI runtimes."""

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from research_ops.api_terminal import terminal_router
from research_ops.services.web_terminal import WebTerminal, terminal_key
from research_ops.settings import Settings

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--frontend", type=Path, required=True)
args = parser.parse_args()
os.environ["HOME"] = str(args.root)
settings = Settings(
    _env_file=None,
    data_dir=args.root / "data",
    runtime_dir=args.root / "runtime",
    workspace_root=args.root,
    web_terminal_enabled=True,
    web_terminal_cwd=args.root,
    web_terminal_shell="/bin/bash",
    infoflow_public_base_url=f"http://127.0.0.1:{args.port}",
    access_token=None,
    require_access_token=False,
)
terminal_key(settings, create=True)
terminal = WebTerminal(settings)


@asynccontextmanager
async def lifespan(app):
    yield
    await terminal.terminate()


app = FastAPI(lifespan=lifespan)
app.include_router(terminal_router(terminal))


@app.get("/api/team/session")
def team():
    return {"enabled": False}


@app.get("/api/programs")
def programs():
    return []


@app.get("/{path:path}")
def frontend(path: str):
    root = args.frontend.resolve()
    candidate = (root / path).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(root / "index.html")


uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
