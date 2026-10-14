from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import httpx
import typer
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from research_ops import models, schemas
from research_ops.api import AppState
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.claude_execution import ClaudeExecutionPreferences
from research_ops.codex.identity import CodexIdentityService, ExecutionPreferences
from research_ops.codex.identity_pool import adapter_for_execution
from research_ops.codex.rollout import find_new_rollout
from research_ops.db import Database, is_sqlite_busy, run_db
from research_ops.eval_worker import EvalWorkerConfig, run_eval_worker
from research_ops.runtime_storage import prepare_runtime_database, seed_runtime_database
from research_ops.services import bootstrap_catalog
from research_ops.services.briefs import developer_instructions
from research_ops.services.core import ConflictError
from research_ops.services.restart_guard import RestartBlocker, RestartGuard, idle_restart_lock
from research_ops.services.web_terminal import terminal_key
from research_ops.settings import PROJECT_ROOT, Settings, get_settings
from research_ops.team.cli import app as team_app

app = typer.Typer(
    no_args_is_help=True,
    help="Orbitree local control plane",
    pretty_exceptions_show_locals=False,
)
server_app = typer.Typer(help="Manage the detached Orbitree Web service")
eval_app = typer.Typer(help="Run distributed Agent Eval Harness workers")
job_app = typer.Typer(help="Submit and inspect logical compute jobs")
auth_app = typer.Typer(help="Manage explicit Codex identities without exposing credentials")
terminal_app = typer.Typer(help="Manage the private Web terminal")
app.add_typer(server_app, name="server")
app.add_typer(eval_app, name="eval")
app.add_typer(job_app, name="job")
app.add_typer(auth_app, name="auth")
app.add_typer(team_app, name="team")
app.add_typer(terminal_app, name="terminal")


@terminal_app.command("key")
def terminal_unlock_key() -> None:
    """Show the private unlock key locally; never expose it through the Web API."""
    typer.echo(terminal_key(get_settings(), create=True))


@auth_app.command("status")
def auth_status() -> None:
    identities = CodexIdentityService(get_settings())
    typer.echo(json.dumps(identities.status(), indent=2))


@auth_app.command("import-api")
def auth_import_api() -> None:
    """Preserve the current API key outside personal Codex's auth.json."""
    try:
        CodexIdentityService(get_settings()).preserve_api_key()
    except CodexUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo("API credentials preserved in the private credential store; no identity changed.")


@auth_app.command("login")
def auth_login() -> None:
    """Explicitly sign in to Personal Codex using the official device flow."""
    settings = get_settings()
    identities = CodexIdentityService(settings)
    try:
        status = identities.status()
        if status["identities"][0]["ready"]:
            identities.preserve_api_key()
        identities.prepare_personal_home()
        env = identities.environment("chatgpt", login=True)
    except CodexUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo("Personal Codex login is isolated. Existing API login and Branches are unchanged.")
    code = subprocess.call([
        settings.codex_command, "-c", 'model_provider="openai"',
        "-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"',
        "login", "--device-auth",
    ], env=env)
    raise typer.Exit(code)


class JobCliError(RuntimeError):
    pass


def _job_api_request(
    method: str,
    path: str,
    *,
    control_url: str,
    token_env: str,
    payload: dict[str, object] | None = None,
    params: dict[str, object] | None = None,
) -> object:
    headers: dict[str, str] = {}
    token = os.environ.get(token_env, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(
            base_url=control_url.rstrip("/"),
            headers=headers,
            timeout=30.0,
            trust_env=False,
        ) as client:
            response = client.request(method, path, json=payload, params=params)
    except httpx.HTTPError as exc:
        raise JobCliError("无法连接统一任务服务。") from exc
    if response.is_error:
        try:
            detail = str(response.json().get("detail") or "")
        except (ValueError, AttributeError):
            detail = ""
        raise JobCliError(detail or f"统一任务服务返回 HTTP {response.status_code}。")
    try:
        return response.json()
    except ValueError as exc:
        raise JobCliError("统一任务服务返回了无效响应。") from exc


def _job_idempotency_key(
    *,
    submitter: str,
    work_id: str,
    job_kind: str,
    hardware: str,
    paths: list[Path],
) -> str:
    digest = hashlib.sha256()
    for value in (submitter, work_id.upper(), job_kind, hardware):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for path in paths:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"bundle-{digest.hexdigest()[:40]}"


def _print_managed_job(payload: dict[str, object]) -> None:
    typer.echo(f"Job: {payload.get('id')}")
    typer.echo(f"Name: {payload.get('name')}")
    typer.echo(f"State: {payload.get('state')}")
    typer.echo(
        f"Resources: {payload.get('requested_nodes')} nodes / "
        f"{payload.get('requested_cards')} cards"
    )
    typer.echo(str(payload.get("message") or ""))


@job_app.command("submit")
def submit_managed_job(
    bundle: Annotated[Path, typer.Argument(help="Directory containing config.yaml and run.sh")],
    work_id: Annotated[str, typer.Option("--work", help="Orbitree Work ID")],
    hardware: Annotated[str, typer.Option(help="Hardware class: b or h100")],
    job_kind: Annotated[
        str,
        typer.Option(
            "--kind",
            help="training, conversion, inference, preprocessing, or other",
        ),
    ] = "training",
    name: Annotated[str | None, typer.Option(help="Logical display name")] = None,
    train: Annotated[Path | None, typer.Option(help="Optional train.py path")] = None,
    receipt: Annotated[
        Path | None,
        typer.Option(help="Required formal-training preflight receipt"),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Stable key for safe retries; generated from the bundle by default"),
    ] = None,
    control_url: Annotated[
        str,
        typer.Option(envvar="ROPS_CONTROL_URL", help="Orbitree base URL"),
    ] = "http://127.0.0.1:8020",
    token_env: Annotated[
        str,
        typer.Option(help="Environment variable containing the Orbitree access token"),
    ] = "ROPS_ACCESS_TOKEN",
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """Submit one logical job through the unified scheduler."""
    root = bundle.expanduser().resolve()
    config = root / "config.yaml"
    run = root / "run.sh"
    resolved_train = train.expanduser().resolve() if train else None
    resolved_receipt = receipt.expanduser().resolve() if receipt else None
    normalized_hardware = hardware.strip().casefold()
    normalized_kind = job_kind.strip().casefold()
    allowed_kinds = {"training", "conversion", "inference", "preprocessing", "other"}
    if normalized_hardware not in {"b", "h100"}:
        raise typer.BadParameter("hardware must be b or h100", param_hint="--hardware")
    if normalized_kind not in allowed_kinds:
        raise typer.BadParameter(
            "kind must be training, conversion, inference, preprocessing, or other",
            param_hint="--kind",
        )
    required_paths = [config, run]
    if resolved_train:
        required_paths.append(resolved_train)
    if resolved_receipt:
        required_paths.append(resolved_receipt)
    missing = [path.name for path in required_paths if not path.is_file()]
    if missing:
        raise typer.BadParameter(f"missing bundle files: {', '.join(missing)}")
    submitter = getpass.getuser()
    stable_key = idempotency_key or _job_idempotency_key(
        submitter=submitter,
        work_id=work_id,
        job_kind=normalized_kind,
        hardware=normalized_hardware,
        paths=required_paths,
    )
    request = {
        "work_item_id": work_id,
        "submitter": submitter,
        "display_name": name,
        "job_kind": normalized_kind,
        "hardware": normalized_hardware,
        "config_path": str(config),
        "run_path": str(run),
        "train_path": str(resolved_train) if resolved_train else None,
        "preflight_receipt_path": str(resolved_receipt) if resolved_receipt else None,
        "idempotency_key": stable_key,
    }
    try:
        response = _job_api_request(
            "POST",
            "/api/managed-jobs",
            control_url=control_url,
            token_env=token_env,
            payload=request,
        )
    except JobCliError as exc:
        typer.echo(f"提交失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    assert isinstance(response, dict)
    if json_output:
        typer.echo(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        _print_managed_job(response)


@job_app.command("status")
def managed_job_status(
    job_id: Annotated[str, typer.Argument(help="Logical job ID")],
    control_url: Annotated[
        str,
        typer.Option(envvar="ROPS_CONTROL_URL", help="Orbitree base URL"),
    ] = "http://127.0.0.1:8020",
    token_env: Annotated[str, typer.Option()] = "ROPS_ACCESS_TOKEN",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one logical job without exposing its physical placement."""
    try:
        response = _job_api_request(
            "GET",
            f"/api/managed-jobs/{job_id}",
            control_url=control_url,
            token_env=token_env,
            params={"submitter": getpass.getuser()},
        )
    except JobCliError as exc:
        typer.echo(f"查询失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    assert isinstance(response, dict)
    if json_output:
        typer.echo(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        _print_managed_job(response)


@job_app.command("list")
def list_managed_jobs(
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
    control_url: Annotated[
        str,
        typer.Option(envvar="ROPS_CONTROL_URL", help="Orbitree base URL"),
    ] = "http://127.0.0.1:8020",
    token_env: Annotated[str, typer.Option()] = "ROPS_ACCESS_TOKEN",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List logical jobs owned by the current operating-system user."""
    try:
        response = _job_api_request(
            "GET",
            "/api/managed-jobs",
            control_url=control_url,
            token_env=token_env,
            params={"submitter": getpass.getuser(), "limit": limit},
        )
    except JobCliError as exc:
        typer.echo(f"查询失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    assert isinstance(response, list)
    if json_output:
        typer.echo(json.dumps(response, ensure_ascii=False, indent=2))
        return
    if not response:
        typer.echo("当前没有逻辑任务。")
        return
    for item in response:
        typer.echo(f"{item.get('id')}  {item.get('state'):<12}  {item.get('name')}")


def _server_paths(settings: Settings) -> tuple[Path, Path]:
    # Keep the supervisor identity with durable control data so a runtime-dir
    # migration can still stop the previous service. Logs are high-churn and
    # belong on the local runtime disk.
    control_dir = settings.data_dir / "run"
    runtime_dir = settings.runtime_data_dir / "run"
    control_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return control_dir / "server.pid", runtime_dir / "server.log"


def _read_server_pid(settings: Settings) -> int | None:
    pid_path, _log_path = _server_paths(settings)
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    try:
        if Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2] == "Z":
            return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _owned_supervisor(pid: int) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except (OSError, UnicodeError):
        return False
    return "research-ops" in command and "_supervise" in command


def _server_healthy(port: int, *, timeout: float = 2.0) -> bool:
    # This probe always targets the local server. Explicitly bypass environment
    # proxies so a global http_proxy cannot turn a healthy localhost response
    # into a proxy-generated 502.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False
    return response.status == 200 and payload.get("status") == "ok"


def _legacy_web_turns_active(settings: Settings) -> tuple[str, ...]:
    """Read active Web work from the old durable SQLite file during migration."""
    database_path = settings.data_dir / "research-ops.db"
    if not database_path.is_file():
        return ()
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    query = """
        select distinct branch_id from conversation_messages
        where role = 'user'
          and json_extract(metadata_json, '$.send_status') = 'pending'
        union
        select distinct branch_id from session_attempts
        where lease_owner = 'web'
          and status in ('starting', 'active')
          and lease_expires_at > ?
        order by branch_id
    """
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=10) as db:
        return tuple(str(row[0]) for row in db.execute(query, (now,)))


def _start_detached_server(settings: Settings, host: str, port: int) -> int:
    pid_path, log_path = _server_paths(settings)
    if _server_healthy(port):
        return _read_server_pid(settings) or 0
    stale_pid = _read_server_pid(settings)
    if stale_pid and _process_alive(stale_pid):
        if _owned_supervisor(stale_pid):
            raise RuntimeError(
                f"Orbitree supervisor {stale_pid} is alive but port {port} is not healthy; "
                "run `rops server restart`"
            )
        raise RuntimeError(f"PID file points to unrelated process {stale_pid}")
    pid_path.unlink(missing_ok=True)
    executable = PROJECT_ROOT / ".venv" / "bin" / "rops"
    if not executable.is_file():
        raise RuntimeError(f"Orbitree executable is unavailable: {executable}")
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            [
                str(executable),
                "_supervise",
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    # Cold starts can include App Server initialization and a sizeable local
    # SQLite recovery. Keep the supervisor alive through that window instead
    # of treating a slow-but-progressing service as a failed restart.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _server_healthy(port, timeout=2.0):
            return process.pid
        if process.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise RuntimeError(f"Orbitree supervisor exited; inspect {log_path}")
        time.sleep(0.25)
    if _process_alive(process.pid):
        os.kill(process.pid, signal.SIGTERM)
    pid_path.unlink(missing_ok=True)
    raise RuntimeError(f"Orbitree did not become healthy; inspect {log_path}")


def _stop_detached_server(settings: Settings, port: int) -> bool:
    pid_path, _log_path = _server_paths(settings)
    pid = _read_server_pid(settings)
    if pid is None:
        if _server_healthy(port):
            raise RuntimeError(
                f"Port {port} is serving Orbitree without a managed PID; stop it manually"
            )
        return False
    if not _process_alive(pid):
        pid_path.unlink(missing_ok=True)
        return False
    if not _owned_supervisor(pid):
        raise RuntimeError(f"Refusing to stop unrelated process {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.2)
    if _process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    return True


def runtime() -> AppState:
    settings = get_settings()
    prepare_runtime_database(settings)
    state = AppState(settings)
    state.database.init()
    bootstrap_catalog(state.database.session_factory, settings)
    return state


@eval_app.command("worker")
def eval_worker_command(
    run_id: Annotated[str, typer.Argument(help="Eval run UUID")],
    evaluator_url: Annotated[
        str,
        typer.Option(
            "--evaluator-url",
            envvar="ROPS_EVAL_EVALUATOR_URL",
            help="Batch evaluator HTTP endpoint",
        ),
    ],
    control_url: Annotated[
        str,
        typer.Option(
            "--control-url",
            envvar="ROPS_EVAL_CONTROL_URL",
            help="Orbitree base URL",
        ),
    ] = "http://127.0.0.1:8020",
    worker_id: Annotated[str, typer.Option(help="Stable worker prefix")] = "",
    slots: Annotated[int, typer.Option(min=1, max=128)] = 1,
    claim_size: Annotated[int, typer.Option(min=1, max=128)] = 8,
    lease_seconds: Annotated[int, typer.Option(min=30, max=7200)] = 900,
    poll_seconds: Annotated[float, typer.Option(min=0.1)] = 2.0,
    evaluator_timeout_seconds: Annotated[float, typer.Option(min=1.0)] = 600.0,
    control_token_env: Annotated[str, typer.Option()] = "ROPS_ACCESS_TOKEN",
    evaluator_token_env: Annotated[str | None, typer.Option()] = None,
    once: Annotated[bool, typer.Option(help="Process at most one claim per slot")] = False,
) -> None:
    """Claim leased eval batches and forward them to a long-lived HTTP evaluator."""
    config = EvalWorkerConfig(
        run_id=run_id,
        evaluator_url=evaluator_url,
        control_url=control_url,
        worker_id=worker_id,
        slots=slots,
        claim_size=claim_size,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        evaluator_timeout_seconds=evaluator_timeout_seconds,
        control_token_env=control_token_env,
        evaluator_token_env=evaluator_token_env,
        once=once,
    )
    try:
        stats = asyncio.run(run_eval_worker(config))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception as exc:
        typer.echo(f"Eval worker failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))


@app.command()
def init() -> None:
    """Initialize the database, Program catalog, and workspace roots."""
    state = runtime()
    typer.echo(
        json.dumps(
            {
                "database": state.settings.resolved_database_url,
                "workspace_root": str(state.settings.workspace_root),
                "programs": [program.code for program in state.service.list_programs()],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("backfill-prompts")
def backfill_prompts(
    interval_seconds: Annotated[
        float,
        typer.Option(help="Seconds between idempotent repair passes"),
    ] = 30.0,
    once: Annotated[bool, typer.Option(help="Run one pass and exit")] = False,
) -> None:
    """Repair missing Codex display queries without running a model turn."""
    state = runtime()
    interval = max(5.0, interval_seconds)
    while True:
        updated = state.codex_results.backfill_missing_prompts()
        if updated:
            typer.echo(
                json.dumps(
                    {
                        "updated": updated,
                        "database": state.settings.resolved_database_url,
                    },
                    ensure_ascii=False,
                ),
                err=True,
            )
        if once:
            return
        time.sleep(interval)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind host")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port")] = None,
    reload: Annotated[bool, typer.Option(help="Reload on source changes")] = False,
) -> None:
    """Run the API and built Web workspace."""
    settings = get_settings()
    uvicorn.run(
        "research_ops.main:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


@app.command("_supervise", hidden=True)
def supervise(
    host: Annotated[str, typer.Option(help="Bind host")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Bind port")] = 8020,
) -> None:
    """Keep the Web service alive when no user systemd session is available."""
    executable = PROJECT_ROOT / ".venv" / "bin" / "rops"
    stopping = False
    child: subprocess.Popen[bytes] | None = None

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stopping:
        typer.echo(
            f"[{datetime.now(UTC).isoformat()}] starting Orbitree on {host}:{port}",
            err=True,
        )
        child = subprocess.Popen(
            [str(executable), "serve", "--host", host, "--port", str(port)],
            cwd=PROJECT_ROOT,
        )
        return_code = child.wait()
        if stopping:
            break
        typer.echo(
            f"[{datetime.now(UTC).isoformat()}] server exited with {return_code}; restarting in 3s",
            err=True,
        )
        time.sleep(3)


@server_app.command("start")
def server_start(
    host: Annotated[str | None, typer.Option(help="Bind host")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port")] = None,
) -> None:
    """Start the Web service in a detached, auto-restarting supervisor."""
    settings = get_settings()
    selected_host = host or settings.host
    selected_port = port or settings.port
    try:
        pid = _start_detached_server(settings, selected_host, selected_port)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    state = "already running" if pid == 0 else f"supervisor PID {pid}"
    typer.echo(f"Orbitree {state}: http://127.0.0.1:{selected_port}")


@server_app.command("stop")
def server_stop(
    port: Annotated[int | None, typer.Option(help="Health-check port")] = None,
) -> None:
    """Stop the detached Orbitree supervisor and Web service."""
    settings = get_settings()
    selected_port = port or settings.port
    try:
        stopped = _stop_detached_server(settings, selected_port)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Orbitree stopped" if stopped else "Orbitree is not running")


@server_app.command("restart")
def server_restart(
    host: Annotated[str | None, typer.Option(help="Bind host")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port")] = None,
) -> None:
    """Restart the detached Orbitree supervisor."""
    settings = get_settings()
    selected_host = host or settings.host
    selected_port = port or settings.port
    try:
        _stop_detached_server(settings, selected_port)
        pid = _start_detached_server(settings, selected_host, selected_port)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Orbitree restarted (supervisor PID {pid}): http://127.0.0.1:{selected_port}")


@server_app.command("restart-when-idle")
def server_restart_when_idle(
    host: Annotated[str, typer.Option(help="Bind host after restart")] = "0.0.0.0",
    port: Annotated[int | None, typer.Option(help="Service port")] = None,
    poll_seconds: Annotated[
        float,
        typer.Option(help="Seconds between conversation activity checks"),
    ] = 15.0,
    stable_checks: Annotated[
        int,
        typer.Option(help="Consecutive idle checks required before restart"),
    ] = 5,
    quiet_seconds: Annotated[
        float,
        typer.Option(help="Minimum continuously idle time before restart"),
    ] = 60.0,
) -> None:
    """Wait for Web, local CLI, and Goals to become idle, then restart once."""
    settings = get_settings()
    selected_port = port or settings.port
    database = Database(settings)
    guard = RestartGuard(database.session_factory)
    pid_path, _ = _server_paths(settings)
    required_checks = max(1, stable_checks)
    interval = max(1.0, poll_seconds)
    quiet_period = max(0.0, quiet_seconds)

    def report(phase: str, **details: object) -> None:
        typer.echo(json.dumps({
            "time": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "phase": phase,
            **details,
        }, ensure_ascii=False), err=True)

    def probe() -> tuple[RestartBlocker, ...]:
        active = guard.blockers()
        if not _server_healthy(selected_port):
            active += (RestartBlocker("Orbitree", "health_check_failed"),)
        return active

    try:
        with idle_restart_lock(pid_path.with_name("restart-when-idle.lock")):
            idle_checks = 0
            idle_since: float | None = None
            last_active: tuple[RestartBlocker, ...] | None = None
            last_report = float("-inf")
            while True:
                try:
                    active = probe()
                except Exception as exc:
                    # A failed read is unknown activity, not permission to stop.
                    active = (RestartBlocker("Activity probe", type(exc).__name__),)
                now = time.monotonic()
                if active:
                    idle_checks = 0
                    idle_since = None
                else:
                    idle_checks += 1
                    if idle_since is None:
                        idle_since = now
                idle_for = now - idle_since if idle_since is not None else 0.0
                if active != last_active or not active or now - last_report >= 60:
                    report(
                        "waiting" if active else "quiet_period",
                        blockers=[item.as_dict() for item in active],
                        idle_checks=idle_checks,
                        required_idle_checks=required_checks,
                        idle_seconds=round(idle_for, 1),
                        required_idle_seconds=quiet_period,
                    )
                    last_active = active
                    last_report = now
                if idle_checks >= required_checks and idle_for >= quiet_period:
                    try:
                        final_active = probe()
                    except Exception:
                        final_active = (RestartBlocker("Activity probe", "final_check_failed"),)
                    if not final_active:
                        break
                    report("final_check_busy", blockers=[b.as_dict() for b in final_active])
                    idle_checks = 0
                    idle_since = None
                time.sleep(interval)

            report("restarting")
            _stop_detached_server(settings, selected_port)
            pid = _start_detached_server(settings, host, selected_port)
            report("completed", supervisor_pid=pid, url=f"http://127.0.0.1:{selected_port}")
    except RuntimeError as exc:
        report("failed", error=str(exc))
        raise typer.Exit(1) from exc
    finally:
        database.engine.dispose()


@server_app.command("migrate-runtime-when-idle")
def server_migrate_runtime_when_idle(
    host: Annotated[str, typer.Option(help="Bind host after migration")] = "0.0.0.0",
    port: Annotated[int | None, typer.Option(help="Service port")] = None,
    poll_seconds: Annotated[
        float,
        typer.Option(help="Seconds between old-service activity checks"),
    ] = 10.0,
    stable_checks: Annotated[
        int,
        typer.Option(help="Consecutive idle checks required before migration"),
    ] = 3,
) -> None:
    """Move SQLite runtime state to local disk after Web turns have drained."""
    settings = get_settings()
    if not settings.uses_separate_runtime_storage:
        typer.echo(
            "ROPS_RUNTIME_DIR must differ from ROPS_DATA_DIR before runtime migration",
            err=True,
        )
        raise typer.Exit(2)
    selected_port = port or settings.port
    required_checks = max(1, stable_checks)
    interval = max(1.0, poll_seconds)
    idle_checks = 0
    last_active: tuple[str, ...] | None = None
    while idle_checks < required_checks:
        active = _legacy_web_turns_active(settings)
        idle_checks = idle_checks + 1 if not active else 0
        if active != last_active or not active:
            typer.echo(
                json.dumps(
                    {
                        "active_web_branch_ids": active,
                        "idle_checks": idle_checks,
                        "required_idle_checks": required_checks,
                    },
                    ensure_ascii=False,
                ),
                err=True,
            )
            last_active = active
        if idle_checks < required_checks:
            time.sleep(interval)

    _stop_detached_server(settings, selected_port)
    if not seed_runtime_database(settings, replace=True):
        raise RuntimeError("Unable to create the local runtime database snapshot")
    pid = _start_detached_server(settings, host, selected_port)
    typer.echo(
        f"Orbitree moved runtime storage and restarted (supervisor PID {pid}): "
        f"http://127.0.0.1:{selected_port}"
    )


@server_app.command("status")
def server_status(
    port: Annotated[int | None, typer.Option(help="Health-check port")] = None,
) -> None:
    """Show service health, supervisor PID, and log location."""
    settings = get_settings()
    selected_port = port or settings.port
    pid = _read_server_pid(settings)
    _pid_path, log_path = _server_paths(settings)
    healthy = _server_healthy(selected_port)
    supervisor_alive = bool(pid and _process_alive(pid) and _owned_supervisor(pid))
    typer.echo(f"Web: {'healthy' if healthy else 'down'}")
    typer.echo(f"Supervisor: {pid if supervisor_alive else 'not running'}")
    typer.echo(f"URL: http://127.0.0.1:{selected_port}")
    typer.echo(f"Log: {log_path}")
    if not healthy:
        raise typer.Exit(1)


@server_app.command("logs")
def server_logs(
    lines: Annotated[int, typer.Option(min=1, max=2000)] = 80,
) -> None:
    """Print the latest detached server logs."""
    settings = get_settings()
    _pid_path, log_path = _server_paths(settings)
    if not log_path.is_file():
        typer.echo(f"No server log yet: {log_path}")
        return
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    typer.echo("\n".join(content[-lines:]))


@app.command("programs")
def list_programs() -> None:
    state = runtime()
    for program in state.service.list_programs():
        typer.echo(f"{program.code:<8} {program.name}")


@app.command("new")
def new_work(
    program: Annotated[str, typer.Argument(help="Program code")] = "FREE",
    title: Annotated[str | None, typer.Option()] = None,
    objective: Annotated[str | None, typer.Option()] = None,
    repository_id: Annotated[str | None, typer.Option()] = None,
    capture: Annotated[bool, typer.Option(help="Capture without starting work")] = False,
) -> None:
    state = runtime()
    work_item, branch = state.service.create_work_item(
        schemas.WorkItemCreate(
            program_code=program,
            title=title,
            objective=objective,
            definition_mode="defined" if title else "emerging",
            repository_id=repository_id,
            branch_mode="capture" if capture else "write",
        )
    )
    typer.echo(
        json.dumps(
            {
                "work_id": work_item.human_id,
                "branch_id": branch.id,
                "cwd": branch.cwd,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("define")
def define_work(
    work_id: Annotated[str, typer.Argument(help="Work ID, for example FREE-0001")],
    title: Annotated[str, typer.Option(help="Concise title")],
    objective: Annotated[str, typer.Option(help="Objective, boundary, and expected outcome")],
) -> None:
    """Backfill the definition of an exploratory WorkItem once its intent is clear."""
    state = runtime()
    work_item = state.service.define_work_item(
        work_id,
        schemas.WorkItemDefinition(title=title, objective=objective),
    )
    typer.echo(
        json.dumps(
            {
                "work_id": work_item.human_id,
                "title": work_item.title,
                "objective": work_item.objective,
                "definition_pending": "needs_definition" in work_item.labels,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def status(program: Annotated[str | None, typer.Option()] = None) -> None:
    state = runtime()
    programs = [program] if program else [item.code for item in state.service.list_programs()]
    for code in programs:
        catalog, work_items, branches, _hidden, _edges, _sessions = state.service.program_tree(code)
        typer.echo(f"\n{catalog.code} {catalog.name}")
        branch_counts: dict[str, int] = {}
        for branch in branches:
            branch_counts[branch.work_item_id] = branch_counts.get(branch.work_item_id, 0) + 1
        for item in work_items:
            typer.echo(
                f"  {item.human_id:<12} {item.lifecycle:<8} {item.flow:<18} "
                f"branches={branch_counts.get(item.id, 0):<2} {item.title}"
            )


@app.command("desk")
def open_program_desk(
    program: Annotated[str, typer.Argument(help="Program code, for example HARN")],
) -> None:
    """Open the persistent coordinator Codex for one Program."""
    state = runtime()
    branch = state.service.program_desk_branch(program)
    typer.echo(f"Opening {program.upper()} Program Desk in {branch.cwd}")
    open_branch(branch.id)


@app.command("desk-context")
def program_desk_context(
    program: Annotated[str, typer.Argument(help="Program code, for example HARN")],
) -> None:
    """Print the current task, branch, handoff, and final-result snapshot for a Program."""
    state = runtime()
    typer.echo(json.dumps(state.service.program_context(program), ensure_ascii=False, indent=2))


@app.command()
def start(branch_id: str, owner: str = "cli") -> None:
    state = runtime()

    async def run() -> models.SessionAttempt:
        await state.codex.start()
        try:
            return await state.service.start_session(branch_id, schemas.SessionStart(owner=owner))
        finally:
            await state.codex.stop()

    attempt = asyncio.run(run())
    typer.echo(
        json.dumps(
            {
                "session_id": attempt.id,
                "thread_id": attempt.codex_thread_id,
                "status": attempt.status,
                "error": attempt.error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def fork(
    branch_id: str,
    name: Annotated[str, typer.Option(prompt=True)],
    objective: Annotated[str, typer.Option(prompt=True)],
    capture: bool = False,
    approved_by_user: Annotated[
        bool,
        typer.Option(
            "--approved-by-user",
            help="Required explicit human approval; agents must not set this themselves",
        ),
    ] = False,
    parallel: Annotated[
        bool,
        typer.Option(
            "--parallel",
            help="Create the child without moving future conversation off the parent",
        ),
    ] = False,
) -> None:
    if not approved_by_user:
        typer.echo(
            "Fork is a user-approved action. Ask the user to run this command again "
            "with --approved-by-user, or use the Web Fork button.",
            err=True,
        )
        raise typer.Exit(code=2)
    state = runtime()

    async def run() -> models.ExecutionBranch:
        if not capture:
            await state.codex.start()
        try:
            return await state.service.fork_branch(
                branch_id,
                schemas.BranchForkCreate(
                    name=name,
                    objective=objective,
                    mode="capture" if capture else "write",
                    continuation="parallel" if parallel or capture else "continue",
                    start_session=False,
                    manual_approval=True,
                ),
            )
        finally:
            if not capture:
                await state.codex.stop()

    branch = asyncio.run(run())
    provisioning = branch.context_snapshot.get("provisioning", {})
    typer.echo(
        json.dumps(
            {
                "branch_id": branch.id,
                "thread_id": branch.codex_thread_id,
                "cwd": branch.cwd,
                "status": branch.status,
                "continuation": branch.context_snapshot.get("continuation"),
                "provisioning": provisioning,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if branch.status == "blocked" or provisioning.get("state") == "failed":
        raise typer.Exit(code=1)
    if not parallel and not capture:
        typer.echo(
            "Fork ready. Stop child-scope work in this parent turn and leave a concise handoff. "
            "The child Branch will remain idle until a user explicitly starts its continuation."
        )


@app.command("repair-fork")
def repair_fork(
    branch_id: str,
    approved_by_user: Annotated[
        bool,
        typer.Option(
            "--approved-by-user",
            help="Required explicit human approval; agents must not set this themselves",
        ),
    ] = False,
    parallel: Annotated[
        bool,
        typer.Option("--parallel", help="Repair without pausing the parent Branch"),
    ] = False,
) -> None:
    if not approved_by_user:
        typer.echo(
            "Fork repair is a user-approved action. Ask the user to run this command "
            "again with --approved-by-user, or use the Web Repair fork button.",
            err=True,
        )
        raise typer.Exit(code=2)
    state = runtime()

    async def run() -> models.ExecutionBranch:
        await state.codex.start()
        try:
            return await state.service.repair_fork(
                branch_id,
                schemas.BranchForkRepair(
                    continuation="parallel" if parallel else "continue",
                    manual_approval=True,
                ),
            )
        finally:
            await state.codex.stop()

    branch = asyncio.run(run())
    provisioning = branch.context_snapshot.get("provisioning", {})
    typer.echo(
        json.dumps(
            {
                "branch_id": branch.id,
                "thread_id": branch.codex_thread_id,
                "status": branch.status,
                "provisioning": provisioning,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if branch.status == "blocked" or provisioning.get("state") == "failed":
        raise typer.Exit(code=1)


@app.command()
def attach(
    branch_id: str,
    execute: Annotated[bool, typer.Option("--exec", help="Replace shell with Codex")] = False,
) -> None:
    state = runtime()
    command = state.service.terminal_command(branch_id)
    typer.echo(command)
    if execute:
        open_branch(branch_id)


@app.command("windows")
def list_codex_windows(
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Include saved historical threads")
    ] = False,
    include_ignored: Annotated[bool, typer.Option(help="Include ignored sessions")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON")
    ] = False,
) -> None:
    """Scan and list local Codex TUI processes and resumable threads."""
    state = runtime()
    state.codex_windows.scan()
    windows = state.codex_windows.list_windows(include_ignored=include_ignored)
    if not all_sessions:
        windows = [item for item in windows if item["live"] or item["state"] == "managed"]
    if json_output:
        typer.echo(json.dumps(windows, ensure_ascii=False, indent=2, default=str))
        return
    typer.echo("STATE       PID      WORK         THREAD                  CWD")
    for item in windows:
        pid = str(item["pid"] or "-")
        work_id = str(item["work_id"] or "-")
        thread_id = str(item["thread_id"])
        typer.echo(f"{item['state']:<11} {pid:<8} {work_id:<12} {thread_id[:22]:<22} {item['cwd']}")


@app.command("sessions")
def list_sessions(
    all_sessions: Annotated[
        bool, typer.Option("--all", help="Include saved historical threads")
    ] = False,
    include_ignored: Annotated[bool, typer.Option(help="Include ignored sessions")] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON")
    ] = False,
) -> None:
    """List complete resumable Session and Branch IDs without requiring the Web service."""
    state = runtime()
    state.codex_windows.scan()
    windows = state.codex_windows.list_windows(include_ignored=include_ignored)
    if not all_sessions:
        windows = [item for item in windows if item["live"] or item["state"] == "managed"]
    if json_output:
        typer.echo(json.dumps(windows, ensure_ascii=False, indent=2, default=str))
        return
    if not windows:
        typer.echo("No resumable Codex sessions found")
        return
    for item in windows:
        state_label = str(item["state"])
        live_label = "live" if item["live"] else "saved"
        work_id = str(item["work_id"] or "unassigned")
        title = str(item["display_name"] or item["work_title"] or "Codex session")
        thread_id = str(item["thread_id"])
        branch_id = str(item["branch_id"] or "-")
        typer.echo(f"\n[{state_label}/{live_label}] {work_id} · {title}")
        typer.echo(f"  Session ID: {thread_id}")
        typer.echo(f"  Branch ID:  {branch_id}")
        typer.echo(f"  Directory:  {item['cwd']}")
        if item["branch_id"]:
            typer.echo(f"  Resume:     {PROJECT_ROOT / '.venv/bin/rops'} open {branch_id}")
        else:
            typer.echo(f"  Resume:     codex resume {thread_id}")


@app.command("adopt")
def adopt_codex_window(
    window_id: Annotated[str, typer.Argument(help="Registry ID or Codex thread UUID")],
    branch_id: Annotated[
        str | None,
        typer.Option("--branch", help="Attach to an existing Branch with the same cwd"),
    ] = None,
    program: Annotated[
        str,
        typer.Option("--program", help="Program for a newly created WorkItem"),
    ] = "FREE",
) -> None:
    """Attach a discovered Codex window or create work around its existing directory."""
    state = runtime()
    state.codex_windows.scan()
    if branch_id:
        payload: dict[str, object] = {"window": state.codex_windows.attach(window_id, branch_id)}
    else:
        window, work_item, branch = state.codex_windows.create_work(window_id, program)
        payload = {
            "window": window,
            "work_id": work_item.human_id,
            "branch_id": branch.id,
            "cwd": branch.cwd,
        }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@app.command("open")
def open_branch(
    branch_id: str,
    identity: Annotated[
        str | None, typer.Option(help="api, chatgpt, or oneapi; manual selection")
    ] = None,
    model: Annotated[str | None, typer.Option()] = None,
    reasoning_effort: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Open the Branch runtime in its managed local TUI."""
    state = runtime()
    detail = state.service.branch_detail(branch_id)
    if detail["runtime"] == "codex" and (
        identity is not None or model is not None or reasoning_effort is not None
    ):
        values = dict(detail["branch"].execution_preferences or {})
        if identity is not None:
            if identity != values.get("identity", "api"):
                values = {"identity": identity}
            else:
                values["identity"] = identity
        if model is not None:
            values["model"] = model
        if reasoning_effort is not None:
            values["reasoning_effort"] = reasoning_effort
        try:
            state.service.update_branch_execution(
                branch_id, ExecutionPreferences.model_validate(values)
            )
        except (ValueError, CodexUnavailable, ConflictError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        detail = state.service.branch_detail(branch_id)
    if detail["runtime"] == "claude":
        if identity is not None:
            typer.echo("Claude login is managed by its configured launcher", err=True)
            raise typer.Exit(2)
        if model is not None or reasoning_effort is not None:
            values = dict(detail["branch"].execution_preferences or {})
            if model is not None:
                values["model"] = model
            if reasoning_effort is not None:
                values["reasoning_effort"] = reasoning_effort
            try:
                state.service.update_claude_branch_execution(
                    branch_id, ClaudeExecutionPreferences.model_validate(values)
                )
            except (ValueError, ConflictError) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(2) from exc
            detail = state.service.branch_detail(branch_id)
    if detail["runtime"] == "pi":
        typer.echo(
            "Managed local Pi takeover is not available in this canary. "
            "Use the Orbitree Web composer and Pi runtime controls.",
            err=True,
        )
        raise typer.Exit(2)
    owner = f"terminal:{socket.gethostname()}:{os.getpid()}"
    try:
        if detail["runtime"] == "codex":
            state.identities.ensure_ready(
                detail["branch"].execution_preferences.get("identity", "api")
            )
        attempt = state.service.claim_terminal_session(branch_id, owner)
    except (ConflictError, CodexUnavailable) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    branch = detail["branch"]
    sessions_root = state.settings.codex_home / "sessions"
    excluded = state.codex_results.known_thread_ids()
    started_at = attempt.started_at or datetime.now(UTC)
    bound = bool(attempt.codex_thread_id and not attempt.codex_thread_id.startswith("deferred:"))
    if not bound:

        async def prepare_thread() -> str | None:
            adapter = (
                state.claude
                if detail["runtime"] == "claude"
                else adapter_for_execution(state.codex, attempt.execution_config)
            )
            await adapter.start()
            try:
                result = await adapter.start_thread(
                    cwd=branch.cwd,
                    instructions=developer_instructions(attempt.brief),
                )
                return result.get("thread", {}).get("id")
            finally:
                await adapter.stop()

        prepared_thread_id = asyncio.run(prepare_thread())
        if prepared_thread_id and not prepared_thread_id.startswith("deferred:"):
            state.codex_results.bind_thread(
                branch_id=branch_id,
                session_id=attempt.id,
                thread_id=prepared_thread_id,
                bound_at=started_at,
            )
            attempt.codex_thread_id = prepared_thread_id
            if detail["runtime"] == "codex":
                excluded.add(prepared_thread_id)
            bound = True
    command = state.service.codex_terminal_command(branch_id, execution=attempt.execution_config)
    process_args = shlex.split(command)
    typer.echo(f"Orbitree session {attempt.id}")
    typer.echo(f"Workspace: {branch.cwd}")
    typer.echo(command)

    process: subprocess.Popen[bytes] | None = None
    return_code = 1
    last_heartbeat = 0.0
    try:
        process = subprocess.Popen(
            process_args,
            cwd=branch.cwd,
            env=(
                state.identities.environment(attempt.execution_config["identity"])
                if detail["runtime"] == "codex"
                else os.environ.copy()
            ),
        )
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 15:
                try:
                    asyncio.run(run_db(
                        state.service.heartbeat_session,
                        branch_id,
                        attempt.id,
                        schemas.SessionHeartbeat(lease_id=attempt.lease_id),
                    ))
                except OperationalError as exc:
                    if not is_sqlite_busy(exc):
                        raise
                    typer.echo(
                        "Orbitree database busy; terminal continues, heartbeat deferred",
                        err=True,
                    )
                last_heartbeat = now
            if not bound and detail["runtime"] == "codex":
                discovered = find_new_rollout(
                    sessions_root,
                    cwd=branch.cwd,
                    since=started_at - timedelta(seconds=3),
                    excluded_thread_ids=excluded,
                )
                if discovered is not None:
                    _path, metadata = discovered
                    state.codex_results.bind_thread(
                        branch_id=branch_id,
                        session_id=attempt.id,
                        thread_id=metadata.thread_id,
                        bound_at=started_at,
                    )
                    attempt.codex_thread_id = metadata.thread_id
                    excluded.add(metadata.thread_id)
                    bound = True
            # Result reconciliation is owned by the API service. Running a
            # second synchronizer in every local TUI makes each window compete
            # with the service and other TUIs for SQLite's single write lock.
            polled = process.poll()
            if polled is not None:
                return_code = polled
                break
            time.sleep(2)
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait()
    except Exception as exc:
        typer.echo(f"Managed terminal failed: {exc}", err=True)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
    finally:
        if not bound and detail["runtime"] == "codex":
            discovered = find_new_rollout(
                sessions_root,
                cwd=branch.cwd,
                since=started_at - timedelta(seconds=3),
                excluded_thread_ids=excluded,
            )
            if discovered is not None:
                _path, metadata = discovered
                state.codex_results.bind_thread(
                    branch_id=branch_id,
                    session_id=attempt.id,
                    thread_id=metadata.thread_id,
                    bound_at=started_at,
                )
                bound = True
        if detail["runtime"] == "claude" and attempt.codex_thread_id:
            try:
                imported = state.claude_results.sync_session(
                    branch_id=branch_id,
                    session_id=attempt.id,
                    thread_id=attempt.codex_thread_id,
                    started_at=started_at,
                )
                if imported:
                    typer.echo(f"Imported {len(imported)} Claude turn(s) into Orbitree")
            except Exception as exc:
                typer.echo(f"Claude result import deferred: {exc}", err=True)
        try:
            asyncio.run(run_db(
                state.service.finish_terminal_session,
                branch_id,
                attempt.id,
                attempt.lease_id,
                return_code,
            ))
        except OperationalError as exc:
            if not is_sqlite_busy(exc):
                raise
            typer.echo(
                "Orbitree database busy; terminal exit will be reconciled by the server",
                err=True,
            )
    runtime_label = "Claude" if detail["runtime"] == "claude" else "Codex"
    typer.echo(f"{runtime_label} exited with status {return_code}")
    if return_code:
        raise typer.Exit(return_code)


@app.command()
def handoff(
    branch_id: str,
    conclusion: Annotated[str, typer.Option(prompt=True)],
    outcome: Annotated[str, typer.Option()] = "continue",
    next_action: Annotated[str | None, typer.Option()] = None,
) -> None:
    state = runtime()
    attempt = state.service.submit_handoff(
        branch_id,
        schemas.HandoffCreate(
            outcome=outcome,  # type: ignore[arg-type]
            conclusion=conclusion,
            next_action=next_action,
        ),
    )
    typer.echo(f"handoff saved for session {attempt.id}")


@app.command()
def doctor() -> None:
    state = runtime()

    async def inspect() -> dict[str, object]:
        try:
            codex = await state.codex.health()
        finally:
            await state.codex.stop()
        with state.database.session_factory() as session:
            return {
                "database": state.settings.resolved_database_url,
                "workspace_root": str(state.settings.workspace_root),
                "workspace_writable": os.access(state.settings.workspace_root, os.W_OK),
                "programs": session.scalar(select(func.count()).select_from(models.Program)) or 0,
                "work_items": session.scalar(select(func.count()).select_from(models.WorkItem))
                or 0,
                "codex": codex,
                "frontend_built": state.settings.frontend_dist.exists(),
            }

    typer.echo(json.dumps(asyncio.run(inspect()), ensure_ascii=False, indent=2))


@app.command("seed-demo")
def seed_demo() -> None:
    """Create local sample work without starting Codex threads."""
    state = runtime()
    with state.database.session_factory() as session:
        exists = session.scalar(select(models.WorkItem).limit(1))
    if exists:
        typer.echo("work items already exist; demo seed skipped")
        return
    samples = [
        (
            "HARN",
            "PPT editing trajectory verifier",
            "Build a deterministic verifier for editable PPT revision trajectories.",
            90,
        ),
        (
            "COST",
            "GLM FP8 paired short run",
            "Compare BF16 and FP8 under a frozen 500-step configuration.",
            80,
        ),
        (
            "ADAPT",
            "New architecture forward parity",
            "Reach forward and checkpoint conversion parity before the training smoke test.",
            70,
        ),
        (
            "FREE",
            "Trajectory tree prototype",
            "Explore a compact visualization for execution branches.",
            30,
        ),
    ]
    for program, title, objective, priority in samples:
        work_item, branch = state.service.create_work_item(
            schemas.WorkItemCreate(
                program_code=program,
                title=title,
                objective=objective,
                priority=priority,
                acceptance_criteria=[
                    schemas.AcceptanceCriterion(description="Review the resulting artifact")
                ],
                branch_mode="write" if program != "FREE" else "capture",
            )
        )
        typer.echo(f"created {work_item.human_id} branch={branch.id}")


if __name__ == "__main__":
    app()
