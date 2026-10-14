from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def serialize_utc_datetime(self, value: Any) -> Any:
        """Keep API timestamps unambiguous even when SQLite drops tzinfo."""
        if isinstance(value, datetime):
            normalized = (
                value.replace(tzinfo=UTC)
                if value.tzinfo is None
                else value.astimezone(UTC)
            )
            return normalized.isoformat().replace("+00:00", "Z")
        return value


class ProgramRead(ORMModel):
    id: str
    code: str
    slug: str
    name: str
    objective: str
    color: str
    sort_order: int
    active: bool


class ProgramCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}$")
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(default="", max_length=10_000)
    color: str = Field(default="#32766d", pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: int = Field(default=50, ge=0, le=10000)


class ProgramUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(default="", max_length=10_000)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    sort_order: int = Field(ge=0, le=10000)
    active: bool = True


class RepositoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    canonical_path: str
    remote_url: str | None = None
    default_ref: str = "HEAD"
    program_codes: list[str] = Field(default_factory=list)


class RepositoryRead(ORMModel):
    id: str
    name: str
    canonical_path: str
    remote_url: str | None
    default_ref: str
    program_codes: list[str]
    active: bool


class AcceptanceCriterion(BaseModel):
    description: str
    kind: Literal["manual", "command", "metric"] = "manual"
    expected: str | float | int | bool | None = None
    command: str | None = None


class WorkItemCreate(BaseModel):
    program_code: str
    title: str | None = Field(default=None, max_length=320)
    objective: str | None = None
    definition_mode: Literal["defined", "emerging"] = "defined"
    kind: str = "task"
    primary_parent_id: str | None = None
    repository_id: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    branch_mode: Literal["write", "read", "capture"] = "write"
    # Optional for backwards compatibility: omitted requests inherit the
    # deployment default, while New Work can select a runtime explicitly.
    runtime: Literal["codex", "pi", "claude"] | None = None

    @field_validator("title", "objective", mode="before")
    @classmethod
    def blank_work_definition_is_optional(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def defined_work_requires_a_title(self) -> WorkItemCreate:
        if self.definition_mode == "defined" and not self.title:
            raise ValueError("Title is required unless definition_mode is emerging")
        return self


class WorkItemDefinition(BaseModel):
    title: str = Field(min_length=1, max_length=320)
    objective: str = Field(min_length=1)

    @field_validator("title", "objective", mode="before")
    @classmethod
    def strip_definition(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class WorkItemUpdate(BaseModel):
    title: str | None = None
    objective: str | None = None
    lifecycle: Literal["draft", "ready", "active", "review", "done", "cancelled"] | None = None
    flow: (
        Literal["actionable", "running", "waiting_external", "blocked", "needs_decision"] | None
    ) = None
    priority: int | None = Field(default=None, ge=0, le=100)
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    labels: list[str] | None = None
    expected_revision: int


class WorkItemRead(ORMModel):
    id: str
    human_id: str
    program_id: str
    primary_parent_id: str | None
    repository_id: str | None
    title: str
    objective: str
    kind: str
    lifecycle: str
    flow: str
    priority: int
    acceptance_criteria: list[dict[str, Any]]
    labels: list[str]
    hidden: bool
    due_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class BranchForkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1)
    mode: Literal["write", "read", "capture"] = "write"
    continuation: Literal["continue", "parallel"] = "parallel"
    # Retained for backwards-compatible validation.  The service rejects True:
    # child execution must be a separate, explicit user action.
    start_session: bool = False
    # Fork creation is a user-approved mutation.  Agents must propose a fork
    # and wait for the user to invoke the Web action or explicitly approve CLI.
    manual_approval: bool = False


class PiContinuationCreate(BaseModel):
    """Create an explicit Codex-to-Pi sidecar Branch."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, min_length=1)
    # Pi is an auxiliary runtime. Keep the originating Codex Branch available
    # unless a caller explicitly requests a one-way foreground continuation.
    continuation: Literal["continue", "parallel"] = "parallel"
    manual_approval: bool = False


class CodexContinuationCreate(BaseModel):
    """Create an explicit Pi-to-Codex sidecar Branch."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, min_length=1)
    # Pi is a fallback runtime. Returning to Codex is always parallel by
    # default so the Pi evidence and conversation remain available.
    continuation: Literal["continue", "parallel"] = "parallel"
    manual_approval: bool = False


class ClaudeContinuationCreate(BaseModel):
    """Create an explicit sidecar Branch backed by Claude Code."""

    name: str | None = Field(default=None, min_length=1, max_length=160)
    objective: str | None = Field(default=None, min_length=1)
    continuation: Literal["continue", "parallel"] = "parallel"
    manual_approval: bool = False
    include_context: bool = True
    expected_handoff_hash: str | None = None


class BranchContinuationRead(BaseModel):
    branch_id: str
    status: Literal["queued", "running", "completed", "failed"]
    requested_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    message_id: str | None = None
    turn_id: str | None = None
    error: str | None = None


class BranchContinuationRequest(BaseModel):
    force: bool = False


class BranchForkRepair(BaseModel):
    continuation: Literal["continue", "parallel"] = "parallel"
    manual_approval: bool = False


class ClaudeWorkspaceHandoff(BaseModel):
    manual_approval: bool = False
    take_over: bool = False


class BranchRead(ORMModel):
    id: str
    work_item_id: str
    parent_branch_id: str | None
    name: str
    slug: str
    status: str
    hidden: bool
    mode: str
    cwd: str
    workspace_path: str | None
    git_branch: str | None
    base_sha: str | None
    head_sha: str | None
    codex_thread_id: str | None
    runtime: Literal["codex", "pi", "claude"] = "codex"
    execution_preferences: dict[str, Any] = Field(default_factory=dict)
    continued_by_branch_id: str | None
    context_snapshot: dict[str, Any]
    memory_cursor: int
    created_at: datetime
    updated_at: datetime


class SessionRead(ORMModel):
    id: str
    branch_id: str
    codex_thread_id: str | None
    runtime: Literal["codex", "pi", "claude"] = "codex"
    execution_config: dict[str, Any] = Field(default_factory=dict)
    status: str
    lease_id: str
    lease_owner: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    work_item_revision: int
    brief: dict[str, Any]
    handoff: dict[str, Any]
    last_turn_id: str | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None

    @field_validator("lease_expires_at", "heartbeat_at", "started_at", "completed_at", mode="after")
    @classmethod
    def assume_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class SessionStart(BaseModel):
    owner: str = "web"


class SessionHeartbeat(BaseModel):
    lease_id: str


class CodexWindowRead(BaseModel):
    id: str
    thread_id: str
    parent_thread_id: str | None
    parent_window_id: str | None
    parent_work_id: str | None
    parent_program_code: str | None
    can_attach_to_parent: bool
    child_count: int
    display_name: str
    title_source: Literal["ai", "manual", "work", "fallback"]
    cwd: str
    cli_version: str | None
    originator: str | None
    pid: int | None
    tty: str | None
    live: bool
    state: Literal["unassigned", "managed", "saved", "ignored"]
    match_confidence: str
    ignored: bool
    started_at: datetime | None
    last_activity_at: datetime
    branch_id: str | None
    branch_name: str | None
    work_id: str | None
    work_title: str | None
    program_code: str | None
    session_id: str | None
    session_status: str | None
    resume_command: str

    @field_validator("started_at", "last_activity_at", mode="after")
    @classmethod
    def window_times_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class CodexWindowTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class CodexWindowAttach(BaseModel):
    branch_id: str


class CodexWindowCreateWork(BaseModel):
    program_code: str = "FREE"


class CodexWindowTarget(BaseModel):
    branch_id: str
    branch_name: str
    work_id: str
    work_title: str
    program_code: str
    cwd: str


class CodexWindowAdoption(BaseModel):
    window: CodexWindowRead
    work_item: WorkItemRead
    branch: BranchRead


class ConversationSend(BaseModel):
    content: str = Field(min_length=1)


class ScheduledCheckSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=32000)
    interval_minutes: int = Field(default=10, ge=1, le=10080)
    stop_mode: Literal["manual", "deadline", "job"] = "manual"
    stop_at: datetime | None = None
    job_id: str | None = None
    notification_mode: Literal["important", "all", "none"] = "important"

    @model_validator(mode="after")
    def validate_check(self) -> ScheduledCheckSave:
        if not self.prompt.strip():
            raise ValueError("Check prompt cannot be blank")
        if self.stop_mode == "deadline":
            if self.stop_at is None or self.stop_at.tzinfo is None:
                raise ValueError("Choose a stop time with an explicit timezone")
        elif self.stop_at is not None:
            raise ValueError("Stop time is only valid for a deadline")
        if (self.stop_mode == "job") != bool(self.job_id):
            raise ValueError("Choose a job only when stopping on job completion")
        return self


class ScheduledCheckControl(BaseModel):
    action: Literal["pause", "resume", "stop", "run_now"]


class ScheduledCheckPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_prompt: str | None = Field(default=None, max_length=32000)


class ScheduledCheckRead(ORMModel):
    id: str
    branch_id: str
    status: str
    phase: str
    prompt: str
    interval_minutes: int
    stop_mode: str
    stop_at: datetime | None
    job_id: str | None
    notification_mode: str
    execution: dict[str, Any]
    next_run_at: datetime | None
    active_run_id: str | None
    last_completed_at: datetime | None
    last_outcome: str | None
    last_summary: str | None
    consecutive_errors: int
    reason: str | None


class ScheduledCheckRunRead(ORMModel):
    id: str
    status: str
    prompt: str
    execution: dict[str, Any]
    message_id: str | None
    outcome: str | None
    summary: str | None
    created_at: datetime
    completed_at: datetime | None


class RuntimeControlResponse(BaseModel):
    accepted: bool
    command: Literal["steer", "follow_up", "abort"]
    runtime: Literal["codex", "pi", "claude"]
    message_id: str | None = None


class RuntimeState(BaseModel):
    runtime: Literal["codex", "pi", "claude"]
    state: Literal["idle", "running", "settled", "failed", "aborted"]
    capabilities: list[str] = Field(default_factory=list)
    session_id: str | None = None
    owner: str | None = None
    queue: dict[str, list[str]] = Field(default_factory=dict)


class ConversationMessageRead(ORMModel):
    id: str
    branch_id: str
    session_id: str | None
    turn_id: str | None
    external_item_id: str | None
    role: str
    kind: str
    content: str
    metadata_json: dict[str, Any]
    created_at: datetime


class CodexTurnRead(ORMModel):
    id: str
    branch_id: str
    session_id: str | None
    thread_id: str
    external_turn_id: str
    status: str
    prompt: str | None
    final_answer: str
    started_at: datetime | None
    completed_at: datetime
    duration_ms: int | None
    time_to_first_token_ms: int | None
    metadata_json: dict[str, Any]

    @field_validator("started_at", "completed_at", mode="after")
    @classmethod
    def turn_times_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class HandoffCreate(BaseModel):
    outcome: Literal["completed", "continue", "blocked", "waiting_external"]
    conclusion: str
    changes: list[str] = Field(default_factory=list)
    external_jobs: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    acceptance_results: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str | None = None
    risks: list[str] = Field(default_factory=list)
    source_turn_ids: list[str] = Field(default_factory=list)


class SessionHandoffCreate(HandoffCreate):
    lease_id: str


class MemoryCreate(BaseModel):
    program_code: str
    scope_type: Literal["program", "work_item", "branch"]
    scope_id: str
    kind: Literal["fact", "decision", "hypothesis", "failure", "procedure", "observation"]
    title: str
    content: str
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: Literal["draft", "proposed"] = "draft"
    source_branch_id: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class MemoryRead(ORMModel):
    id: str
    program_id: str
    scope_type: str
    scope_id: str
    kind: str
    status: str
    title: str
    content: str
    confidence: float
    source_branch_id: str | None
    evidence_refs: list[str]
    supersedes_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class MemoryReview(BaseModel):
    status: Literal["accepted", "rejected", "superseded"]


class ReflectionWorkstream(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_id: str
    task_title: str
    program: str
    state: Literal[
        "completed",
        "in_progress",
        "waiting_external",
        "blocked",
        "failed",
        "unclear",
    ]
    summary: str
    next_action: str
    evidence_refs: list[str]


class ReflectionAccomplishment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    outcome: str
    significance: str
    evidence_refs: list[str]


class ReflectionLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    insight: str
    reusable_as_skill: bool
    evidence_refs: list[str]


class ReflectionShortcoming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: str
    evidence: str
    impact: str
    evidence_refs: list[str]


class ReflectionOptimization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Literal["P0", "P1", "P2"]
    action: str
    rationale: str
    success_signal: str
    evidence_refs: list[str]


class ReflectionTomorrowFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    success_signal: str
    evidence_refs: list[str]


class DailyReflectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str
    workstreams: list[ReflectionWorkstream]
    accomplishments: list[ReflectionAccomplishment]
    lessons: list[ReflectionLesson]
    shortcomings: list[ReflectionShortcoming]
    optimizations: list[ReflectionOptimization]
    tomorrow_focus: list[ReflectionTomorrowFocus]
    coverage_notes: list[str]


class DailyReflectionRead(ORMModel):
    id: str
    reflection_date: date
    timezone: str
    status: str
    source_started_at: datetime
    source_ended_at: datetime
    source_counts: dict[str, Any]
    evidence_refs: list[str]
    report_json: dict[str, Any]
    report_markdown: str
    agent_model: str | None
    agent_provider: str | None
    attempts: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    notification_status: str
    notification_attempts: int
    notification_sent_at: datetime | None
    notification_error: str | None
    created_at: datetime
    updated_at: datetime


class DailyReflectionRun(BaseModel):
    reflection_date: date | None = None
    force: bool = False


class BranchResultCreate(BaseModel):
    outcome: Literal["merge", "park", "abandon", "expire"]
    conclusion: str
    confidence: float = Field(default=0.5, ge=0, le=1)
    acceptance_results: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    proposed_promotions: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str | None = None


class BranchResultRead(ORMModel):
    id: str
    branch_id: str
    outcome: str
    conclusion: str
    confidence: float
    acceptance_results: list[dict[str, Any]]
    artifact_refs: list[str]
    proposed_promotions: list[dict[str, Any]]
    next_action: str | None
    created_at: datetime


class MergeReviewRead(ORMModel):
    id: str
    source_branch_id: str
    target_branch_id: str
    branch_result_id: str | None
    status: str
    resolution: str | None
    selected_changes: dict[str, Any]
    reviewer_note: str | None
    resolved_at: datetime | None
    created_at: datetime


class MergeResolve(BaseModel):
    resolution: Literal["merge", "park", "abandon"]
    selected_changes: dict[str, Any] = Field(default_factory=dict)
    reviewer_note: str | None = None


class SkillProposalCreate(BaseModel):
    requested: bool = True


class InstalledSkillRead(BaseModel):
    name: str
    directory_name: str
    description: str
    scope: Literal["personal", "system"]
    install_path: str
    source_path: str | None
    source_available: bool
    supporting_files: int
    valid: bool
    error: str | None
    updated_at: datetime


class SkillProposalRead(ORMModel):
    id: str
    branch_id: str
    session_id: str
    status: str
    action: str | None
    candidate_name: str | None
    target_skill: str | None
    title: str
    description: str
    rationale: str
    reusable_steps: list[str]
    risks: list[str]
    evidence_refs: list[str]
    source_context: dict[str, Any]
    proposed_content: str
    diff_text: str
    source_path: str | None
    install_path: str | None
    validation: dict[str, Any]
    error: str | None
    reviewer_note: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SkillProposalReview(BaseModel):
    reviewer_note: str | None = None


class ExternalJobCreate(BaseModel):
    work_item_id: str
    provider: str
    remote_job_id: str
    status: str = "unknown"
    url: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class ExternalJobRead(ORMModel):
    id: str
    work_item_id: str
    execution_id: str | None
    provider: str
    remote_job_id: str
    status: str
    url: str | None
    metadata_json: dict[str, Any]
    last_observed_at: datetime | None
    next_poll_at: datetime | None
    consecutive_failures: int
    created_at: datetime


class ExternalJobUpdate(BaseModel):
    status: str | None = None
    url: str | None = None
    metadata_json: dict[str, Any] | None = None


class GoalPolicy(BaseModel):
    """Explicit permissions for a background Goal worker."""

    allow_external_submit: bool = False
    allow_external_cancel: bool = False
    max_consecutive_errors: int = Field(default=3, ge=1, le=20)


class GoalCompletion(BaseModel):
    """Mechanical completion contracts supported by the first Goal worker."""

    kind: Literal["manual", "external_job", "checkpoint"] = "manual"
    external_job_id: str | None = None
    checkpoint_path: str | None = None
    checkpoint_iteration: int | None = Field(default=None, ge=0)
    expected_shards: int = Field(default=128, ge=1, le=65_536)

    @model_validator(mode="after")
    def require_contract_fields(self) -> GoalCompletion:
        if self.kind == "external_job" and not self.external_job_id:
            raise ValueError("external_job_id is required for external_job completion")
        if self.kind == "checkpoint" and (
            not self.checkpoint_path or self.checkpoint_iteration is None
        ):
            raise ValueError(
                "checkpoint_path and checkpoint_iteration are required for checkpoint completion"
            )
        return self


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1)
    executor: Literal["agent", "observer"] = "agent"
    cadence_seconds: int | None = Field(default=None, ge=30, le=86_400)
    max_agent_turns: int = Field(default=12, ge=1, le=200)
    policy: GoalPolicy = Field(default_factory=GoalPolicy)
    completion: GoalCompletion = Field(default_factory=GoalCompletion)
    # A Goal can create future agent turns and, when explicitly enabled, may
    # perform external submissions.  Require the initiating UI/API call to
    # carry a durable acknowledgement rather than trusting a model message.
    manual_approval: bool = False


class GoalControl(BaseModel):
    action: Literal["pause", "resume", "stop", "complete", "run_now"]


class GoalRead(ORMModel):
    id: str
    work_item_id: str
    branch_id: str
    title: str
    objective: str
    executor: Literal["agent", "observer"]
    status: Literal["active", "paused", "needs_decision", "completed", "failed", "stopped"]
    cadence_seconds: int
    max_agent_turns: int
    agent_turns_started: int
    policy_json: dict[str, Any]
    completion_json: dict[str, Any]
    state_json: dict[str, Any]
    next_run_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_started_at: datetime | None
    last_completed_at: datetime | None
    completed_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class GoalEventRead(ORMModel):
    id: str
    goal_run_id: str
    event_type: str
    message: str
    payload_json: dict[str, Any]
    created_at: datetime


class GoalDetail(BaseModel):
    goal: GoalRead
    events: list[GoalEventRead]


NativeGoalStatus = Literal[
    "active",
    "paused",
    "blocked",
    "usageLimited",
    "budgetLimited",
    "complete",
]


class NativeGoalRead(BaseModel):
    """The persisted Goal owned by one Codex thread."""

    thread_id: str
    objective: str
    status: NativeGoalStatus
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int


class NativeGoalRuntime(BaseModel):
    status: str = "unknown"
    last_checked_at: datetime | None = None
    last_activity_at: datetime | None = None
    last_summary: str | None = None
    last_error: str | None = None
    retry_attempts: int = 0
    next_retry_at: datetime | None = None
    notification_status: str | None = None
    notification_sent_at: datetime | None = None
    notification_error: str | None = None
    progress_interval_seconds: int = 900
    supervised: bool = True
    notifications_enabled: bool = False


class NativeGoalState(BaseModel):
    supported: bool
    thread_id: str | None = None
    goal: NativeGoalRead | None = None
    reason: str | None = None
    runtime: NativeGoalRuntime | None = None


class NativeGoalSet(BaseModel):
    objective: str = Field(min_length=1, max_length=4_000)
    token_budget: int | None = Field(default=None, ge=1)

    @field_validator("objective", mode="before")
    @classmethod
    def validate_native_goal_objective(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("The Goal objective cannot be blank")
        return value


class NativeGoalHistoryRead(BaseModel):
    goal_key: str
    thread_id: str
    objective: str
    status: str
    token_budget: int | None
    tokens_used: int | None
    time_used_seconds: int | None
    goal_created_at: int | None
    goal_updated_at: int | None
    first_recorded_at: datetime
    last_recorded_at: datetime
    cleared_at: datetime | None
    last_summary: str | None
    last_error: str | None
    turn_id: str | None
    source: Literal["live", "monitor", "notification"]


class NativeGoalHistoryPage(BaseModel):
    items: list[NativeGoalHistoryRead]
    total: int
    next_offset: int | None


class NativeGoalControl(BaseModel):
    action: Literal["pause", "resume", "complete", "clear"]


class ManagedJobSubmit(BaseModel):
    work_item_id: str = Field(min_length=1, max_length=32)
    submitter: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    job_kind: Literal["training", "conversion", "inference", "preprocessing", "other"]
    hardware: Literal["b", "h100"]
    config_path: str = Field(min_length=1)
    run_path: str = Field(min_length=1)
    train_path: str | None = None
    preflight_receipt_path: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)


class ManagedJobRead(BaseModel):
    id: str
    work_id: str
    name: str
    submitter: str
    job_kind: str
    hardware: str
    state: Literal[
        "accepted",
        "verifying",
        "queued",
        "running",
        "completed",
        "failed",
        "needs_review",
    ]
    requested_nodes: int
    requested_cards: int
    message: str
    created_at: datetime
    updated_at: datetime


class ExecutionRead(ORMModel):
    id: str
    work_item_id: str
    branch_id: str | None
    experiment_id: str | None
    execution_type: str
    status: str
    idempotency_key: str
    config_snapshot: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EvalRunCreate(BaseModel):
    work_item_id: str
    name: str = Field(min_length=1, max_length=320)
    adapter: str = Field(default="generic", min_length=1, max_length=80)
    mode: Literal["canonical", "fast"] = "canonical"
    candidate_id: str = Field(min_length=1, max_length=320)
    candidate_digest: str | None = Field(default=None, max_length=128)
    source_uri: str | None = None
    artifact_uri: str | None = None
    tool_parser: bool = True
    batch_size: int = Field(default=8, ge=1, le=128)
    max_attempts: int = Field(default=3, ge=1, le=10)
    config_json: dict[str, Any] = Field(default_factory=dict)


class EvalRunRead(ORMModel):
    id: str
    work_item_id: str
    name: str
    adapter: str
    mode: str
    status: str
    candidate_id: str
    candidate_digest: str | None
    config_hash: str
    config_json: dict[str, Any]
    source_uri: str | None
    artifact_uri: str | None
    tool_parser: bool
    batch_size: int
    max_attempts: int
    expected_units: int
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EvalSummary(BaseModel):
    total_units: int
    expected_units: int
    total_cases: int
    status_counts: dict[str, int]
    error_counts: dict[str, int]
    completed_units: int
    quarantined_units: int
    active_workers: int
    cache_hits: int
    attempts: int
    completion_ratio: float
    mean_score: float | None
    aggregate_score: float | None
    aggregate_cases: int
    throughput_units_per_minute: float | None
    eta_seconds: int | None


class EvalRunOverview(BaseModel):
    run: EvalRunRead
    summary: EvalSummary


class EvalPresentBenchImport(BaseModel):
    task_file: str = Field(min_length=1)
    result_root: str = Field(min_length=1)
    candidate_filename: str = Field(default="slides.pptx", min_length=1, max_length=240)
    limit: int | None = Field(default=None, ge=1, le=100_000)
    auto_start: bool = True


class EvalUnitSeed(BaseModel):
    case_key: str = Field(min_length=1, max_length=640)
    rubric_key: str = Field(min_length=1, max_length=240)
    rubric_group: str | None = Field(default=None, max_length=160)
    candidate_digest: str | None = Field(default=None, max_length=128)
    domain: str | None = Field(default=None, max_length=120)
    material_key: str | None = Field(default=None, max_length=640)
    priority: int = Field(default=50, ge=0, le=100)
    input_json: dict[str, Any] = Field(default_factory=dict)


class EvalUnitBatchImport(BaseModel):
    units: list[EvalUnitSeed] = Field(min_length=1, max_length=100_000)
    replace: bool = True
    auto_start: bool = True


class EvalImportResult(BaseModel):
    run: EvalRunRead
    imported_cases: int
    imported_units: int
    reused_units: int
    quarantined_units: int
    summary: EvalSummary


class EvalClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=8, ge=1, le=128)
    lease_seconds: int = Field(default=900, ge=30, le=7200)


class EvalWorkerRun(BaseModel):
    id: str
    adapter: str
    mode: str
    candidate_id: str
    config_hash: str
    config_json: dict[str, Any]
    tool_parser: bool


class EvalClaimUnit(BaseModel):
    id: str
    lease_token: str
    case_key: str
    domain: str | None
    rubric_key: str
    rubric_group: str
    material_key: str
    attempt_count: int
    lease_expires_at: datetime
    input_json: dict[str, Any]


class EvalClaimRead(BaseModel):
    run: EvalWorkerRun
    worker_id: str
    units: list[EvalClaimUnit]


class EvalUnitRead(ORMModel):
    id: str
    run_id: str
    case_key: str
    domain: str | None
    rubric_key: str
    rubric_group: str
    unit_key: str
    material_key: str
    priority: int
    status: str
    input_json: dict[str, Any]
    result_json: dict[str, Any]
    score: float | None
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    error_category: str | None
    error_message: str | None
    duration_ms: int | None
    cache_hit: bool
    cache_source_unit_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EvalUnitComplete(BaseModel):
    lease_token: str = Field(min_length=1, max_length=36)
    score: float = Field(ge=0.0, le=1.0)
    result_json: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = Field(default=None, ge=0)
    cache_hit: bool = False


class EvalUnitFailure(BaseModel):
    lease_token: str = Field(min_length=1, max_length=36)
    category: str = Field(default="worker_error", min_length=1, max_length=80)
    message: str = Field(default="", max_length=8000)
    retryable: bool = True
    duration_ms: int | None = Field(default=None, ge=0)


class EvalUnitHeartbeat(BaseModel):
    lease_token: str = Field(min_length=1, max_length=36)
    lease_seconds: int = Field(default=900, ge=30, le=7200)


class EvalRequeue(BaseModel):
    unit_ids: list[str] = Field(default_factory=list, max_length=5000)
    error_category: str | None = Field(default=None, max_length=80)


class EvalRequeueResult(EvalRunOverview):
    requeued_units: int


class EvalAudit(BaseModel):
    run_id: str
    valid: bool
    issues: list[dict[str, Any]]
    summary: EvalSummary


class AgentOSBatchSubmit(BaseModel):
    """Internal control-plane request for one Datawheel batch.

    ``payload`` is sent to AgentOS as-is, but only a redacted structural
    snapshot is persisted locally.  Callers should reference credentials via
    their configured environment/secret mechanism rather than putting them in
    Orbitree state.
    """

    work_item_id: str
    idempotency_key: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any]
    execution_type: str = Field(default="agentos_rollout", min_length=1, max_length=40)


class AgentOSBatchRead(BaseModel):
    execution: ExecutionRead
    external_job: ExternalJobRead


class AgentOSBatchStatusRead(BaseModel):
    batch_task_id: str
    status: str
    status_counts: dict[str, int]
    total_size: int | None
    external_job: ExternalJobRead | None = None


class PendingApprovalRead(ORMModel):
    id: str
    branch_id: str | None
    thread_id: str | None
    rpc_request_id: str
    method: str
    status: str
    params: dict[str, Any]
    response: dict[str, Any]
    created_at: datetime


class ApprovalResolve(BaseModel):
    decision: Literal["accept", "acceptForSession", "decline", "cancel"] | None = None
    response: dict[str, Any] | None = None


class ActivityRead(ORMModel):
    id: str
    scope_type: str
    scope_id: str
    event_type: str
    actor: str
    correlation_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class ConversationState(BaseModel):
    can_send: bool
    can_interrupt: bool = False
    web_turn_status: Literal["idle", "running", "completed", "failed", "interrupted"]
    local_turn_active: bool
    reason: str | None = None


class TreeResponse(BaseModel):
    program: ProgramRead
    work_items: list[WorkItemRead]
    branches: list[BranchRead]
    hidden_branches: list[BranchRead] = Field(default_factory=list)
    sessions: list[SessionRead]
    edges: list[dict[str, str]]


class BranchDetail(BaseModel):
    branch: BranchRead
    work_item: WorkItemRead
    runtime: Literal["codex", "pi", "claude"] = "codex"
    session: SessionRead | None
    turns: list[CodexTurnRead]
    messages: list[ConversationMessageRead]
    memory: list[MemoryRead]
    external_jobs: list[ExternalJobRead]
    goals: list[GoalRead] = Field(default_factory=list)
    approvals: list[PendingApprovalRead]
    activity: list[ActivityRead]
    conversation_state: ConversationState


class TodayResponse(BaseModel):
    continue_items: list[WorkItemRead]
    needs_decision: list[WorkItemRead]
    waiting_external: list[ExternalJobRead]
    suggested_next: list[WorkItemRead]
    active_sessions: int
    pending_merges: int


class ClusterOverviewSource(ORMModel):
    kind: Literal["live", "snapshot_file"]
    fetched_at: datetime
    cache_hit: bool
    degraded: bool
    stale: bool
    age_seconds: float
    refresh_error: str | None


class ClusterCoverage(BaseModel):
    source: str | None
    complete: bool
    requested_users: int
    successful_users: int
    failed_users: int
    failed_user_names: list[str]


class ClusterOverviewSummary(BaseModel):
    clusters: int
    resource_pools: int
    resource_domains: int
    available_cards: float
    free_nodes: int
    running_jobs: int
    queued_jobs: int
    running_nodes: int
    queued_nodes: int
    profiled_jobs: int
    estimated_jobs: int
    checkpoint_interval_jobs: int


class ClusterResource(BaseModel):
    name: str
    resource_pool: str | None
    resource_domain: str | None
    hardware: str
    scheduling_class: str
    quota_cards: float
    used_cards: float
    available_cards: float
    gpus_per_node: int
    free_nodes: int
    fragment_cards: float
    utilization_pct: float
    stable_window_seconds: float
    volatility: float
    forecast_source: str | None
    forecast_samples: int
    available_formula: str | None


class ClusterJob(ORMModel):
    id: str
    name: str
    user: str
    cluster: str
    state: Literal["running", "queued"]
    provider_status: str
    category: str
    nodes: int | None
    worker_nodes: int | None
    age: str | None
    submission_time: str | None
    last_status_update: str | None
    profiled: bool
    profile_rule: str | None
    classification_source: str | None
    blocked_reason: str | None
    eta_kind: Literal["remaining", "ready"] | None
    eta_seconds: float | None
    estimate_confidence: float | None
    estimate_source: str | None
    checkpoint_interval_seconds: float | None
    checkpoint_source: str | None
    last_checkpoint_at: datetime | None
    next_checkpoint_at: datetime | None
    next_checkpoint_seconds: float | None


class ClusterQueue(BaseModel):
    name: str
    resource: ClusterResource | None
    running_jobs: list[ClusterJob]
    queued_jobs: list[ClusterJob]
    running_nodes: int
    queued_nodes: int
    running_nodes_known_jobs: int
    queued_nodes_known_jobs: int
    profiled_jobs: int


class ClusterOverviewResponse(ORMModel):
    schema_version: Literal[1]
    mode: str
    observed_at: datetime
    resources_observed_at: datetime
    jobs_observed_at: datetime
    source: ClusterOverviewSource
    coverage: ClusterCoverage
    summary: ClusterOverviewSummary
    categories: dict[str, dict[str, int]]
    clusters: list[ClusterQueue]
    warnings: list[str]


class LtpMonitorStateRead(ORMModel):
    id: str
    initialized_at: datetime | None
    last_sync_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    source_json: dict[str, Any]


class LtpMonitoredJobRead(ORMModel):
    id: str
    user: str
    job_name: str
    cluster: str | None
    state: str
    provider_status: str | None
    state_group: str | None
    category: str | None
    first_observed_at: datetime
    last_observed_at: datetime
    last_status_at: datetime
    last_probe_at: datetime | None
    terminal_at: datetime | None
    missing_since: datetime | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class LtpMonitorEventRead(ORMModel):
    id: str
    job_id: str
    event_type: str
    previous_state: str | None
    state: str
    provider_status: str | None
    source: str
    observed_at: datetime
    detail_json: dict[str, Any]
    created_at: datetime


class LtpMonitorNotificationRead(ORMModel):
    id: str
    event_id: str
    channel: str
    target: str
    status: str
    attempts: int
    next_attempt_at: datetime | None
    sent_at: datetime | None
    provider_message_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class LtpMonitorOverviewRead(BaseModel):
    enabled: bool
    users: list[str]
    notifications_enabled: bool
    group_id: str | None
    state: LtpMonitorStateRead | None
    jobs: list[LtpMonitoredJobRead]
    events: list[LtpMonitorEventRead]
    notifications: list[LtpMonitorNotificationRead]


class LtpMonitorTrackCreate(BaseModel):
    job_name: str = Field(min_length=1, max_length=320)
    user: str | None = Field(default=None, max_length=160)
    cluster: str | None = Field(default=None, max_length=160)


class HealthResponse(BaseModel):
    status: str
    database: str
    codex: dict[str, Any]
    workspace_root: str
    frontend_built: bool
    conversation_input_mode: Literal["verbatim"] = "verbatim"


class RuntimeModelSettingsUpdate(BaseModel):
    codex_model: str | None = Field(default=None, min_length=1, max_length=160)
    codex_reasoning_effort: str | None = Field(default=None, min_length=1, max_length=24)
    pi_model: str | None = Field(default=None, min_length=1, max_length=160)
    pi_thinking: str | None = Field(default=None, min_length=1, max_length=24)

    @field_validator("codex_model", "codex_reasoning_effort", "pi_model", "pi_thinking")
    @classmethod
    def strip_optional_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime model settings must not be blank")
        return normalized


class RuntimeModelSettingsRead(BaseModel):
    codex_model: str
    codex_reasoning_effort: str
    pi_model: str
    pi_thinking: str
    model_presets: list[str]
    reasoning_efforts: list[str]
    source: Literal["environment", "platform"]
    updated_at: datetime | None = None
