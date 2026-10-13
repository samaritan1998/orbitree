from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from mimetypes import guess_type
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from research_ops import models, schemas
from research_ops.agentos import (
    AgentOSClient,
    AgentOSClientError,
    AgentOSConfigurationError,
    AgentOSHTTPError,
)
from research_ops.api_terminal import terminal_router
from research_ops.codex.base import CodexAdapter, CodexUnavailable
from research_ops.codex.claude import ClaudeCliAdapter
from research_ops.codex.claude_execution import ClaudeExecutionPreferences
from research_ops.codex.identity import CodexIdentityService, ExecutionPreferences
from research_ops.codex.identity_pool import IdentityCodexPool
from research_ops.codex.pi import PiRpcAdapter
from research_ops.codex.reflection import CodexReflectionAgent, ReflectionAgent
from research_ops.codex.workflow_analysis import CodexWorkflowAnalyst, WorkflowAnalyst
from research_ops.db import Database, run_db
from research_ops.events import EventHub
from research_ops.infoflow import InfoflowMessageClient
from research_ops.ltp import BundleValidationError, LtpControlLiveAdapter
from research_ops.octo import OctoClient
from research_ops.openreward.environment import ORSEnvironment
from research_ops.openreward.server import ORSServer
from research_ops.runtime_storage import prepare_runtime_database
from research_ops.services import (
    AgentOSJobSync,
    AgentOSSubmissionConflict,
    ClusterOverviewService,
    ClusterOverviewUnavailable,
    CodexWindowRegistry,
    DagJobSync,
    EvalHarnessService,
    GoalService,
    GoalSupervisor,
    LocalCodexLiveSync,
    LtpCliTaskSource,
    LtpMonitorService,
    ManagedJobService,
    NativeGoalService,
    ResearchOpsService,
    RuntimeModelSettingsService,
    SkillProposalService,
    TurnNotificationService,
    WorkspaceManager,
    bootstrap_catalog,
)
from research_ops.services.claude_results import ClaudeResultSync
from research_ops.services.claude_wakeups import ClaudeWakeupService, ClaudeWakeupWorker
from research_ops.services.codex_events import CodexEventService
from research_ops.services.codex_results import CodexResultSync
from research_ops.services.core import ConflictError, NotFoundError
from research_ops.services.daily_reflections import DailyReflectionService
from research_ops.services.model_catalog import CodexModelCatalog
from research_ops.services.native_goal_history import NativeGoalHistoryService
from research_ops.services.scheduled_check_prompts import (
    CheckPromptUnavailable,
    ScheduledCheckPromptService,
)
from research_ops.services.scheduled_checks import ScheduledCheckService, ScheduledCheckWorker
from research_ops.services.session_workflows import SessionWorkflowService
from research_ops.services.web_terminal import WebTerminal
from research_ops.services.workflow_links import WorkflowLinkService
from research_ops.settings import PROJECT_ROOT, Settings, get_settings

logger = logging.getLogger(__name__)

RESULT_IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
RESULT_IMAGE_MAX_BYTES = 25 * 1024 * 1024
SVG_RESULT_CSP = (
    "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; "
    "img-src data:; style-src 'unsafe-inline'"
)


class AppState:
    def __init__(
        self,
        settings: Settings,
        codex: CodexAdapter | None = None,
        pi: CodexAdapter | None = None,
        agentos_client: AgentOSClient | None = None,
        reflection_agent: ReflectionAgent | None = None,
        workflow_analyst: WorkflowAnalyst | None = None,
        octo_client: OctoClient | None = None,
        claude: CodexAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.web_terminal = WebTerminal(settings)
        self.runtime_model_settings = RuntimeModelSettingsService(settings)
        self.identities = CodexIdentityService(settings)
        self.model_catalog = CodexModelCatalog(settings)
        self.database = Database(settings)
        self.event_hub = EventHub()
        self.infoflow = InfoflowMessageClient(
            base_url=settings.infoflow_base_url,
            app_key_env=settings.infoflow_app_key_env,
            app_secret_env=settings.infoflow_app_secret_env,
            credentials_file=settings.infoflow_credentials_file,
            timeout_seconds=settings.infoflow_timeout_seconds,
            verify=settings.infoflow_verify_tls,
            trust_env=settings.infoflow_trust_env,
        )
        self.turn_notifications = TurnNotificationService(
            self.database.session_factory,
            settings,
            self.infoflow,
        )
        self.daily_reflections = DailyReflectionService(
            self.database.session_factory,
            settings,
            reflection_agent or CodexReflectionAgent(settings),
            self.infoflow,
        )
        self.codex_events = CodexEventService(
            self.database.session_factory,
            self.event_hub,
            notification_recipient=(
                settings.infoflow_recipient if settings.infoflow_notifications_enabled else None
            ),
        )
        self.codex_results = CodexResultSync(
            self.database.session_factory,
            settings.codex_home / "sessions",
            max_line_bytes=settings.codex_event_line_limit_bytes,
            notification_recipient=(
                settings.infoflow_recipient if settings.infoflow_notifications_enabled else None
            ),
        )
        self.codex_live = LocalCodexLiveSync(
            self.database.session_factory,
            max_line_bytes=settings.codex_event_line_limit_bytes,
            replay_bytes=settings.codex_local_live_replay_bytes,
            max_events=settings.codex_local_live_max_events,
            max_content_chars=settings.codex_local_live_max_content_chars,
        )
        self.claude_results = ClaudeResultSync(
            self.database.session_factory,
            settings.claude_home / "projects",
            max_line_bytes=settings.claude_event_line_limit_bytes,
            notification_recipient=(
                settings.infoflow_recipient if settings.infoflow_notifications_enabled else None
            ),
        )
        global_pi = settings.pi_enabled or settings.codex_backend == "pi"
        if pi is not None:
            self.pi = pi
        elif codex is not None:
            # Test and embedded callers may supply one adapter for either
            # selected runtime. Cross-runtime tests pass ``pi=`` explicitly.
            self.pi = codex
        else:
            self.pi = PiRpcAdapter(
                settings,
                event_handler=self.codex_events.handle_event,
                request_handler=self.codex_events.handle_request,
            )
        self.claude = claude or ClaudeCliAdapter(
            settings,
            event_handler=self.codex_events.handle_event,
            request_handler=self.codex_events.handle_request,
        )
        if codex is not None:
            self.codex = codex
        elif global_pi:
            self.codex = self.pi
        else:
            self.codex = IdentityCodexPool(
                settings,
                event_handler=self.codex_events.handle_event,
                request_handler=self.codex_events.handle_request,
            )
        self.codex_events.adapter = self.codex
        self.codex_events.claude_adapter = self.claude
        self.workspace = WorkspaceManager(settings)
        self.service = ResearchOpsService(
            session_factory=self.database.session_factory,
            settings=settings,
            workspace=self.workspace,
            codex=self.codex,
            pi=self.pi,
            claude=self.claude,
            event_hub=self.event_hub,
        )
        self.goals = GoalService(self.database.session_factory, settings)
        self.scheduled_checks = ScheduledCheckService(self.database.session_factory, settings)
        self.scheduled_check_prompts = ScheduledCheckPromptService(
            self.database.session_factory, settings,
        )
        self.scheduled_check_worker = ScheduledCheckWorker(
            self.scheduled_checks, self.service, self.event_hub
        )
        self.claude_wakeups = ClaudeWakeupService(self.database.session_factory, settings)
        self.claude_wakeup_worker = ClaudeWakeupWorker(
            self.claude_wakeups, self.service, self.event_hub
        )
        self.codex_events.claude_wakeup_observer = self.claude_wakeup_worker.observe
        self.native_goal_history = NativeGoalHistoryService(self.database.session_factory)
        self.native_goals = NativeGoalService(
            self.database.session_factory,
            settings,
            self.codex,
        )
        self.codex_events.goal_observer = self.native_goals.observe_event
        self.goal_supervisor = GoalSupervisor(
            self.goals,
            self.service,
            self.event_hub,
            settings,
        )
        self.codex_windows = CodexWindowRegistry(
            session_factory=self.database.session_factory,
            settings=settings,
            service=self.service,
            result_sync=self.codex_results,
        )
        self.skill_proposals = SkillProposalService(
            session_factory=self.database.session_factory,
            settings=settings,
        )
        self.dag_jobs = DagJobSync(
            self.database.session_factory,
            max_bytes=settings.dag_state_max_bytes,
        )
        self.session_workflows = SessionWorkflowService(
            self.database.session_factory, settings,
            octo_client or OctoClient(settings),
            workflow_analyst or CodexWorkflowAnalyst(settings),
        )
        self.workflow_links = WorkflowLinkService(self.database.session_factory, settings)
        self.eval_harness = EvalHarnessService(self.database.session_factory)
        self.cluster_overview = ClusterOverviewService(
            LtpControlLiveAdapter(
                enabled=settings.cluster_dashboard_enabled,
                command=settings.ltp_control_command,
                python_command=settings.ltp_control_python_command,
                source_root=settings.ltp_control_source_root,
                snapshot_path=settings.ltp_control_snapshot_path,
                directory_user=settings.ltp_control_directory_user,
                allow_partial_users=settings.ltp_control_allow_partial_users,
                state_dir=settings.resolved_ltp_control_state_dir,
                profiles_file=settings.ltp_control_profiles_file,
                query_timeout_seconds=settings.ltp_control_query_timeout_seconds,
                process_timeout_seconds=settings.ltp_control_process_timeout_seconds,
                max_response_bytes=settings.ltp_control_max_response_bytes,
            ),
            cache_seconds=settings.cluster_dashboard_cache_seconds,
            stale_seconds=settings.cluster_dashboard_stale_seconds,
        )
        self.managed_jobs = ManagedJobService(
            self.database.session_factory,
            settings,
            self.cluster_overview,
        )
        self.ltp_monitor = LtpMonitorService(
            self.database.session_factory,
            settings,
            LtpCliTaskSource(
                command=settings.ltp_monitor_cli,
                api_url=settings.ltp_monitor_cli_api_url,
                timeout_seconds=settings.ltp_monitor_status_probe_timeout_seconds,
            ),
            self.infoflow,
        )
        self.agentos_jobs = AgentOSJobSync(
            self.database.session_factory,
            settings,
            client=agentos_client,
            reconcile_work=self.service.reconcile_external_activity,
        )


def create_app(
    settings: Settings | None = None,
    codex: CodexAdapter | None = None,
    pi: CodexAdapter | None = None,
    agentos_client: AgentOSClient | None = None,
    openreward_environments: dict[str, type[ORSEnvironment]] | None = None,
    reflection_agent: ReflectionAgent | None = None,
    workflow_analyst: WorkflowAnalyst | None = None,
    octo_client: OctoClient | None = None,
    claude: CodexAdapter | None = None,
) -> FastAPI:
    # ``BaseSettings`` reads ROPS_* fields from .env, while provider secrets
    # remain ordinary process environment references resolved by adapters.
    settings = settings or get_settings()
    if settings.load_project_env:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    if settings.require_access_token and not settings.access_token:
        raise ValueError("A private worker requires an access token")
    state = AppState(
        settings,
        codex,
        pi,
        agentos_client,
        reflection_agent,
        workflow_analyst,
        octo_client,
        claude,
    )
    ors_server: ORSServer | None = None
    if settings.openreward_enabled or openreward_environments is not None:
        from research_ops.openreward import (
            build_default_environments,
            build_environments_from_contract,
            load_task_catalogs,
        )

        if openreward_environments is not None:
            ors_environments = openreward_environments
        elif settings.openreward_backend_mode == "contract":
            ors_environments = build_environments_from_contract(
                settings.openreward_backend_config,
                real_domains=settings.openreward_real_domains,
                task_catalog_path=settings.openreward_task_catalog,
            )
        else:
            catalogs = (
                load_task_catalogs(settings.openreward_task_catalog)
                if settings.openreward_task_catalog is not None
                else None
            )
            ors_environments = build_default_environments(task_catalogs=catalogs)
        ors_server = ORSServer(
            ors_environments,
            session_ttl_seconds=settings.openreward_session_ttl_seconds,
            api_key_env=settings.openreward_api_key_env,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        await asyncio.to_thread(prepare_runtime_database, settings)
        state.database.init()
        bootstrap_catalog(state.database.session_factory, settings)
        await run_db(state.codex_events.expire_detached_claude_approvals)
        await run_db(state.native_goal_history.backfill)
        if ors_server is not None:
            await ors_server.start()
        if settings.codex_enabled:
            with suppress(Exception):
                await state.codex.start()
        native_goal_recovery_task = (
            asyncio.create_task(_supervise_native_goals(state))
            if settings.codex_enabled and settings.native_goal_supervision_enabled
            else None
        )
        result_sync_task = (
            asyncio.create_task(_sync_codex_results(state))
            if settings.codex_enabled and settings.codex_result_sync_enabled
            else None
        )
        continuation_task = (
            asyncio.create_task(_dispatch_branch_continuations(state))
            if settings.codex_enabled
            else None
        )
        local_live_task = (
            asyncio.create_task(_sync_local_codex_live(state))
            if settings.codex_enabled and settings.codex_local_live_enabled
            else None
        )
        registry_task = (
            asyncio.create_task(_scan_codex_windows(state))
            if settings.codex_registry_enabled
            else None
        )
        dag_job_task = (
            asyncio.create_task(_sync_dag_jobs(state))
            if settings.dag_job_sync_enabled
            else None
        )
        agentos_job_task = (
            asyncio.create_task(_sync_agentos_jobs(state))
            if (
                settings.agentos_enabled
                and settings.agentos_job_sync_enabled
                and state.agentos_jobs.configured
            )
            else None
        )
        managed_job_task = (
            asyncio.create_task(_sync_managed_jobs(state))
            if settings.managed_jobs_enabled
            else None
        )
        ltp_monitor_task = (
            asyncio.create_task(_sync_ltp_monitor(state))
            if settings.ltp_monitor_enabled
            else None
        )
        goal_task = (
            asyncio.create_task(_dispatch_goals(state))
            if settings.goals_enabled
            else None
        )
        scheduled_check_task = (
            asyncio.create_task(_dispatch_scheduled_checks(state))
            if settings.scheduled_checks_enabled else None
        )
        notification_task = (
            asyncio.create_task(_dispatch_turn_notifications(state))
            if settings.infoflow_notifications_enabled
            else None
        )
        reflection_task = (
            asyncio.create_task(_run_daily_reflections(state))
            if settings.codex_enabled and settings.daily_reflection_enabled
            else None
        )
        try:
            yield
        finally:
            await state.web_terminal.close()
            await state.scheduled_check_prompts.stop()
            if scheduled_check_task is not None:
                scheduled_check_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduled_check_task
                await state.scheduled_check_worker.stop()
                await state.claude_wakeup_worker.stop()
            if native_goal_recovery_task is not None:
                native_goal_recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await native_goal_recovery_task
            if ltp_monitor_task is not None:
                ltp_monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ltp_monitor_task
            if goal_task is not None:
                goal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await goal_task
                await state.goal_supervisor.stop()
            if managed_job_task is not None:
                managed_job_task.cancel()
                with suppress(asyncio.CancelledError):
                    await managed_job_task
            if reflection_task is not None:
                reflection_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reflection_task
            if notification_task is not None:
                notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await notification_task
            if agentos_job_task is not None:
                agentos_job_task.cancel()
                with suppress(asyncio.CancelledError):
                    await agentos_job_task
            if ors_server is not None:
                await ors_server.stop()
            if dag_job_task is not None:
                dag_job_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dag_job_task
            if registry_task is not None:
                registry_task.cancel()
                with suppress(asyncio.CancelledError):
                    await registry_task
            if local_live_task is not None:
                local_live_task.cancel()
                with suppress(asyncio.CancelledError):
                    await local_live_task
            if result_sync_task is not None:
                result_sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await result_sync_task
            if continuation_task is not None:
                continuation_task.cancel()
                with suppress(asyncio.CancelledError):
                    await continuation_task
            await state.codex.stop()
            if state.pi is not state.codex:
                await state.pi.stop()
            if state.claude is not state.codex and state.claude is not state.pi:
                await state.claude.stop()

    app = FastAPI(title="Research Ops", version="0.1.0", lifespan=lifespan)
    app.state.rops = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(build_router(state))
    app.include_router(terminal_router(state.web_terminal))
    if settings.require_access_token:
        from research_ops.team.security import WorkerTokenMiddleware

        app.add_middleware(WorkerTokenMiddleware, token=settings.access_token)
    if ors_server is not None:
        # Mounting keeps the standard ORS paths intact below a configurable
        # prefix while leaving the existing Research Ops API untouched.
        app.mount(
            "/openreward",
            ors_server.create_app(),
            name="openreward",
        )
    install_error_handlers(app)
    _mount_frontend(app, settings.frontend_dist)
    return app


async def _sync_codex_results(state: AppState) -> None:
    loop = asyncio.get_running_loop()
    next_prompt_backfill = loop.time()
    while True:
        try:
            synced = await asyncio.to_thread(state.codex_results.sync_all)
            for item in synced:
                turn = schemas.CodexTurnRead.model_validate(item.turn).model_dump(mode="json")
                await state.event_hub.publish(
                    item.branch_id, {"type": "turn.synced", "turn": turn}
                )
            for branch_id in {item.branch_id for item in synced}:
                detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
                await state.event_hub.publish(
                    branch_id,
                    {
                        "type": "conversation.state",
                        "conversation_state": detail["conversation_state"],
                    },
                )
            now = loop.time()
            if now >= next_prompt_backfill:
                next_prompt_backfill = now + state.settings.codex_prompt_backfill_interval_seconds
                await asyncio.to_thread(state.codex_results.backfill_missing_prompts)
                backfilled_keys = state.codex_results.take_backfilled_turn_keys()
                if backfilled_keys:
                    backfilled = await asyncio.to_thread(
                        _load_backfilled_turn_payloads, state, backfilled_keys
                    )
                    for branch_id, turn in backfilled:
                        await state.event_hub.publish(
                            branch_id, {"type": "turn.synced", "turn": turn}
                        )
            interrupted = await asyncio.to_thread(
                state.service.reconcile_web_turn_priority
            )
            for branch_id in interrupted:
                detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
                await state.event_hub.publish(
                    branch_id,
                    {
                        "type": "conversation.state",
                        "conversation_state": detail["conversation_state"],
                    },
                )
        except Exception:
            logger.exception("Failed to synchronize Codex turn results")
        await asyncio.sleep(state.settings.codex_result_sync_interval_seconds)


def _load_backfilled_turn_payloads(
    state: AppState, turn_keys: set[tuple[str, str]]
) -> list[tuple[str, dict[str, Any]]]:
    """Serialize turns recovered by the background prompt repair pass."""
    by_branch: dict[str, set[str]] = {}
    for branch_id, external_turn_id in turn_keys:
        by_branch.setdefault(branch_id, set()).add(external_turn_id)
    payloads: list[tuple[str, dict[str, Any]]] = []
    with state.database.session_factory() as session:
        for branch_id, external_ids in by_branch.items():
            turns = session.scalars(
                select(models.CodexTurn).where(
                    models.CodexTurn.branch_id == branch_id,
                    models.CodexTurn.external_turn_id.in_(external_ids),
                )
            )
            for turn in turns:
                payloads.append(
                    (
                        branch_id,
                        schemas.CodexTurnRead.model_validate(turn).model_dump(mode="json"),
                    )
                )
    return payloads


async def _dispatch_branch_continuations(state: AppState) -> None:
    """Keep foreground child forks alive across CLI exits and service restarts."""
    while True:
        try:
            await state.service.dispatch_queued_continuations()
        except Exception:
            logger.exception("Failed to dispatch child Branch continuations")
        await asyncio.sleep(state.settings.codex_continuation_dispatch_interval_seconds)


async def _scan_codex_windows(state: AppState) -> None:
    while True:
        try:
            await asyncio.to_thread(state.codex_windows.scan)
        except Exception:
            logger.exception("Failed to scan local Codex windows")
        if state.settings.codex_title_generation_enabled:
            try:
                await asyncio.to_thread(state.codex_windows.generate_titles)
            except Exception:
                logger.exception("Failed to generate Codex window titles")
        await asyncio.sleep(state.settings.codex_registry_scan_interval_seconds)


async def _sync_local_codex_live(state: AppState) -> None:
    while True:
        try:
            events = await asyncio.to_thread(state.codex_live.poll)
            pending_branch: str | None = None
            pending_events: list[dict[str, Any]] = []

            async def flush() -> None:
                nonlocal pending_branch, pending_events
                if pending_branch and pending_events:
                    await state.event_hub.publish(
                        pending_branch,
                        {"type": "local.live.batch", "events": pending_events},
                    )
                pending_branch = None
                pending_events = []

            for item in events:
                if item.event.get("type") == "local.live":
                    if pending_branch not in {None, item.branch_id}:
                        await flush()
                    pending_branch = item.branch_id
                    event = item.event.get("event")
                    if isinstance(event, dict):
                        pending_events.append(event)
                    continue
                await flush()
                await state.event_hub.publish(item.branch_id, item.event)
            await flush()
        except Exception:
            logger.exception("Failed to synchronize local Codex live activity")
        await asyncio.sleep(state.settings.codex_local_live_interval_seconds)


async def _sync_dag_jobs(state: AppState) -> None:
    while True:
        try:
            await asyncio.to_thread(state.dag_jobs.sync_all)
        except Exception:
            logger.exception("Failed to synchronize registered DAG jobs")
        await asyncio.sleep(state.settings.dag_job_sync_interval_seconds)


async def _sync_agentos_jobs(state: AppState) -> None:
    while True:
        try:
            await asyncio.to_thread(state.agentos_jobs.sync_all)
        except Exception:
            logger.exception("Failed to synchronize AgentOS jobs")
        await asyncio.sleep(state.settings.agentos_job_sync_interval_seconds)


async def _sync_managed_jobs(state: AppState) -> None:
    await asyncio.to_thread(state.managed_jobs.recover_interrupted_dispatches)
    while True:
        try:
            await asyncio.to_thread(state.managed_jobs.reconcile)
            await asyncio.to_thread(state.managed_jobs.dispatch_one)
        except Exception:
            logger.exception("Failed to synchronize managed jobs")
        await asyncio.sleep(state.settings.managed_job_dispatch_interval_seconds)


async def _sync_ltp_monitor(state: AppState) -> None:
    """Keep the LTP task ledger and its group-message outbox independent of Sessions."""

    while True:
        started_at = asyncio.get_running_loop().time()
        try:
            result = await asyncio.to_thread(state.ltp_monitor.sync)
            deliveries = await asyncio.to_thread(state.ltp_monitor.dispatch_notifications)
            if result["errors"]:
                logger.warning("LTP monitor source returned %s error(s)", result["errors"])
            for delivery in deliveries:
                if delivery.status == "failed":
                    logger.warning(
                        "LTP monitor group notification failed (%s): %s",
                        delivery.notification_id,
                        delivery.error,
                    )
        except Exception:
            logger.exception("Failed to synchronize LTP monitor")
        elapsed = asyncio.get_running_loop().time() - started_at
        await asyncio.sleep(
            max(0.0, state.settings.ltp_monitor_poll_interval_seconds - elapsed)
        )


async def _dispatch_goals(state: AppState) -> None:
    """Run due Goals from durable state; recovery is lease-based, not in-memory."""

    while True:
        try:
            await state.goal_supervisor.dispatch_due()
        except Exception:
            logger.exception("Failed to dispatch durable Goals")
        await asyncio.sleep(state.settings.goal_dispatch_interval_seconds)


async def _dispatch_scheduled_checks(state: AppState) -> None:
    while True:
        try:
            await state.scheduled_check_worker.tick()
            await state.claude_wakeup_worker.tick()
        except Exception as exc:
            state.scheduled_check_worker.last_error = type(exc).__name__
            logger.exception("Scheduled check worker failed")
        await asyncio.sleep(state.settings.scheduled_check_tick_seconds)


async def _supervise_native_goals(state: AppState) -> None:
    discover = True
    while True:
        try:
            await state.native_goals.monitor_once(discover=discover)
            discover = False
        except Exception:
            logger.exception("Native Goal supervision failed")
        await asyncio.sleep(state.settings.native_goal_check_interval_seconds)


async def _dispatch_turn_notifications(state: AppState) -> None:
    while True:
        try:
            deliveries = await asyncio.to_thread(state.turn_notifications.dispatch_pending)
            for delivery in deliveries:
                if delivery.status == "failed":
                    logger.warning(
                        "Infoflow turn notification failed (%s): %s",
                        delivery.notification_id,
                        delivery.error,
                    )
        except Exception:
            logger.exception("Failed to dispatch turn notifications")
        await asyncio.sleep(state.settings.infoflow_dispatch_interval_seconds)


async def _run_daily_reflections(state: AppState) -> None:
    while True:
        try:
            await state.daily_reflections.run_due()
            await asyncio.to_thread(state.daily_reflections.dispatch_notifications)
        except Exception:
            logger.exception("Failed to run or deliver daily reflection")
        await asyncio.sleep(state.settings.daily_reflection_poll_interval_seconds)


def build_router(state: AppState) -> APIRouter:
    router = APIRouter(prefix="/api")

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        token = state.settings.access_token
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    write_guard = [Depends(authorize)]

    @router.get("/health", response_model=schemas.HealthResponse, tags=["system"])
    async def health() -> schemas.HealthResponse:
        codex_health = await state.codex.health()
        return schemas.HealthResponse(
            status="ok",
            database=state.settings.resolved_database_url.split(":", 1)[0],
            codex=codex_health,
            workspace_root=str(state.settings.workspace_root),
            frontend_built=state.settings.frontend_dist.exists(),
        )

    @router.get(
        "/settings/runtime-model",
        response_model=schemas.RuntimeModelSettingsRead,
        tags=["settings"],
    )
    def runtime_model_settings() -> dict[str, Any]:
        return state.runtime_model_settings.read()

    @router.patch(
        "/settings/runtime-model",
        response_model=schemas.RuntimeModelSettingsRead,
        dependencies=write_guard,
        tags=["settings"],
    )
    def update_runtime_model_settings(
        payload: schemas.RuntimeModelSettingsUpdate,
    ) -> dict[str, Any]:
        return state.runtime_model_settings.update(payload.model_dump(exclude_none=True))

    @router.get("/settings/identities", tags=["settings"])
    def identity_status() -> dict[str, Any]:
        return state.identities.status()

    @router.get("/settings/identities/{identity}/models", tags=["settings"])
    async def identity_models(
        identity: Literal["api", "chatgpt", "oneapi"], refresh: bool = False
    ) -> dict[str, Any]:
        return await state.model_catalog.list_models(identity, refresh=refresh)

    @router.get("/branches/{branch_id}/execution", tags=["sessions"])
    def branch_execution(branch_id: str) -> dict[str, Any]:
        return state.service.branch_execution(branch_id)

    @router.put("/branches/{branch_id}/execution", dependencies=write_guard, tags=["sessions"])
    def update_branch_execution(branch_id: str, payload: ExecutionPreferences) -> dict[str, Any]:
        return state.service.update_branch_execution(branch_id, payload)

    @router.get("/branches/{branch_id}/claude-execution", tags=["sessions"])
    def claude_branch_execution(branch_id: str) -> dict[str, Any]:
        return state.service.claude_branch_execution(branch_id)

    @router.put(
        "/branches/{branch_id}/claude-execution", dependencies=write_guard, tags=["sessions"]
    )
    def update_claude_branch_execution(
        branch_id: str, payload: ClaudeExecutionPreferences
    ) -> dict[str, Any]:
        return state.service.update_claude_branch_execution(branch_id, payload)

    @router.get("/programs", response_model=list[schemas.ProgramRead], tags=["programs"])
    def programs(include_archived: bool = False) -> list[models.Program]:
        return state.service.list_programs(include_archived)

    @router.post("/programs", response_model=schemas.ProgramRead, dependencies=write_guard)
    def create_program(payload: schemas.ProgramCreate) -> models.Program:
        return state.service.create_program(payload)

    @router.put("/programs/{code}", response_model=schemas.ProgramRead, dependencies=write_guard)
    def update_program(code: str, payload: schemas.ProgramUpdate) -> models.Program:
        return state.service.update_program(code, payload)

    @router.get("/team/session")
    def standalone_session() -> dict[str, bool]:
        return {"enabled": False}

    @router.get(
        "/programs/{program_code}/tree", response_model=schemas.TreeResponse, tags=["programs"]
    )
    def tree(program_code: str) -> schemas.TreeResponse:
        program, work_items, branches, hidden_branches, edges, sessions = (
            state.service.program_tree(program_code)
        )
        return schemas.TreeResponse(
            program=schemas.ProgramRead.model_validate(program),
            work_items=[schemas.WorkItemRead.model_validate(item) for item in work_items],
            branches=[schemas.BranchRead.model_validate(item) for item in branches],
            hidden_branches=[
                schemas.BranchRead.model_validate(item) for item in hidden_branches
            ],
            sessions=[schemas.SessionRead.model_validate(item) for item in sessions],
            edges=[
                {
                    "from_work_item_id": edge.from_work_item_id,
                    "to_work_item_id": edge.to_work_item_id,
                    "edge_type": edge.edge_type,
                }
                for edge in edges
            ],
        )

    @router.get(
        "/programs/{program_code}/desk",
        response_model=schemas.BranchDetail,
        tags=["programs"],
    )
    def program_desk(program_code: str) -> dict[str, Any]:
        branch = state.service.program_desk_branch(program_code)
        return state.service.branch_detail(branch.id)

    @router.get(
        "/programs/{program_code}/desk-context",
        response_model=dict[str, Any],
        tags=["programs"],
    )
    def program_desk_context(program_code: str) -> dict[str, Any]:
        return state.service.program_context(program_code)

    @router.get("/today", response_model=schemas.TodayResponse, tags=["work"])
    def today() -> dict[str, Any]:
        return state.service.today()

    @router.get(
        "/clusters/overview",
        response_model=schemas.ClusterOverviewResponse,
        dependencies=write_guard,
        tags=["clusters"],
    )
    async def cluster_overview(
        refresh: bool = Query(default=False),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(state.cluster_overview.overview, refresh=refresh)
        except ClusterOverviewUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from None

    @router.get(
        "/ltp-monitor",
        response_model=schemas.LtpMonitorOverviewRead,
        tags=["ltp-monitor"],
    )
    async def ltp_monitor() -> dict[str, Any]:
        return await asyncio.to_thread(state.ltp_monitor.overview)

    @router.post(
        "/ltp-monitor/refresh",
        response_model=schemas.LtpMonitorOverviewRead,
        dependencies=write_guard,
        tags=["ltp-monitor"],
    )
    async def refresh_ltp_monitor() -> dict[str, Any]:
        await asyncio.to_thread(state.ltp_monitor.sync)
        await asyncio.to_thread(state.ltp_monitor.dispatch_notifications)
        return await asyncio.to_thread(state.ltp_monitor.overview)

    @router.post(
        "/ltp-monitor/tasks",
        response_model=schemas.LtpMonitoredJobRead,
        dependencies=write_guard,
        tags=["ltp-monitor"],
    )
    async def track_ltp_monitor_task(
        payload: schemas.LtpMonitorTrackCreate,
    ) -> models.LtpMonitoredJob:
        try:
            return await asyncio.to_thread(
                state.ltp_monitor.track_job,
                job_name=payload.job_name,
                user=payload.user,
                cluster=payload.cluster,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from None

    @router.post(
        "/managed-jobs",
        response_model=schemas.ManagedJobRead,
        dependencies=write_guard,
        tags=["managed-jobs"],
    )
    async def submit_managed_job(payload: schemas.ManagedJobSubmit) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(state.managed_jobs.submit, payload)
        except BundleValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from None

    @router.get(
        "/managed-jobs",
        response_model=list[schemas.ManagedJobRead],
        dependencies=write_guard,
        tags=["managed-jobs"],
    )
    async def list_managed_jobs(
        submitter: str = Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            state.managed_jobs.list,
            submitter=submitter,
            limit=limit,
        )

    @router.get(
        "/managed-jobs/{job_id}",
        response_model=schemas.ManagedJobRead,
        dependencies=write_guard,
        tags=["managed-jobs"],
    )
    async def managed_job(
        job_id: str,
        submitter: str = Query(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            state.managed_jobs.get,
            job_id,
            submitter=submitter,
        )

    @router.get(
        "/codex-windows",
        response_model=list[schemas.CodexWindowRead],
        tags=["sessions"],
    )
    async def codex_windows(
        include_ignored: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        await asyncio.to_thread(state.codex_windows.scan)
        return state.codex_windows.list_windows(include_ignored=include_ignored)

    @router.post(
        "/codex-windows/scan",
        response_model=list[schemas.CodexWindowRead],
        dependencies=write_guard,
        tags=["sessions"],
    )
    async def scan_codex_windows() -> list[dict[str, Any]]:
        return await asyncio.to_thread(state.codex_windows.scan)

    @router.post(
        "/codex-windows/generate-titles",
        response_model=list[schemas.CodexWindowRead],
        dependencies=write_guard,
        tags=["sessions"],
    )
    async def generate_codex_window_titles() -> list[dict[str, Any]]:
        await asyncio.to_thread(state.codex_windows.scan)
        return await asyncio.to_thread(state.codex_windows.generate_titles, force=True)

    @router.get(
        "/codex-windows/{window_id}/attach-targets",
        response_model=list[schemas.CodexWindowTarget],
        tags=["sessions"],
    )
    def codex_window_targets(window_id: str) -> list[dict[str, Any]]:
        return state.codex_windows.attach_targets(window_id)

    @router.post(
        "/codex-windows/{window_id}/attach",
        response_model=schemas.CodexWindowRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def attach_codex_window(
        window_id: str, payload: schemas.CodexWindowAttach
    ) -> dict[str, Any]:
        return state.codex_windows.attach(window_id, payload.branch_id)

    @router.post(
        "/codex-windows/{window_id}/create-work",
        response_model=schemas.CodexWindowAdoption,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def create_work_from_codex_window(
        window_id: str, payload: schemas.CodexWindowCreateWork
    ) -> dict[str, Any]:
        window, work_item, branch = state.codex_windows.create_work(
            window_id, payload.program_code
        )
        return {"window": window, "work_item": work_item, "branch": branch}

    @router.post(
        "/codex-windows/{window_id}/attach-parent",
        response_model=schemas.CodexWindowAdoption,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def attach_codex_window_under_parent(window_id: str) -> dict[str, Any]:
        window, work_item, branch = state.codex_windows.attach_under_parent(window_id)
        return {"window": window, "work_item": work_item, "branch": branch}

    @router.post(
        "/codex-windows/{window_id}/ignore",
        response_model=schemas.CodexWindowRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def ignore_codex_window(window_id: str) -> dict[str, Any]:
        return state.codex_windows.ignore(window_id, ignored=True)

    @router.post(
        "/codex-windows/{window_id}/restore",
        response_model=schemas.CodexWindowRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def restore_codex_window(window_id: str) -> dict[str, Any]:
        return state.codex_windows.ignore(window_id, ignored=False)

    @router.patch(
        "/codex-windows/{window_id}/title",
        response_model=schemas.CodexWindowRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def rename_codex_window(
        window_id: str, payload: schemas.CodexWindowTitleUpdate
    ) -> dict[str, Any]:
        return state.codex_windows.rename(window_id, payload.title)

    @router.post(
        "/work-items", response_model=schemas.WorkItemRead, dependencies=write_guard, tags=["work"]
    )
    def create_work_item(payload: schemas.WorkItemCreate) -> models.WorkItem:
        work_item, _branch = state.service.create_work_item(payload)
        return work_item

    @router.post(
        "/work-items/{work_id}/definition",
        response_model=schemas.WorkItemRead,
        dependencies=write_guard,
        tags=["work"],
    )
    def define_work_item(
        work_id: str, payload: schemas.WorkItemDefinition
    ) -> models.WorkItem:
        return state.service.define_work_item(work_id, payload)

    @router.delete(
        "/work-items/{work_id}",
        response_model=schemas.WorkItemRead,
        dependencies=write_guard,
        tags=["work"],
    )
    def hide_work_item(work_id: str) -> models.WorkItem:
        return state.service.hide_work_item(work_id)

    @router.patch(
        "/work-items/{work_id}",
        response_model=schemas.WorkItemRead,
        dependencies=write_guard,
        tags=["work"],
    )
    def update_work_item(work_id: str, payload: schemas.WorkItemUpdate) -> models.WorkItem:
        return state.service.update_work_item(work_id, payload)

    @router.get("/branches/{branch_id}", response_model=schemas.BranchDetail, tags=["branches"])
    def branch_detail(branch_id: str) -> dict[str, Any]:
        return state.service.branch_detail(branch_id)

    @router.get("/branches/{branch_id}/workflow", tags=["workflows"])
    def session_workflow(branch_id: str) -> dict[str, Any]:
        return state.session_workflows.view(branch_id)

    @router.get("/branches/{branch_id}/workflow-links", tags=["workflows"])
    def session_workflow_links(branch_id: str) -> dict[str, Any]:
        return state.workflow_links.view(branch_id)

    @router.post(
        "/branches/{branch_id}/workflow/observe", tags=["workflows"], dependencies=write_guard
    )
    def observe_session_workflow(branch_id: str) -> dict[str, Any]:
        raise HTTPException(
            status_code=410,
            detail="Automatic analysis was retired. Use the octo-workflow skill on demand.",
        )

    @router.get("/branches/{branch_id}/workflow/reports", tags=["workflows"])
    async def session_workflow_reports(
        branch_id: str, workflow_id: str, node_id: str
    ) -> dict[str, Any]:
        try:
            reports = await state.session_workflows.reports(branch_id, workflow_id, node_id)
            return {"reports": reports}
        except NotFoundError:
            raise
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/branches/{branch_id}/native-goal",
        response_model=schemas.NativeGoalState,
        tags=["goals"],
    )
    async def native_goal(branch_id: str) -> schemas.NativeGoalState:
        return await state.native_goals.get(branch_id)

    @router.get(
        "/branches/{branch_id}/native-goal/history",
        response_model=schemas.NativeGoalHistoryPage,
        tags=["goals"],
    )
    def native_goal_history(
        branch_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> schemas.NativeGoalHistoryPage:
        return state.native_goal_history.page(branch_id, limit=limit, offset=offset)

    @router.put(
        "/branches/{branch_id}/native-goal",
        response_model=schemas.NativeGoalState,
        dependencies=write_guard,
        tags=["goals"],
    )
    async def set_native_goal(
        branch_id: str,
        payload: schemas.NativeGoalSet,
    ) -> schemas.NativeGoalState:
        goal_state = await state.native_goals.set(branch_id, payload)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "native_goal.updated",
                "state": goal_state.model_dump(mode="json"),
            },
        )
        return goal_state

    @router.post(
        "/branches/{branch_id}/native-goal/control",
        response_model=schemas.NativeGoalState,
        dependencies=write_guard,
        tags=["goals"],
    )
    async def control_native_goal(
        branch_id: str,
        payload: schemas.NativeGoalControl,
    ) -> schemas.NativeGoalState:
        goal_state = await state.native_goals.control(branch_id, payload)
        await state.event_hub.publish(
            branch_id,
            {
                "type": (
                    "native_goal.cleared"
                    if payload.action == "clear"
                    else "native_goal.updated"
                ),
                "state": goal_state.model_dump(mode="json"),
            },
        )
        return goal_state

    def scheduled_check_detail(branch_id: str) -> dict[str, Any]:
        result = state.scheduled_checks.detail(branch_id)
        result["wakeups"] = state.claude_wakeups.detail(branch_id)
        result["worker"] = state.scheduled_check_worker.health()
        result["notifications_enabled"] = (
            state.settings.infoflow_notifications_enabled and state.infoflow.configured
        )
        return result

    @router.get("/branches/{branch_id}/scheduled-check", tags=["scheduled-checks"])
    def get_scheduled_check(branch_id: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return scheduled_check_detail(branch_id)

    @router.post("/branches/{branch_id}/scheduled-check/prompt",
                 dependencies=write_guard, tags=["scheduled-checks"])
    async def draft_scheduled_check_prompt(
        branch_id: str, payload: schemas.ScheduledCheckPromptRequest,
    ) -> dict[str, Any]:
        try:
            return await state.scheduled_check_prompts.generate(branch_id, payload)
        except CheckPromptUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @router.put("/branches/{branch_id}/scheduled-check",
                dependencies=write_guard, tags=["scheduled-checks"])
    async def save_scheduled_check(
        branch_id: str, payload: schemas.ScheduledCheckSave
    ) -> dict[str, Any]:
        await run_db(state.scheduled_checks.save, branch_id, payload)
        await state.scheduled_check_worker.publish(branch_id)
        return await run_db(scheduled_check_detail, branch_id)

    @router.post("/branches/{branch_id}/claude-wakeups/{wakeup_id}/cancel",
                 dependencies=write_guard, tags=["scheduled-checks"])
    async def cancel_claude_wakeup(branch_id: str, wakeup_id: str) -> dict[str, Any]:
        await run_db(state.claude_wakeups.cancel, branch_id, wakeup_id)
        await state.claude_wakeup_worker.publish(branch_id)
        return await run_db(scheduled_check_detail, branch_id)

    @router.post("/branches/{branch_id}/scheduled-check/control",
                 dependencies=write_guard, tags=["scheduled-checks"])
    async def control_scheduled_check(
        branch_id: str, payload: schemas.ScheduledCheckControl
    ) -> dict[str, Any]:
        await run_db(state.scheduled_checks.control, branch_id, payload.action)
        await state.scheduled_check_worker.publish(branch_id)
        return await run_db(scheduled_check_detail, branch_id)

    @router.get(
        "/branches/{branch_id}/goals",
        response_model=list[schemas.GoalRead],
        tags=["goals"],
    )
    def branch_goals(branch_id: str) -> list[models.GoalRun]:
        # Ensure the Branch identity is validated even if it has no Goals.
        state.service.branch_detail(branch_id)
        return state.goals.list_for_branch(branch_id)

    @router.post(
        "/branches/{branch_id}/goals",
        response_model=schemas.GoalRead,
        dependencies=write_guard,
        tags=["goals"],
    )
    async def create_goal(
        branch_id: str, payload: schemas.GoalCreate
    ) -> models.GoalRun:
        goal = await asyncio.to_thread(state.goals.create, branch_id, payload)
        await asyncio.to_thread(
            state.service.reconcile_external_activity, {goal.work_item_id}
        )
        await state.event_hub.publish(
            branch_id,
            {"type": "goal.updated", "goal_id": goal.id},
        )
        return goal

    @router.get(
        "/goals/{goal_id}",
        response_model=schemas.GoalDetail,
        tags=["goals"],
    )
    def goal_detail(goal_id: str) -> dict[str, Any]:
        return state.goals.detail(goal_id)

    @router.post(
        "/goals/{goal_id}/control",
        response_model=schemas.GoalRead,
        dependencies=write_guard,
        tags=["goals"],
    )
    async def control_goal(
        goal_id: str, payload: schemas.GoalControl
    ) -> models.GoalRun:
        goal = await asyncio.to_thread(state.goals.control, goal_id, payload)
        await asyncio.to_thread(
            state.service.reconcile_external_activity, {goal.work_item_id}
        )
        await state.event_hub.publish(
            goal.branch_id,
            {"type": "goal.updated", "goal_id": goal.id},
        )
        return goal

    @router.post(
        "/branches/{branch_id}/hide",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    def hide_branch(branch_id: str) -> models.ExecutionBranch:
        return state.service.set_branch_hidden(branch_id, hidden=True)

    @router.post(
        "/branches/{branch_id}/restore",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    def restore_branch(branch_id: str) -> models.ExecutionBranch:
        return state.service.set_branch_hidden(branch_id, hidden=False)

    @router.get(
        "/branches/{branch_id}/result-file",
        response_class=FileResponse,
        tags=["conversation"],
    )
    def branch_result_file(
        branch_id: str,
        path: str = Query(min_length=1, max_length=4096),
    ) -> FileResponse:
        raw_path = path.strip()
        if raw_path.casefold().startswith("file://"):
            raw_path = raw_path[7:]
        if not raw_path or "\x00" in raw_path:
            raise HTTPException(status_code=404, detail="Result image not found")

        with state.database.session_factory() as session:
            branch = session.get(models.ExecutionBranch, branch_id)
            if branch is None:
                raise NotFoundError("Branch not found")
            requested = Path(raw_path).expanduser()
            candidate = requested if requested.is_absolute() else Path(branch.cwd) / requested
            resolved = candidate.resolve()
            references = {
                path.strip(),
                raw_path,
                resolved.as_posix(),
                f"file://{resolved.as_posix()}",
                quote(raw_path, safe="/"),
            }
            texts = [
                *session.scalars(
                    select(models.CodexTurn.final_answer).where(
                        models.CodexTurn.branch_id == branch_id
                    )
                ),
                *session.scalars(
                    select(models.ConversationMessage.content).where(
                        models.ConversationMessage.branch_id == branch_id
                    )
                ),
            ]
            referenced = any(
                reference and reference in content
                for content in texts
                for reference in references
            )

        if not referenced or not resolved.is_file():
            raise HTTPException(status_code=404, detail="Result image not found")
        if resolved.suffix.casefold() not in RESULT_IMAGE_SUFFIXES:
            raise HTTPException(status_code=415, detail="Result file is not a supported image")
        if resolved.stat().st_size > RESULT_IMAGE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Result image exceeds the preview limit")
        media_type = guess_type(resolved.name)[0]
        if not media_type or not media_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="Result file is not a supported image")
        headers = {
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        }
        if resolved.suffix.casefold() == ".svg":
            headers["Content-Security-Policy"] = SVG_RESULT_CSP
        return FileResponse(
            resolved,
            media_type=media_type,
            filename=resolved.name,
            content_disposition_type="inline",
            headers=headers,
        )

    @router.get(
        "/branches/{branch_id}/conversation-state",
        response_model=schemas.ConversationState,
        tags=["conversation"],
    )
    def branch_conversation_state(branch_id: str) -> dict[str, Any]:
        return state.service.branch_conversation_state(branch_id)

    @router.post(
        "/branches/{branch_id}/fork",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    async def fork_branch(
        branch_id: str, payload: schemas.BranchForkCreate
    ) -> models.ExecutionBranch:
        return await state.service.fork_branch(branch_id, payload)

    @router.post(
        "/branches/{branch_id}/continue",
        response_model=schemas.BranchContinuationRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    async def continue_branch(
        branch_id: str, payload: schemas.BranchContinuationRequest
    ) -> dict[str, Any]:
        state.service.queue_branch_continuation(branch_id, force=payload.force)
        await state.service.dispatch_queued_continuations()
        continuation = state.service.continuation_state(branch_id)
        if continuation is None:
            raise HTTPException(status_code=409, detail="Continuation was not queued")
        return {"branch_id": branch_id, **continuation}

    @router.post(
        "/branches/{branch_id}/repair-fork",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    async def repair_fork(
        branch_id: str, payload: schemas.BranchForkRepair
    ) -> models.ExecutionBranch:
        return await state.service.repair_fork(branch_id, payload)

    @router.post(
        "/branches/{branch_id}/reopen",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    def reopen_branch_path(branch_id: str) -> models.ExecutionBranch:
        return state.service.reopen_branch_path(branch_id)

    @router.post(
        "/branches/{branch_id}/recover-codex-thread",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["conversation"],
    )
    def recover_codex_thread(branch_id: str) -> models.ExecutionBranch:
        return state.service.recover_codex_thread(branch_id)

    @router.post(
        "/branches/{branch_id}/sessions",
        response_model=schemas.SessionRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    async def start_session(branch_id: str, payload: schemas.SessionStart) -> models.SessionAttempt:
        if payload.owner == "web":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Web sessions are disabled; use the managed local terminal command",
            )
        return await state.service.start_session(branch_id, payload)

    @router.post(
        "/branches/{branch_id}/sessions/{session_id}/heartbeat",
        response_model=schemas.SessionRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def heartbeat_session(
        branch_id: str, session_id: str, payload: schemas.SessionHeartbeat
    ) -> models.SessionAttempt:
        return state.service.heartbeat_session(branch_id, session_id, payload)

    @router.post(
        "/branches/{branch_id}/messages",
        response_model=schemas.ConversationMessageRead,
        dependencies=write_guard,
        tags=["conversation"],
    )
    async def send_message(
        branch_id: str, payload: schemas.ConversationSend
    ) -> models.ConversationMessage:
        await asyncio.to_thread(state.service.reconcile_web_turn_priority)
        message = await state.service.queue_web_message(branch_id, payload)
        detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": detail["conversation_state"],
            },
        )
        return message

    @router.post(
        "/branches/{branch_id}/messages/{message_id}/interrupt",
        response_model=schemas.ConversationMessageRead,
        dependencies=write_guard,
        tags=["conversation"],
    )
    async def interrupt_web_turn(
        branch_id: str, message_id: str
    ) -> models.ConversationMessage:
        message = await asyncio.to_thread(
            state.service.interrupt_web_turn, branch_id, message_id
        )
        serialized = schemas.ConversationMessageRead.model_validate(message).model_dump(
            mode="json"
        )
        await state.event_hub.publish(
            branch_id, {"type": "message.updated", "message": serialized}
        )
        detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": detail["conversation_state"],
            },
        )
        return message

    @router.get(
        "/branches/{branch_id}/runtime-state",
        response_model=schemas.RuntimeState,
        tags=["runtime"],
    )
    def runtime_state(branch_id: str) -> dict[str, Any]:
        return state.service.runtime_state(branch_id)

    @router.post(
        "/branches/{branch_id}/continue-with-pi",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["runtime", "branches"],
    )
    async def continue_with_pi(
        branch_id: str, payload: schemas.PiContinuationCreate
    ) -> models.ExecutionBranch:
        child = await state.service.continue_with_pi(branch_id, payload)
        await state.event_hub.publish(
            branch_id,
            {"type": "branch.continued_with_pi", "branch_id": child.id},
        )
        return child

    @router.post(
        "/branches/{branch_id}/continue-with-codex",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["runtime", "branches"],
    )
    async def continue_with_codex(
        branch_id: str, payload: schemas.CodexContinuationCreate
    ) -> models.ExecutionBranch:
        child = await state.service.continue_with_codex(branch_id, payload)
        await state.event_hub.publish(
            branch_id,
            {"type": "branch.continued_with_codex", "branch_id": child.id},
        )
        return child

    @router.get("/branches/{branch_id}/claude-handoff", tags=["runtime", "branches"])
    async def preview_claude_handoff(branch_id: str) -> dict[str, Any]:
        return await run_db(state.service.preview_claude_handoff, branch_id)

    @router.post(
        "/branches/{branch_id}/claude-workspace-handoff", dependencies=write_guard,
        tags=["runtime", "branches"],
    )
    async def claude_workspace_handoff(
        branch_id: str, payload: schemas.ClaudeWorkspaceHandoff
    ) -> dict[str, Any]:
        result = await run_db(
            state.service.prepare_claude_workspace_handoff, branch_id,
            manual_approval=payload.manual_approval, take_over=payload.take_over,
        )
        await state.event_hub.publish(branch_id, {"type": "workspace.snapshot_created"})
        return result

    @router.post(
        "/branches/{branch_id}/continue-with-claude",
        response_model=schemas.BranchRead,
        dependencies=write_guard,
        tags=["runtime", "branches"],
    )
    async def continue_with_claude(
        branch_id: str, payload: schemas.ClaudeContinuationCreate
    ) -> models.ExecutionBranch:
        child = await state.service.continue_with_claude(branch_id, payload)
        await state.event_hub.publish(
            branch_id,
            {"type": "branch.continued_with_claude", "branch_id": child.id},
        )
        return child

    @router.post(
        "/branches/{branch_id}/runtime/steer",
        response_model=schemas.RuntimeControlResponse,
        dependencies=write_guard,
        tags=["runtime"],
    )
    async def steer_runtime(
        branch_id: str, payload: schemas.ConversationSend
    ) -> dict[str, Any]:
        result = await state.service.runtime_control(
            branch_id, "steer", content=payload.content
        )
        detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": detail["conversation_state"],
            },
        )
        return result

    @router.post(
        "/branches/{branch_id}/runtime/follow-up",
        response_model=schemas.RuntimeControlResponse,
        dependencies=write_guard,
        tags=["runtime"],
    )
    async def follow_up_runtime(
        branch_id: str, payload: schemas.ConversationSend
    ) -> dict[str, Any]:
        result = await state.service.runtime_control(
            branch_id, "follow_up", content=payload.content
        )
        detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": detail["conversation_state"],
            },
        )
        return result

    @router.post(
        "/branches/{branch_id}/runtime/abort",
        response_model=schemas.RuntimeControlResponse,
        dependencies=write_guard,
        tags=["runtime"],
    )
    async def abort_runtime(branch_id: str) -> dict[str, Any]:
        result = await state.service.runtime_control(branch_id, "abort")
        detail = await asyncio.to_thread(state.service.branch_detail, branch_id)
        await state.event_hub.publish(
            branch_id,
            {
                "type": "conversation.state",
                "conversation_state": detail["conversation_state"],
            },
        )
        return result

    @router.post(
        "/branches/{branch_id}/handoff",
        response_model=schemas.SessionRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def handoff(branch_id: str, payload: schemas.HandoffCreate) -> models.SessionAttempt:
        return state.service.submit_handoff(branch_id, payload)

    @router.post(
        "/branches/{branch_id}/sessions/{session_id}/handoff",
        response_model=schemas.SessionRead,
        dependencies=write_guard,
        tags=["sessions"],
    )
    def session_handoff(
        branch_id: str, session_id: str, payload: schemas.SessionHandoffCreate
    ) -> models.SessionAttempt:
        handoff = schemas.HandoffCreate.model_validate(
            payload.model_dump(exclude={"lease_id"})
        )
        return state.service.submit_handoff(
            branch_id,
            handoff,
            session_id=session_id,
            lease_id=payload.lease_id,
        )

    @router.post(
        "/branches/{branch_id}/sessions/{session_id}/skill-proposals",
        response_model=schemas.SkillProposalRead,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=write_guard,
        tags=["skills"],
    )
    def create_skill_proposal(
        branch_id: str,
        session_id: str,
        payload: schemas.SkillProposalCreate,
        background_tasks: BackgroundTasks,
    ) -> models.SkillProposal:
        if not payload.requested:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Skill extraction was not requested",
            )
        proposal = state.skill_proposals.create(branch_id, session_id)
        if proposal.status in {"pending", "failed"}:
            background_tasks.add_task(state.skill_proposals.extract, proposal.id)
        return proposal

    @router.get(
        "/skills",
        response_model=list[schemas.InstalledSkillRead],
        tags=["skills"],
    )
    def installed_skills() -> list[dict[str, Any]]:
        return state.skill_proposals.list_installed()

    @router.get(
        "/skill-proposals",
        response_model=list[schemas.SkillProposalRead],
        tags=["skills"],
    )
    def skill_proposals(
        proposal_status: str | None = Query(default=None, alias="status"),
    ) -> list[models.SkillProposal]:
        return state.skill_proposals.list(proposal_status)

    @router.get(
        "/skill-proposals/{proposal_id}",
        response_model=schemas.SkillProposalRead,
        tags=["skills"],
    )
    def skill_proposal(proposal_id: str) -> models.SkillProposal:
        return state.skill_proposals.get(proposal_id)

    @router.post(
        "/skill-proposals/{proposal_id}/retry",
        response_model=schemas.SkillProposalRead,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=write_guard,
        tags=["skills"],
    )
    def retry_skill_proposal(
        proposal_id: str, background_tasks: BackgroundTasks
    ) -> models.SkillProposal:
        proposal = state.skill_proposals.retry(proposal_id)
        background_tasks.add_task(state.skill_proposals.extract, proposal.id)
        return proposal

    @router.post(
        "/skill-proposals/{proposal_id}/accept",
        response_model=schemas.SkillProposalRead,
        dependencies=write_guard,
        tags=["skills"],
    )
    def accept_skill_proposal(
        proposal_id: str, payload: schemas.SkillProposalReview
    ) -> models.SkillProposal:
        return state.skill_proposals.accept(proposal_id, payload.reviewer_note)

    @router.post(
        "/skill-proposals/{proposal_id}/reject",
        response_model=schemas.SkillProposalRead,
        dependencies=write_guard,
        tags=["skills"],
    )
    def reject_skill_proposal(
        proposal_id: str, payload: schemas.SkillProposalReview
    ) -> models.SkillProposal:
        return state.skill_proposals.reject(proposal_id, payload.reviewer_note)

    @router.post(
        "/branches/{branch_id}/result",
        response_model=schemas.BranchResultRead,
        dependencies=write_guard,
        tags=["branches"],
    )
    def branch_result(branch_id: str, payload: schemas.BranchResultCreate) -> models.BranchResult:
        result, _review = state.service.create_branch_result(branch_id, payload)
        return result

    @router.get(
        "/branches/{branch_id}/terminal-command", response_model=dict[str, str], tags=["sessions"]
    )
    def terminal_command(branch_id: str) -> dict[str, str]:
        branch = state.service.branch_detail(branch_id)["branch"]
        thread_id = branch.codex_thread_id
        managed_command = state.service.terminal_command(branch_id)
        return {
            "command": managed_command,
            "codex_command": (
                state.service.raw_terminal_command(branch_id)
                if branch.runtime == "claude"
                else managed_command
            ),
            "thread_id": (
                thread_id if thread_id and not thread_id.startswith("deferred:") else ""
            ),
        }

    @router.get("/repositories", response_model=list[schemas.RepositoryRead], tags=["repositories"])
    def repositories() -> list[models.Repository]:
        return state.service.list_repositories()

    @router.post(
        "/repositories",
        response_model=schemas.RepositoryRead,
        dependencies=write_guard,
        tags=["repositories"],
    )
    def create_repository(payload: schemas.RepositoryCreate) -> models.Repository:
        return state.service.create_repository(payload)

    @router.get("/memory", response_model=list[schemas.MemoryRead], tags=["memory"])
    def memory(
        program: str | None = Query(default=None),
        memory_status: str | None = Query(default=None, alias="status"),
    ) -> list[models.MemoryEntry]:
        return state.service.list_memory(program_code=program, status=memory_status)

    @router.get(
        "/daily-reflections",
        response_model=list[schemas.DailyReflectionRead],
        tags=["memory"],
    )
    def daily_reflections(
        limit: int = Query(default=31, ge=1, le=366),
    ) -> list[models.DailyReflection]:
        return state.daily_reflections.list(limit)

    @router.post(
        "/daily-reflections/run",
        response_model=schemas.DailyReflectionRead,
        dependencies=write_guard,
        tags=["memory"],
    )
    async def run_daily_reflection(
        payload: schemas.DailyReflectionRun,
        background_tasks: BackgroundTasks,
    ) -> models.DailyReflection:
        try:
            reflection = state.daily_reflections.queue(
                payload.reflection_date,
                force=payload.force,
            )
        except ValueError as exc:
            raise ConflictError(str(exc)) from exc
        if reflection.status != "completed" or payload.force:
            background_tasks.add_task(state.daily_reflections.run, reflection.id)
        return reflection

    @router.post(
        "/memory", response_model=schemas.MemoryRead, dependencies=write_guard, tags=["memory"]
    )
    def create_memory(payload: schemas.MemoryCreate) -> models.MemoryEntry:
        return state.service.create_memory(payload)

    @router.post(
        "/memory/{memory_id}/review",
        response_model=schemas.MemoryRead,
        dependencies=write_guard,
        tags=["memory"],
    )
    def review_memory(memory_id: str, payload: schemas.MemoryReview) -> models.MemoryEntry:
        return state.service.review_memory(memory_id, payload)

    @router.get("/merge-queue", response_model=list[schemas.MergeReviewRead], tags=["merges"])
    def merge_queue() -> list[models.MergeReview]:
        return state.service.list_merge_queue()

    @router.post(
        "/merge-queue/{merge_id}/resolve",
        response_model=schemas.MergeReviewRead,
        dependencies=write_guard,
        tags=["merges"],
    )
    def resolve_merge(merge_id: str, payload: schemas.MergeResolve) -> models.MergeReview:
        return state.service.resolve_merge(merge_id, payload)

    @router.get(
        "/eval-runs",
        response_model=list[schemas.EvalRunOverview],
        tags=["eval-harness"],
    )
    def eval_runs(
        work_item_id: str | None = Query(default=None),
        run_status: str | None = Query(default=None, alias="status"),
    ) -> list[dict[str, Any]]:
        return state.eval_harness.list_runs(work_item_id=work_item_id, status=run_status)

    @router.post(
        "/eval-runs",
        response_model=schemas.EvalRunRead,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def create_eval_run(payload: schemas.EvalRunCreate) -> models.EvalRun:
        return state.eval_harness.create_run(payload)

    @router.get(
        "/eval-runs/{run_id}",
        response_model=schemas.EvalRunOverview,
        tags=["eval-harness"],
    )
    def eval_run(run_id: str) -> dict[str, Any]:
        return state.eval_harness.get_run(run_id)

    @router.get(
        "/eval-runs/{run_id}/units",
        response_model=list[schemas.EvalUnitRead],
        tags=["eval-harness"],
    )
    def eval_units(
        run_id: str,
        unit_status: str | None = Query(default=None, alias="status"),
        error_category: str | None = Query(default=None),
        case_key: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[models.EvalUnit]:
        return state.eval_harness.list_units(
            run_id,
            status=unit_status,
            error_category=error_category,
            case_key=case_key,
            limit=limit,
            offset=offset,
        )

    @router.post(
        "/eval-runs/{run_id}/import-presentbench",
        response_model=schemas.EvalImportResult,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def import_presentbench(
        run_id: str, payload: schemas.EvalPresentBenchImport
    ) -> dict[str, Any]:
        return state.eval_harness.import_presentbench(run_id, payload)

    @router.post(
        "/eval-runs/{run_id}/units/import",
        response_model=schemas.EvalImportResult,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def import_eval_units(
        run_id: str, payload: schemas.EvalUnitBatchImport
    ) -> dict[str, Any]:
        return state.eval_harness.import_units(run_id, payload)

    @router.post(
        "/eval-runs/{run_id}/start",
        response_model=schemas.EvalRunOverview,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def start_eval_run(run_id: str) -> dict[str, Any]:
        return state.eval_harness.start_run(run_id)

    @router.post(
        "/eval-runs/{run_id}/pause",
        response_model=schemas.EvalRunOverview,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def pause_eval_run(run_id: str) -> dict[str, Any]:
        return state.eval_harness.pause_run(run_id)

    @router.post(
        "/eval-runs/{run_id}/claim",
        response_model=schemas.EvalClaimRead,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def claim_eval_units(
        run_id: str, payload: schemas.EvalClaimRequest
    ) -> dict[str, Any]:
        return state.eval_harness.claim(run_id, payload)

    @router.post(
        "/eval-runs/{run_id}/requeue",
        response_model=schemas.EvalRequeueResult,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def requeue_eval_units(run_id: str, payload: schemas.EvalRequeue) -> dict[str, Any]:
        return state.eval_harness.requeue(run_id, payload)

    @router.get(
        "/eval-runs/{run_id}/audit",
        response_model=schemas.EvalAudit,
        tags=["eval-harness"],
    )
    def audit_eval_run(run_id: str) -> dict[str, Any]:
        return state.eval_harness.audit(run_id)

    @router.post(
        "/eval-units/{unit_id}/complete",
        response_model=schemas.EvalUnitRead,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def complete_eval_unit(
        unit_id: str, payload: schemas.EvalUnitComplete
    ) -> models.EvalUnit:
        return state.eval_harness.complete_unit(unit_id, payload)

    @router.post(
        "/eval-units/{unit_id}/fail",
        response_model=schemas.EvalUnitRead,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def fail_eval_unit(
        unit_id: str, payload: schemas.EvalUnitFailure
    ) -> models.EvalUnit:
        return state.eval_harness.fail_unit(unit_id, payload)

    @router.post(
        "/eval-units/{unit_id}/heartbeat",
        response_model=schemas.EvalUnitRead,
        dependencies=write_guard,
        tags=["eval-harness"],
    )
    def heartbeat_eval_unit(
        unit_id: str, payload: schemas.EvalUnitHeartbeat
    ) -> models.EvalUnit:
        return state.eval_harness.heartbeat_unit(unit_id, payload)

    @router.post(
        "/external-jobs",
        response_model=schemas.ExternalJobRead,
        dependencies=write_guard,
        tags=["jobs"],
    )
    def create_external_job(payload: schemas.ExternalJobCreate) -> models.ExternalJob:
        return state.service.create_external_job(payload)

    @router.patch(
        "/external-jobs/{job_id}",
        response_model=schemas.ExternalJobRead,
        dependencies=write_guard,
        tags=["jobs"],
    )
    def update_external_job(job_id: str, payload: schemas.ExternalJobUpdate) -> models.ExternalJob:
        return state.service.update_external_job(job_id, payload)

    @router.post(
        "/agentos/batches",
        response_model=schemas.AgentOSBatchRead,
        dependencies=write_guard,
        tags=["agentos"],
    )
    async def submit_agentos_batch(
        payload: schemas.AgentOSBatchSubmit,
    ) -> dict[str, Any]:
        if not state.settings.agentos_enabled:
            raise HTTPException(status_code=503, detail="AgentOS integration is disabled")
        try:
            execution, job = await asyncio.to_thread(
                state.agentos_jobs.submit_batch,
                work_item_id=payload.work_item_id,
                payload=payload.payload,
                idempotency_key=payload.idempotency_key,
                execution_type=payload.execution_type,
            )
        except AgentOSSubmissionConflict as exc:
            raise ConflictError(str(exc)) from exc
        except AgentOSConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AgentOSHTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"AgentOS submit failed with HTTP {exc.status_code}",
            ) from exc
        except AgentOSClientError as exc:
            raise HTTPException(status_code=502, detail="AgentOS submit failed") from exc
        return {"execution": execution, "external_job": job}

    @router.get(
        "/agentos/batches/{batch_task_id}",
        response_model=schemas.AgentOSBatchStatusRead,
        tags=["agentos"],
    )
    def agentos_batch_status(batch_task_id: str) -> dict[str, Any]:
        _execution, job = state.agentos_jobs.get_local_batch(batch_task_id)
        if job is None:
            raise NotFoundError("AgentOS batch not found")
        counts = job.metadata_json.get("status_counts") or {}
        return {
            "batch_task_id": job.remote_job_id,
            "status": job.status,
            "status_counts": {
                str(key): int(value)
                for key, value in counts.items()
                if isinstance(value, (int, float))
            },
            "total_size": job.metadata_json.get("total_size"),
            "external_job": job,
        }

    @router.post(
        "/agentos/sync",
        response_model=dict[str, int],
        dependencies=write_guard,
        tags=["agentos"],
    )
    async def sync_agentos_jobs() -> dict[str, int]:
        if not state.settings.agentos_enabled:
            raise HTTPException(status_code=503, detail="AgentOS integration is disabled")
        updated = await asyncio.to_thread(state.agentos_jobs.sync_all)
        return {"updated": updated}

    @router.get(
        "/agentos/trajectories/{app_id}",
        response_model=Any,
        dependencies=write_guard,
        tags=["agentos"],
    )
    async def agentos_trajectory(
        app_id: str,
        environment: str = Query(default="evaluation"),
    ) -> Any:
        if not state.settings.agentos_enabled:
            raise HTTPException(status_code=503, detail="AgentOS integration is disabled")
        try:
            return await asyncio.to_thread(
                state.agentos_jobs.trajectory,
                app_id,
                environment=environment,
            )
        except AgentOSConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AgentOSHTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"AgentOS trajectory failed with HTTP {exc.status_code}",
            ) from exc
        except AgentOSClientError as exc:
            raise HTTPException(status_code=502, detail="AgentOS trajectory fetch failed") from exc

    @router.post(
        "/approvals/{approval_id}/resolve",
        response_model=schemas.PendingApprovalRead,
        dependencies=write_guard,
        tags=["approvals"],
    )
    async def resolve_approval(
        approval_id: str, payload: schemas.ApprovalResolve
    ) -> models.PendingApproval:
        try:
            return await state.codex_events.resolve_approval(approval_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.websocket("/ws/branches/{branch_id}")
    async def branch_events(websocket: WebSocket, branch_id: str) -> None:
        token = state.settings.access_token
        if token and websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await state.event_hub.connect(branch_id, websocket)
        try:
            # A browser can connect just after the result-sync worker commits a
            # turn, which means it never receives the one-shot ``turn.synced``
            # event. Send a small durable tail on connect so Results converges
            # without waiting for the next full Branch poll.
            recent_turns = await asyncio.to_thread(_recent_branch_turns, state, branch_id)
            for turn in recent_turns:
                await websocket.send_json(
                    {
                        "type": "turn.synced",
                        "turn": schemas.CodexTurnRead.model_validate(turn).model_dump(
                            mode="json"
                        ),
                        "source": "branch_snapshot",
                    }
                )
            snapshot = state.codex_live.snapshot(branch_id)
            if snapshot:
                await websocket.send_json({"type": "local.live.snapshot", "events": snapshot})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await state.event_hub.disconnect(branch_id, websocket)

    return router


def _recent_branch_turns(state: AppState, branch_id: str, limit: int = 5) -> list[models.CodexTurn]:
    """Load a bounded, visible result tail for a WebSocket reconnect."""
    with state.database.session_factory() as session:
        turns = list(
            session.scalars(
                select(models.CodexTurn)
                .where(models.CodexTurn.branch_id == branch_id)
                .order_by(models.CodexTurn.completed_at.desc())
                .limit(limit)
            )
        )
    return [turn for turn in reversed(turns) if not (turn.metadata_json or {}).get("hidden")]


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    # The HTML shell contains the content-hashed bundle name.  Keep the shell
    # revalidated so a service restart cannot leave an old composer bundle in a
    # long-lived browser tab while the API has already moved to a new state
    # protocol.
    frontend_headers = {"Cache-Control": "no-store, max-age=0"}

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (dist / path).resolve()
        if dist.exists() and candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate, headers=frontend_headers)
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index, headers=frontend_headers)
        raise HTTPException(status_code=404, detail="Frontend has not been built")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found(_request, exc: NotFoundError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(_request, exc: ConflictError):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CodexUnavailable)
    async def codex_unavailable(_request, exc: CodexUnavailable):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=503, content={"detail": str(exc)})
