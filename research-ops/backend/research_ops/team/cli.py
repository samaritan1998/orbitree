from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from .gateway import TeamConfig, create_team_app
from .runtime import provision, run_worker, start_worker, stop_worker
from .store import TeamStore

app = typer.Typer(help="Invite-only accounts and isolated Linux workers (opt-in)")
ROOT = Path("/srv/research-ops-team")
RootOption = Annotated[Path, typer.Option(help="Private team state directory")]


@app.command("add-user")
def add_user(username: str, name: str, root: RootOption = ROOT, admin: bool = False) -> None:
    """Register an account. This does not copy credentials or start an Agent."""
    account = TeamStore(root).add_account(username, name, admin=admin)
    typer.echo(
        f"Registered {account['username']}. Provision its Linux workspace before inviting it."
    )


@app.command("provision")
def provision_user(
    username: str,
    release: Annotated[Path, typer.Option()],
    root: RootOption = ROOT,
    shared_read: Annotated[list[Path] | None, typer.Option()] = None,
) -> None:
    """Create a locked Linux account and private home, without starting work."""
    home = provision(root, username, release, shared_read or [])
    typer.echo(f"Workspace prepared: {home}")


@app.command("invite")
def invite(username: str, origin: str, root: RootOption = ROOT) -> None:
    """Print a one-use activation link. Treat it as a secret; expires in 24 hours."""
    TeamConfig(root, Path("."), origin)
    token = TeamStore(root).invitation(username, "cli")
    typer.echo(f"{origin}/#invite={token}")


@app.command("users")
def users(root: RootOption = ROOT) -> None:
    typer.echo(json.dumps(TeamStore(root).accounts(), ensure_ascii=False, indent=2))


@app.command("reset-login")
def reset_login(username: str, origin: str, root: RootOption = ROOT) -> None:
    """Revoke Web login and print a fresh invite; workspace and model login are unchanged."""
    TeamConfig(root, Path("."), origin)
    store = TeamStore(root)
    store.reset_login(username)
    token = store.invitation(username, "cli")
    typer.echo(f"{origin}/#invite={token}")


@app.command("start-user")
def start_user(username: str, root: RootOption = ROOT) -> None:
    typer.echo(f"Starting worker, PID {start_worker(root, username)}")


@app.command("stop-user")
def stop_user(username: str, root: RootOption = ROOT) -> None:
    """Stop a worker. Active Web turns may be interrupted; no files are deleted."""
    stop_worker(root, username)
    typer.echo("Sent SIGTERM to the worker. Background external jobs were not deleted.")


@app.command("shell")
def shell(username: str, root: RootOption = ROOT) -> None:
    """Enter the same non-root filesystem policy used by this account's Agents."""
    from .store import valid_username

    valid_username(username)
    run_worker(root / "control" / f"{username}.json", ["/bin/bash", "--noprofile", "--norc"])


@app.command("serve")
def serve(
    origin: Annotated[str, typer.Option()],
    frontend: Annotated[Path, typer.Option()],
    release: Annotated[
        Path | None, typer.Option(help="Clean shared release for new accounts")
    ] = None,
    root: RootOption = ROOT,
    host: str = "127.0.0.1",
    port: int = 8030,
) -> None:
    """Serve login and authenticated HTTP/WebSocket routing; no worker auto-start."""
    uvicorn.run(
        create_team_app(TeamConfig(root, frontend, origin, release)),
        host=host,
        port=port,
        proxy_headers=False,
        access_log=False,
    )
