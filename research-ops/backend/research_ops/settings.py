from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ROPS_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8020
    data_dir: Path = PROJECT_ROOT / "data"
    # High-frequency runtime state can live on local disk while durable
    # configuration and artifacts remain under ``data_dir`` on shared storage.
    runtime_dir: Path | None = None
    workspace_root: Path = PROJECT_ROOT / "data" / "research-workspaces"
    database_url: str | None = None
    programs_file: Path = PROJECT_ROOT / "config" / "programs.yaml"
    frontend_dist: Path = PROJECT_ROOT / "web" / "dist"
    access_token: str | None = Field(default=None, repr=False, exclude=True)
    require_access_token: bool = False
    load_project_env: bool = True
    web_terminal_enabled: bool = False
    web_terminal_cwd: Path | None = None
    web_terminal_shell: str = "/bin/bash"
    web_terminal_tmux: str = "tmux"

    codex_enabled: bool = True
    codex_home: Path = Path.home() / ".codex"
    codex_command: str = "codex"
    codex_backend: str = "cli"
    # Keep the managed runtime explicit instead of inheriting a possibly stale
    # model from the operator's global Codex config.
    codex_model: str | None = "gpt-6-astra"
    codex_reasoning_effort: str = "max"
    codex_model_provider: str | None = None
    oneapi_base_url: str = "https://models.example.com/v1"
    oneapi_api_key: str | None = Field(default=None, repr=False, exclude=True)
    oneapi_model: str = "DeepSeek-V4.1-Flash"
    oneapi_reasoning_effort: str = "high"
    # Personal-account egress is isolated from Company API and server I/O.
    codex_personal_http_proxy: str | None = None
    codex_personal_https_proxy: str | None = None
    codex_personal_no_proxy: str | None = None
    # Deprecated: native Codex configuration owns MCP availability.
    codex_disabled_mcp_servers: list[str] = Field(default_factory=list)
    codex_approval_policy: str | None = None
    codex_sandbox: str | None = None
    # Orbitree does not impose a Web concurrency limit by default. A
    # positive value enables an optional deployment-level semaphore; ``0``
    # means unlimited and lets the provider own admission/back-pressure.
    # Same-Branch turns remain serialized by the Branch lock, while local CLI
    # sessions are independent of this setting.
    codex_max_concurrent_turns: int = Field(default=0, ge=0, le=64)
    codex_capacity_retry_attempts: int = Field(default=3, ge=0, le=8)
    codex_capacity_retry_initial_seconds: float = Field(default=30.0, ge=1.0)
    codex_capacity_retry_max_seconds: float = Field(default=300.0, ge=1.0)
    # A gateway disconnect is different from a completed model error. Retry it
    # only when Orbitree has not observed an item from that turn; otherwise
    # a replay could repeat a tool call or an external submission.
    codex_transport_retry_attempts: int = Field(default=1, ge=0, le=3)
    codex_transport_retry_initial_seconds: float = Field(default=5.0, ge=1.0)
    codex_transport_retry_max_seconds: float = Field(default=30.0, ge=1.0)
    codex_request_timeout_seconds: float = 30.0
    codex_fork_timeout_seconds: float = 120.0
    codex_event_line_limit_bytes: int = 8 * 1024 * 1024
    # Deprecated compatibility setting. Orbitree never rolls threads over
    # automatically; context management belongs to the native runtime.
    codex_thread_rollover_bytes: int = Field(
        default=0, ge=0, le=64 * 1024 * 1024
    )
    codex_result_sync_enabled: bool = True
    codex_result_sync_interval_seconds: float = 3.0
    # Prompt backfill is a recovery path for old imported turns. Normal turns
    # receive their prompt during incremental sync, so do not repeatedly scan
    # large historical JSONL files on the hot path.
    codex_prompt_backfill_interval_seconds: float = Field(default=600.0, ge=30.0)
    # Foreground forks are durable queue entries.  Keep dispatch independent
    # from the request that created the fork so a CLI process can exit safely.
    codex_continuation_dispatch_interval_seconds: float = 2.0
    codex_local_live_enabled: bool = True
    codex_local_live_interval_seconds: float = 1.5
    codex_local_live_replay_bytes: int = 8 * 1024 * 1024
    codex_local_live_max_events: int = 200
    codex_local_live_max_content_chars: int = 8_000
    codex_registry_enabled: bool = True
    codex_registry_scan_interval_seconds: float = 30.0
    codex_registry_max_sessions: int = 250
    codex_registry_lookback_days: int = 30
    codex_title_generation_enabled: bool = False
    codex_title_generation_batch_size: int = 8
    codex_title_generation_timeout_seconds: float = 180.0

    # Pi is an optional second agent runtime.  It deliberately reuses the
    # existing Work/Branch/session control plane until the generic runtime
    # schema is introduced.  The proxy is scoped to Pi subprocesses only.
    pi_enabled: bool = False
    pi_command: str = "pi"
    pi_session_dir: Path = Path.home() / ".pi" / "agent" / "sessions"
    pi_provider: str = "openai-codex"
    pi_model: str = "gpt-6-astra"
    pi_thinking: str = "max"
    pi_http_proxy: str | None = None
    pi_https_proxy: str | None = None
    pi_no_proxy: str | None = None
    pi_extensions_enabled: bool = False
    pi_tools: str | None = None
    pi_start_timeout_seconds: float = 20.0
    pi_request_timeout_seconds: float = 30.0
    pi_turn_timeout_seconds: float = 86_400.0
    pi_event_line_limit_bytes: int = 8 * 1024 * 1024

    # Claude Code/Baidu CC is a third, independently authenticated runtime.
    # Orbitree invokes the installed CLI and never reads or copies its
    # account credentials.
    claude_enabled: bool = True
    claude_command: str = "claude"
    claude_account_label: str = "CLI account"
    claude_home: Path = Path.home() / ".claude"
    claude_model: str = "auto"
    claude_effort: str = "high"
    claude_permission_mode: str = "bypassPermissions"
    claude_web_approvals_enabled: bool = True
    claude_turn_timeout_seconds: float = 86_400.0
    claude_event_line_limit_bytes: int = 8 * 1024 * 1024

    skill_extraction_enabled: bool = False
    skill_source_root: Path = PROJECT_ROOT / "data" / "skills"
    skill_install_root: Path = Path.home() / ".codex" / "skills"
    skill_extraction_timeout_seconds: float = 300.0
    skill_extraction_max_turns: int = 12

    lease_minutes: int = 45
    web_turn_heartbeat_seconds: float = 15.0
    dag_job_sync_enabled: bool = True
    dag_job_sync_interval_seconds: float = 5.0
    dag_state_max_bytes: int = 5 * 1024 * 1024

    # Read-side session workflows never participate in conversation admission,
    # prompts, Goals, or job execution. Analysis has its own background worker.
    session_workflows_enabled: bool = False
    session_workflow_poll_seconds: float = Field(default=30.0, ge=5.0)
    session_workflow_analysis_enabled: bool = False
    session_workflow_analysis_model: str | None = None
    session_workflow_analysis_effort: str = "low"
    session_workflow_analysis_timeout_seconds: float = Field(default=180.0, ge=10.0)
    session_workflow_analysis_cooldown_seconds: float = Field(default=300.0, ge=10.0)
    session_workflow_evidence_chars: int = Field(default=48_000, ge=4_000, le=200_000)
    octo_base_url: str = "http://127.0.0.1:8300"
    octo_api_key: str | None = Field(default=None, repr=False)
    octo_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    octo_poll_seconds: float = Field(default=60.0, ge=10.0)
    octo_max_workflows: int = Field(default=2_000, ge=20, le=10_000)

    # The cluster dashboard is read-only. LTP Control remains the owner of
    # all-user discovery, Tidal formula validation, and scheduler estimates.
    cluster_dashboard_enabled: bool = True
    cluster_dashboard_cache_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    cluster_dashboard_stale_seconds: float = Field(default=180.0, ge=30.0, le=3600.0)
    ltp_control_command: str = "ltp-control"
    ltp_control_python_command: Path | None = None
    ltp_control_source_root: Path | None = None
    ltp_control_snapshot_path: Path | None = None
    ltp_control_profiles_file: Path | None = None
    ltp_control_directory_user: str = "bob"
    ltp_control_allow_partial_users: bool = True
    ltp_control_state_dir: Path | None = None
    ltp_control_query_timeout_seconds: int = Field(default=15, ge=1, le=120)
    ltp_control_process_timeout_seconds: float = Field(default=90.0, ge=10.0, le=600.0)
    ltp_control_max_response_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        le=256 * 1024 * 1024,
    )

    # Managed jobs expose one logical submission API while keeping physical
    # cluster placement and provider identities inside the control plane.
    managed_jobs_enabled: bool = True
    managed_job_submit_enabled: bool = False
    managed_job_dispatch_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    managed_job_verification_timeout_seconds: float = Field(
        default=300.0, ge=30.0, le=3600.0
    )
    managed_job_allowed_roots: list[Path] = Field(
        default_factory=lambda: [PROJECT_ROOT / "data" / "research-workspaces"]
    )
    managed_job_ltp_cli: str = "ltp-cli"
    managed_job_submit_timeout_seconds: float = Field(default=90.0, ge=10.0, le=600.0)
    managed_job_bundle_check_script: Path = (
        Path.home() / ".codex/skills/chaoxi-infra-shared/scripts/check_ltp_bundle.py"
    )
    managed_job_training_preflight_script: Path = (
        Path.home() / ".codex/skills/chaoxi-infra-shared/scripts/validate_training_preflight.py"
    )

    # LTP Monitor is intentionally separate from managed jobs, Work Items,
    # and Codex sessions. It observes a configured user's remote jobs and
    # records every state transition in its own durable ledger.
    ltp_monitor_enabled: bool = False
    ltp_monitor_users: list[str] = Field(default_factory=lambda: ["alice"])
    ltp_monitor_poll_interval_seconds: float = Field(default=30.0, ge=5.0, le=600.0)
    ltp_monitor_max_jobs: int = Field(default=100, ge=1, le=2_000)
    ltp_monitor_cli: str = "ltp-cli"
    ltp_monitor_cli_api_url: str | None = None
    ltp_monitor_status_probe_enabled: bool = True
    ltp_monitor_status_probe_timeout_seconds: float = Field(default=20.0, ge=3.0, le=120.0)
    ltp_monitor_max_status_probes_per_sync: int = Field(default=24, ge=1, le=200)
    ltp_monitor_status_probe_concurrency: int = Field(default=6, ge=1, le=16)
    ltp_monitor_notifications_enabled: bool = False
    ltp_monitor_group_id: str | None = None
    ltp_monitor_notify_states: list[str] = Field(
        default_factory=lambda: ["created", "pending", "running", "failed"]
    )

    # Goals are durable, user-approved background objectives.  Their worker
    # uses database leases, so a server restart does not lose a pending goal
    # or create duplicate concurrent ticks.
    goals_enabled: bool = True
    scheduled_checks_enabled: bool = True
    scheduled_check_tick_seconds: float = Field(default=15.0, ge=1.0, le=300.0)
    goal_dispatch_interval_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    goal_lease_seconds: int = Field(default=900, ge=30, le=86_400)
    goal_lease_heartbeat_seconds: float = Field(default=60.0, ge=5.0, le=3_600.0)
    goal_default_cadence_seconds: int = Field(default=300, ge=30, le=86_400)
    goal_max_dispatch_per_tick: int = Field(default=4, ge=1, le=32)
    native_goal_supervision_enabled: bool = True
    native_goal_check_interval_seconds: float = Field(default=30.0, ge=5.0, le=300.0)
    native_goal_progress_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    native_goal_max_recovery_attempts: int = Field(default=3, ge=0, le=10)
    native_goal_retry_delay_seconds: int = Field(default=30, ge=1, le=3_600)
    goal_allowed_roots: list[Path] = Field(
        default_factory=lambda: [PROJECT_ROOT / "data"]
    )

    # Turn-completion notifications are opt-in. App credentials stay in the
    # referenced environment variables and are never persisted by Orbitree.
    infoflow_notifications_enabled: bool = False
    # Failure alerts share the durable Infoflow outbox with completed-turn
    # reminders. This is meaningful only when the parent notification switch
    # is enabled, and may be disabled separately for deliberately quiet runs.
    infoflow_error_notifications_enabled: bool = True
    infoflow_recipient: str = "alice"
    infoflow_base_url: str = "https://messages.example.com"
    infoflow_app_key_env: str = "INFOFLOW_APP_KEY"
    infoflow_app_secret_env: str = "INFOFLOW_APP_SECRET"
    # Optional local secret file used when the referenced process environment
    # variables are absent. The file must not be group/world accessible.
    infoflow_credentials_file: Path | None = None
    infoflow_public_base_url: str = "http://127.0.0.1:8020"
    infoflow_timeout_seconds: float = 15.0
    infoflow_verify_tls: bool = True
    infoflow_trust_env: bool = True
    infoflow_dispatch_interval_seconds: float = 5.0
    infoflow_dispatch_batch_size: int = Field(default=10, ge=1, le=50)
    infoflow_summary_chars: int = Field(default=480, ge=80, le=2_000)

    # A separate ephemeral Codex agent reflects on the completed Shanghai
    # calendar day. The feature is opt-in per deployment and catches up after
    # downtime without creating duplicate reports.
    daily_reflection_enabled: bool = False
    daily_reflection_timezone: str = "Asia/Shanghai"
    daily_reflection_hour: int = Field(default=0, ge=0, le=23)
    daily_reflection_minute: int = Field(default=0, ge=0, le=59)
    daily_reflection_poll_interval_seconds: float = Field(default=30.0, ge=5.0)
    daily_reflection_timeout_seconds: float = Field(default=900.0, ge=30.0)
    daily_reflection_max_records: int = Field(default=240, ge=20, le=1_000)
    daily_reflection_max_prompt_chars: int = Field(
        default=140_000, ge=10_000, le=500_000
    )
    daily_reflection_item_chars: int = Field(default=2_400, ge=200, le=10_000)
    daily_reflection_infoflow_enabled: bool = True

    # AgentOS/Datawheel is intentionally opt-in.  The bearer token is never
    # stored here; ``agentos_token_env`` names the environment variable from
    # which the HTTP adapter resolves it at request time.
    agentos_enabled: bool = False
    agentos_datawheel_base_url: str | None = None
    agentos_trajectory_base_url: str | None = None
    agentos_token_env: str = "DATAWHEEL_TOKEN"
    agentos_timeout_seconds: float = 60.0
    agentos_verify_tls: bool = True
    agentos_trust_env: bool = False
    agentos_job_sync_enabled: bool = True
    agentos_job_sync_interval_seconds: float = 10.0
    agentos_job_sync_max_jobs: int = 100

    # ORS is opt-in as well.  Mock mode is credential-free; contract mode
    # assembles all four real adapters from the versioned config and runtime
    # environment references.
    openreward_enabled: bool = False
    openreward_session_ttl_seconds: float = 900.0
    openreward_api_key_env: str | None = None
    openreward_backend_mode: str = "mock"
    openreward_backend_config: Path = (
        PROJECT_ROOT / "config" / "openreward-backends.example.yaml"
    )
    openreward_task_catalog: Path | None = None
    openreward_real_domains: list[str] = Field(
        default_factory=lambda: ["miaoda", "swe", "ppt", "tooluse"]
    )

    # Goblin Reward is an external evaluator.  Its token is always resolved
    # from the named environment variable at request time.
    goblin_enabled: bool = False
    goblin_base_url: str | None = None
    goblin_token_env: str = "GOBLIN_REWARD_API_TOKEN"
    goblin_timeout_seconds: float = 120.0
    goblin_poll_interval_seconds: float = 10.0
    goblin_poll_timeout_seconds: float = 3_600.0
    goblin_benchmark_dataset_name: str | None = None
    goblin_prompt_version: str = "v1_6"
    goblin_reward_model: str = "gpt-5.5-agent"
    goblin_eval_mm: bool = True
    goblin_eval_miniprog: bool = True

    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"]
    )

    @field_validator(
        "data_dir",
        "workspace_root",
        "programs_file",
        "frontend_dist",
        "codex_home",
        "claude_home",
        "pi_session_dir",
        "skill_source_root",
        "skill_install_root",
        "openreward_backend_config",
        "managed_job_bundle_check_script",
        "managed_job_training_preflight_script",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @field_validator(
        "openreward_task_catalog",
        "ltp_control_python_command",
        "ltp_control_source_root",
        "ltp_control_snapshot_path",
        "ltp_control_profiles_file",
        "ltp_control_state_dir",
        mode="before",
    )
    @classmethod
    def expand_optional_path(cls, value: str | Path | None) -> Path | None:
        if value is None or not str(value).strip():
            return None
        return cls.expand_path(value)

    @field_validator("managed_job_allowed_roots", mode="before")
    @classmethod
    def expand_managed_job_roots(cls, value: object) -> list[Path]:
        if isinstance(value, (str, Path)):
            values = [value]
        elif isinstance(value, list):
            values = value
        else:
            raise ValueError("managed_job_allowed_roots must be a list of paths")
        roots = [Path(item).expanduser().resolve() for item in values if str(item).strip()]
        if not roots:
            raise ValueError("managed_job_allowed_roots must not be empty")
        return roots

    @field_validator("ltp_monitor_users", mode="before")
    @classmethod
    def validate_ltp_monitor_users(cls, value: object) -> list[str]:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            raise ValueError("ltp_monitor_users must be a list of user names")
        normalized = list(
            dict.fromkeys(str(item).strip() for item in values if str(item).strip())
        )
        has_whitespace = any(
            any(character.isspace() for character in item) for item in normalized
        )
        if not normalized or has_whitespace:
            raise ValueError("ltp_monitor_users must contain non-empty user names")
        return normalized

    @field_validator("ltp_monitor_group_id")
    @classmethod
    def validate_ltp_monitor_group_id(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not normalized.isdigit():
            raise ValueError("ltp_monitor_group_id must be a numeric group ID")
        return normalized

    @field_validator("ltp_monitor_notify_states", mode="before")
    @classmethod
    def validate_ltp_monitor_notify_states(cls, value: object) -> list[str]:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            raise ValueError("ltp_monitor_notify_states must be a list")
        allowed = {"created", "pending", "running", "failed", "completed", "cancelled"}
        normalized = list(
            dict.fromkeys(str(item).strip().casefold() for item in values if str(item).strip())
        )
        if any(item not in allowed for item in normalized):
            raise ValueError("ltp_monitor_notify_states contains an unsupported state")
        return normalized

    @field_validator("openreward_backend_mode")
    @classmethod
    def validate_openreward_backend_mode(cls, value: str) -> str:
        normalized = str(value).strip().casefold()
        if normalized not in {"mock", "contract"}:
            raise ValueError("openreward_backend_mode must be mock or contract")
        return normalized

    @field_validator(
        "ltp_control_command",
        "ltp_control_directory_user",
        "managed_job_ltp_cli",
        "ltp_monitor_cli",
    )
    @classmethod
    def validate_ltp_control_identity(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("LTP Control command and directory user must be single values")
        return normalized

    @field_validator("codex_backend")
    @classmethod
    def validate_codex_backend(cls, value: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized not in {"cli", "app-server", "pi"}:
            raise ValueError("codex_backend must be cli, app-server, or pi")
        return normalized

    @field_validator("openreward_api_key_env")
    @classmethod
    def validate_openreward_api_key_env(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not normalized[0].isalpha() or not normalized.isupper() or not all(
            char.isalnum() or char == "_" for char in normalized
        ):
            raise ValueError("openreward_api_key_env must be an environment-variable name")
        return normalized

    @field_validator("infoflow_app_key_env", "infoflow_app_secret_env")
    @classmethod
    def validate_infoflow_credential_env(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if (
            not normalized
            or not normalized[0].isalpha()
            or not normalized.isupper()
            or not all(char.isalnum() or char == "_" for char in normalized)
        ):
            raise ValueError("Infoflow credential references must be environment-variable names")
        return normalized

    @field_validator("daily_reflection_timezone")
    @classmethod
    def validate_daily_reflection_timezone(cls, value: str) -> str:
        normalized = str(value or "").strip()
        try:
            ZoneInfo(normalized)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("daily_reflection_timezone must be an IANA timezone") from exc
        return normalized

    @field_validator("infoflow_recipient")
    @classmethod
    def validate_infoflow_recipient(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or "@" in normalized or any(char.isspace() for char in normalized):
            raise ValueError("infoflow_recipient must be one email prefix")
        return normalized

    @field_validator("openreward_real_domains")
    @classmethod
    def validate_openreward_real_domains(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item).strip().casefold() for item in value))
        if not normalized or any(
            item not in {"miaoda", "swe", "ppt", "tooluse"} for item in normalized
        ):
            raise ValueError("openreward_real_domains contains an unknown domain")
        return normalized

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.runtime_data_dir / 'research-ops.db'}"

    @property
    def runtime_data_dir(self) -> Path:
        return self.runtime_dir or self.data_dir

    @property
    def resolved_ltp_control_state_dir(self) -> Path:
        return self.ltp_control_state_dir or self.runtime_data_dir / "ltp-control-live"

    @property
    def uses_separate_runtime_storage(self) -> bool:
        return self.database_url is None and self.runtime_data_dir != self.data_dir

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifact-index").mkdir(exist_ok=True)
        if self.cluster_dashboard_enabled:
            self.resolved_ltp_control_state_dir.mkdir(parents=True, exist_ok=True)
        if self.pi_enabled or self.codex_backend == "pi":
            self.pi_session_dir.mkdir(parents=True, exist_ok=True)
        if self.skill_extraction_enabled:
            self.skill_source_root.mkdir(parents=True, exist_ok=True)
            self.skill_install_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    team_file = os.environ.get("ROPS_TEAM_SETTINGS")
    if team_file:
        # Dedicated workers must never fall back to the shared deployment .env.
        from dotenv import load_dotenv

        if os.environ.get("ROPS_ENV_FILE"):
            load_dotenv(os.environ["ROPS_ENV_FILE"], override=False)
        values = json.loads(Path(team_file).read_text())
        settings = Settings(_env_file=os.environ.get("ROPS_ENV_FILE"), **values)
    elif "ROPS_ENV_FILE" in os.environ:
        settings = Settings(_env_file=os.environ["ROPS_ENV_FILE"] or None)
    else:
        settings = Settings()
    # Platform model preferences are durable and take precedence over the
    # deployment environment for future agent turns.
    from .services.runtime_settings import apply_persisted_runtime_model_settings

    apply_persisted_runtime_model_settings(settings)
    settings.ensure_directories()
    return settings
