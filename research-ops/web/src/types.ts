export type AgentRuntime = "codex" | "pi" | "claude";

export type WebTerminalStatus = {
  enabled: boolean;
  available: boolean;
  requires_unlock: boolean;
  active?: boolean;
  user?: string;
  hostname?: string;
  cwd?: string;
  shell?: string;
  persistent?: boolean;
  unlock_command?: string;
};

export type ScheduledCheckDraft = {
  prompt: string;
  interval_minutes: number;
  stop_mode: "manual" | "deadline" | "job";
  stop_at?: string | null;
  job_id?: string | null;
  notification_mode: "important" | "all" | "none";
};

export type ScheduledCheckPrompt = {
  prompt: string;
  provider: "oneapi";
  model: string;
  generated_at: string;
  context_updated_at: string;
  context_turns: number;
  context_messages: number;
  cached: boolean;
};

export type ScheduledCheck = ScheduledCheckDraft & {
  id: string;
  branch_id: string;
  status: string;
  phase: string;
  execution: Record<string, unknown>;
  next_run_at: string | null;
  active_run_id: string | null;
  last_completed_at: string | null;
  last_outcome: string | null;
  last_summary: string | null;
  consecutive_errors: number;
  reason: string | null;
};

export type ScheduledCheckView = {
  wakeups?: Array<{ id: string; status: string; prompt: string; due_at: string; reason: string | null; source_turn_id: string; message_id: string | null }>;
  check: ScheduledCheck | null;
  runs: Array<{ id: string; status: string; prompt: string; execution: Record<string, unknown>; message_id: string | null; outcome: string | null; summary: string | null; created_at: string; completed_at: string | null }>;
  jobs: Array<{ id: string; name: string; provider: string; status: string }>;
  default_prompt: string;
  notification: { status: string; error: string | null } | null;
  notifications_enabled: boolean;
  worker: {
    enabled: boolean;
    last_tick_at: string | null;
    error: string | null;
    status?: "healthy" | "starting" | "stale" | "error" | "disabled";
    checked_at?: string;
    stale_after_seconds?: number;
  };
};

export type ExecutionPreferences = {
  identity: "api" | "chatgpt" | "oneapi";
  model?: string;
  reasoning_effort?: string;
};

export type ExecutionIdentity = ExecutionPreferences & {
  label: string;
  provider: string;
  base_url: string;
};

export type IdentityStatus = {
  identities: Array<{ id: ExecutionPreferences["identity"]; label: string; ready: boolean; reason?: string; default_model?: string; default_reasoning_effort?: string }>;
  api_isolated: boolean;
  login_command: string;
};

export type BranchExecution = IdentityStatus & {
  runtime: AgentRuntime;
  preferences: Partial<ExecutionPreferences>;
  effective: ExecutionIdentity;
  native_goal_execution?: ExecutionIdentity;
};

export type ExecutionModel = {
  id: string;
  name: string;
  reasoning_efforts: string[];
  default_reasoning_effort?: string | null;
  is_default: boolean;
};

export type ClaudeExecutionPreferences = {
  model?: string;
  reasoning_effort?: string;
};

export type ClaudeBranchExecution = {
  runtime: "claude";
  preferences: ClaudeExecutionPreferences;
  effective: ClaudeExecutionPreferences & { identity: "claude"; label: string; provider: string };
  models: Array<{ id: string; name: string }>;
  reasoning_efforts: string[];
};

export type IdentityModels = {
  identity: ExecutionPreferences["identity"];
  models: ExecutionModel[];
  allow_custom: boolean;
};

export type RuntimeModelSettings = {
  codex_model: string;
  codex_reasoning_effort: string;
  pi_model: string;
  pi_thinking: string;
  model_presets: string[];
  reasoning_efforts: string[];
  source: "environment" | "platform";
  updated_at?: string;
};

export type Program = {
  id: string;
  code: string;
  slug: string;
  name: string;
  objective: string;
  color: string;
  sort_order: number;
  active: boolean;
};

export type Repository = {
  id: string;
  name: string;
  canonical_path: string;
  remote_url?: string;
  default_ref: string;
  program_codes: string[];
  active: boolean;
};

export type WorkItem = {
  id: string;
  human_id: string;
  program_id: string;
  primary_parent_id?: string;
  repository_id?: string;
  title: string;
  objective: string;
  kind: string;
  lifecycle: string;
  flow: string;
  priority: number;
  acceptance_criteria: Array<Record<string, unknown>>;
  labels: string[];
  hidden?: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type Branch = {
  id: string;
  work_item_id: string;
  parent_branch_id?: string;
  name: string;
  slug: string;
  status: string;
  hidden?: boolean;
  mode: string;
  cwd: string;
  workspace_path?: string;
  git_branch?: string;
  base_sha?: string;
  head_sha?: string;
  codex_thread_id?: string;
  runtime?: AgentRuntime;
  continued_by_branch_id?: string;
  context_snapshot: Record<string, unknown>;
  memory_cursor: number;
  created_at: string;
  updated_at: string;
};

export type Session = {
  id: string;
  branch_id: string;
  codex_thread_id?: string;
  runtime?: AgentRuntime;
  status: string;
  lease_id: string;
  lease_owner: string;
  lease_expires_at: string;
  heartbeat_at: string;
  brief: Record<string, unknown>;
  handoff: Record<string, unknown>;
  error?: string;
};

export type CodexWindow = {
  id: string;
  thread_id: string;
  parent_thread_id?: string;
  parent_window_id?: string;
  parent_work_id?: string;
  parent_program_code?: string;
  can_attach_to_parent: boolean;
  child_count: number;
  display_name: string;
  title_source: "ai" | "manual" | "work" | "fallback";
  cwd: string;
  cli_version?: string;
  originator?: string;
  pid?: number;
  tty?: string;
  live: boolean;
  state: "unassigned" | "managed" | "saved" | "ignored";
  match_confidence: string;
  ignored: boolean;
  started_at?: string;
  last_activity_at: string;
  branch_id?: string;
  branch_name?: string;
  work_id?: string;
  work_title?: string;
  program_code?: string;
  session_id?: string;
  session_status?: string;
  resume_command: string;
};

export type CodexWindowTarget = {
  branch_id: string;
  branch_name: string;
  work_id: string;
  work_title: string;
  program_code: string;
  cwd: string;
};

export type CodexWindowAdoption = {
  window: CodexWindow;
  work_item: WorkItem;
  branch: Branch;
};

export type ConversationMessage = {
  id: string;
  branch_id: string;
  session_id?: string;
  turn_id?: string;
  external_item_id?: string;
  role: "user" | "assistant" | "tool" | "system";
  kind: string;
  content: string;
  metadata_json: Record<string, unknown>;
  created_at: string;
};

export type ConversationState = {
  can_send: boolean;
  can_interrupt?: boolean;
  web_turn_status: "idle" | "running" | "completed" | "failed" | "interrupted";
  local_turn_active: boolean;
  reason?: string;
};

export type RuntimeControlResponse = {
  accepted: boolean;
  command: "steer" | "follow_up" | "abort";
  runtime: AgentRuntime;
  message_id?: string;
};

export type RuntimeState = {
  runtime: AgentRuntime;
  state: "idle" | "running" | "settled" | "failed" | "aborted";
  capabilities: string[];
  session_id?: string;
  owner?: string;
  queue: {
    steering?: string[];
    follow_up?: string[];
  };
};

export type CodexTurn = {
  id: string;
  branch_id: string;
  session_id?: string;
  thread_id: string;
  external_turn_id: string;
  status: string;
  prompt?: string;
  final_answer: string;
  started_at?: string;
  completed_at: string;
  duration_ms?: number;
  time_to_first_token_ms?: number;
  metadata_json: Record<string, unknown>;
};

export type Memory = {
  id: string;
  program_id: string;
  scope_type: string;
  scope_id: string;
  kind: string;
  status: string;
  title: string;
  content: string;
  confidence: number;
  source_branch_id?: string;
  evidence_refs: string[];
  version: number;
  created_at: string;
  updated_at: string;
};

export type DailyReflection = {
  id: string;
  reflection_date: string;
  timezone: string;
  status: "queued" | "running" | "completed" | "failed";
  source_started_at: string;
  source_ended_at: string;
  source_counts: Record<string, unknown>;
  evidence_refs: string[];
  report_json: Record<string, unknown>;
  report_markdown: string;
  agent_model?: string;
  agent_provider?: string;
  attempts: number;
  next_attempt_at?: string;
  started_at?: string;
  completed_at?: string;
  error?: string;
  notification_status: string;
  notification_attempts: number;
  notification_sent_at?: string;
  notification_error?: string;
  created_at: string;
  updated_at: string;
};

export type ExternalJob = {
  id: string;
  work_item_id: string;
  provider: string;
  remote_job_id: string;
  status: string;
  url?: string;
  metadata_json: Record<string, unknown>;
  last_observed_at?: string;
  created_at: string;
};

export type GoalPolicy = {
  allow_external_submit: boolean;
  allow_external_cancel: boolean;
  max_consecutive_errors: number;
};

export type GoalCompletion = {
  kind: "manual" | "external_job" | "checkpoint";
  external_job_id?: string;
  checkpoint_path?: string;
  checkpoint_iteration?: number;
  expected_shards?: number;
};

export type GoalRun = {
  id: string;
  work_item_id: string;
  branch_id: string;
  title: string;
  objective: string;
  executor: "agent" | "observer";
  status: "active" | "paused" | "needs_decision" | "completed" | "failed" | "stopped";
  cadence_seconds: number;
  max_agent_turns: number;
  agent_turns_started: number;
  policy_json: GoalPolicy;
  completion_json: GoalCompletion;
  state_json: Record<string, unknown>;
  next_run_at?: string;
  lease_owner?: string;
  lease_expires_at?: string;
  last_started_at?: string;
  last_completed_at?: string;
  completed_at?: string;
  error?: string;
  created_at: string;
  updated_at: string;
};

export type GoalEvent = {
  id: string;
  goal_run_id: string;
  event_type: string;
  message: string;
  payload_json: Record<string, unknown>;
  created_at: string;
};

export type GoalDetail = {
  goal: GoalRun;
  events: GoalEvent[];
};

export type NativeGoalStatus =
  | "active"
  | "paused"
  | "blocked"
  | "usageLimited"
  | "budgetLimited"
  | "complete";

export type NativeGoal = {
  thread_id: string;
  objective: string;
  status: NativeGoalStatus;
  token_budget?: number | null;
  tokens_used: number;
  time_used_seconds: number;
  created_at: number;
  updated_at: number;
};

export type NativeGoalState = {
  supported: boolean;
  thread_id?: string | null;
  goal?: NativeGoal | null;
  reason?: string | null;
  runtime?: {
    status: string;
    last_checked_at?: string | null;
    last_activity_at?: string | null;
    last_summary?: string | null;
    last_error?: string | null;
    retry_attempts: number;
    next_retry_at?: string | null;
    notification_status?: string | null;
    notification_sent_at?: string | null;
    notification_error?: string | null;
    progress_interval_seconds: number;
    supervised: boolean;
    notifications_enabled?: boolean;
  } | null;
};

export type NativeGoalHistoryEntry = {
  goal_key: string;
  thread_id: string;
  objective: string;
  status: string;
  token_budget: number | null;
  tokens_used: number | null;
  time_used_seconds: number | null;
  goal_created_at: number | null;
  goal_updated_at: number | null;
  first_recorded_at: string;
  last_recorded_at: string;
  cleared_at: string | null;
  last_summary: string | null;
  last_error: string | null;
  turn_id: string | null;
  source: "live" | "monitor" | "notification";
};

export type NativeGoalHistoryPage = {
  items: NativeGoalHistoryEntry[];
  total: number;
  next_offset: number | null;
};

export type ClusterOverviewSource = {
  kind: "live" | "snapshot_file";
  fetched_at: string;
  cache_hit: boolean;
  degraded: boolean;
  stale: boolean;
  age_seconds: number;
  refresh_error?: string | null;
};

export type ClusterCoverage = {
  source?: string | null;
  complete: boolean;
  requested_users: number;
  successful_users: number;
  failed_users: number;
  failed_user_names: string[];
};

export type ClusterOverviewSummary = {
  clusters: number;
  resource_pools: number;
  resource_domains: number;
  available_cards: number;
  free_nodes: number;
  running_jobs: number;
  queued_jobs: number;
  running_nodes: number;
  queued_nodes: number;
  profiled_jobs: number;
  estimated_jobs: number;
  checkpoint_interval_jobs: number;
};

export type ClusterResource = {
  name: string;
  resource_pool?: string | null;
  resource_domain?: string | null;
  hardware: string;
  scheduling_class: string;
  quota_cards: number;
  used_cards: number;
  available_cards: number;
  gpus_per_node: number;
  free_nodes: number;
  fragment_cards: number;
  utilization_pct: number;
  stable_window_seconds: number;
  volatility: number;
  forecast_source?: string | null;
  forecast_samples: number;
  available_formula?: string | null;
};

export type ClusterJob = {
  id: string;
  name: string;
  user: string;
  cluster: string;
  state: "running" | "queued";
  provider_status: string;
  category: string;
  nodes?: number | null;
  worker_nodes?: number | null;
  age?: string | null;
  submission_time?: string | null;
  last_status_update?: string | null;
  profiled: boolean;
  profile_rule?: string | null;
  classification_source?: string | null;
  blocked_reason?: string | null;
  eta_kind?: "remaining" | "ready" | null;
  eta_seconds?: number | null;
  estimate_confidence?: number | null;
  estimate_source?: string | null;
  checkpoint_interval_seconds?: number | null;
  checkpoint_source?: string | null;
  last_checkpoint_at?: string | null;
  next_checkpoint_at?: string | null;
  next_checkpoint_seconds?: number | null;
};

export type ClusterQueue = {
  name: string;
  resource?: ClusterResource | null;
  running_jobs: ClusterJob[];
  queued_jobs: ClusterJob[];
  running_nodes: number;
  queued_nodes: number;
  running_nodes_known_jobs: number;
  queued_nodes_known_jobs: number;
  profiled_jobs: number;
};

export type ClusterOverview = {
  schema_version: 1;
  mode: string;
  observed_at: string;
  resources_observed_at: string;
  jobs_observed_at: string;
  source: ClusterOverviewSource;
  coverage: ClusterCoverage;
  summary: ClusterOverviewSummary;
  categories: Record<string, { running: number; queued: number }>;
  clusters: ClusterQueue[];
  warnings: string[];
};

export type LtpMonitorState = {
  id: string;
  initialized_at?: string | null;
  last_sync_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  source_json: Record<string, unknown>;
};

export type LtpMonitoredJob = {
  id: string;
  user: string;
  job_name: string;
  cluster?: string | null;
  state: string;
  provider_status?: string | null;
  state_group?: string | null;
  category?: string | null;
  first_observed_at: string;
  last_observed_at: string;
  last_status_at: string;
  last_probe_at?: string | null;
  terminal_at?: string | null;
  missing_since?: string | null;
  metadata_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type LtpMonitorEvent = {
  id: string;
  job_id: string;
  event_type: string;
  previous_state?: string | null;
  state: string;
  provider_status?: string | null;
  source: string;
  observed_at: string;
  detail_json: Record<string, unknown>;
  created_at: string;
};

export type LtpMonitorNotification = {
  id: string;
  event_id: string;
  channel: string;
  target: string;
  status: string;
  attempts: number;
  next_attempt_at?: string | null;
  sent_at?: string | null;
  provider_message_id?: string | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
};

export type LtpMonitorOverview = {
  enabled: boolean;
  users: string[];
  notifications_enabled: boolean;
  group_id?: string | null;
  state?: LtpMonitorState | null;
  jobs: LtpMonitoredJob[];
  events: LtpMonitorEvent[];
  notifications: LtpMonitorNotification[];
};

export type EvalRun = {
  id: string;
  work_item_id: string;
  name: string;
  adapter: string;
  mode: "canonical" | "fast";
  status: "draft" | "running" | "paused" | "completed" | "needs_review" | string;
  candidate_id: string;
  candidate_digest?: string;
  config_hash: string;
  config_json: Record<string, unknown>;
  source_uri?: string;
  artifact_uri?: string;
  tool_parser: boolean;
  batch_size: number;
  max_attempts: number;
  expected_units: number;
  started_at?: string;
  completed_at?: string;
  created_at: string;
  updated_at: string;
};

export type EvalSummary = {
  total_units: number;
  expected_units: number;
  total_cases: number;
  status_counts: Record<string, number>;
  error_counts: Record<string, number>;
  completed_units: number;
  quarantined_units: number;
  active_workers: number;
  cache_hits: number;
  attempts: number;
  completion_ratio: number;
  mean_score?: number;
  aggregate_score?: number;
  aggregate_cases: number;
  throughput_units_per_minute?: number;
  eta_seconds?: number;
};

export type EvalRunOverview = {
  run: EvalRun;
  summary: EvalSummary;
};

export type EvalUnit = {
  id: string;
  run_id: string;
  case_key: string;
  domain?: string;
  rubric_key: string;
  status: string;
  score?: number;
  attempt_count: number;
  max_attempts: number;
  error_category?: string;
  error_message?: string;
  duration_ms?: number;
  cache_hit: boolean;
};

export type EvalAudit = {
  run_id: string;
  valid: boolean;
  issues: Array<Record<string, unknown>>;
  summary: EvalSummary;
};

export type Approval = {
  id: string;
  branch_id?: string;
  thread_id?: string;
  rpc_request_id: string;
  method: string;
  status: string;
  params: Record<string, unknown>;
  response: Record<string, unknown>;
  created_at: string;
};

export type Activity = {
  id: string;
  scope_type: string;
  scope_id: string;
  event_type: string;
  actor: string;
  correlation_id?: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type ProgramTree = {
  program: Program;
  work_items: WorkItem[];
  branches: Branch[];
  hidden_branches?: Branch[];
  sessions: Session[];
  edges: Array<{
    from_work_item_id: string;
    to_work_item_id: string;
    edge_type: string;
  }>;
};

export type BranchDetail = {
  branch: Branch;
  work_item: WorkItem;
  runtime?: AgentRuntime;
  session?: Session;
  turns: CodexTurn[];
  messages: ConversationMessage[];
  memory: Memory[];
  external_jobs: ExternalJob[];
  goals: GoalRun[];
  approvals: Approval[];
  activity: Activity[];
  conversation_state: ConversationState;
};

export type MergeReview = {
  id: string;
  source_branch_id: string;
  target_branch_id: string;
  branch_result_id?: string;
  status: string;
  resolution?: string;
  selected_changes: Record<string, unknown>;
  reviewer_note?: string;
  created_at: string;
};

export type SkillProposal = {
  id: string;
  branch_id: string;
  session_id: string;
  status: "pending" | "analyzing" | "ready" | "installing" | "accepted" | "rejected" | "failed" | "no_skill";
  action?: "new" | "merge" | "none";
  candidate_name?: string;
  target_skill?: string;
  title: string;
  description: string;
  rationale: string;
  reusable_steps: string[];
  risks: string[];
  evidence_refs: string[];
  source_context: Record<string, string>;
  proposed_content: string;
  diff_text: string;
  source_path?: string | null;
  install_path?: string;
  validation: Record<string, unknown>;
  error?: string | null;
  reviewer_note?: string;
  resolved_at?: string;
  created_at: string;
  updated_at: string;
};

export type InstalledSkill = {
  name: string;
  directory_name: string;
  description: string;
  scope: "personal" | "system";
  install_path: string;
  source_path?: string;
  source_available: boolean;
  supporting_files: number;
  valid: boolean;
  error?: string;
  updated_at: string;
};

export type Today = {
  continue_items: WorkItem[];
  needs_decision: WorkItem[];
  waiting_external: ExternalJob[];
  suggested_next: WorkItem[];
  active_sessions: number;
  pending_merges: number;
};
export type WorkflowNode = {
  id: string;
  title: string;
  status: string;
  raw_status: string;
  status_basis: "observed" | "reported";
  evidence_refs: string[];
  detail: string;
  associated: boolean;
};

export type SessionWorkflowGraph = {
  id: string;
  title: string;
  source: "octo" | "dag-runner" | "analysis";
  nodes: WorkflowNode[];
  edges: { source: string; target: string; evidence_refs?: string[] }[];
  observed_at: string | null;
  summary: string;
  stale: boolean;
};

export type WorkflowEvidence = {
  id: string;
  kind: string;
  label: string;
  excerpt: string;
  at: string | null;
  status: string;
};

export type WorkflowLinkView = {
  branch_id: string;
  links: { title: string; url: string; source: "conversation" | "octo"; record_id: string; at: string | null }[];
};

export type SessionWorkflowView = {
  branch_id: string;
  enabled: boolean;
  observing: boolean;
  analysis_status: string;
  analysis_error: string | null;
  analyzed_at: string | null;
  observed_at: string | null;
  next_analysis_at: string | null;
  analysis_stale: boolean;
  graphs: SessionWorkflowGraph[];
  evidence: WorkflowEvidence[];
  warnings: string[];
  octo: { state: string; last_synced_at: string | null; error: string | null };
};

export type WorkflowReport = {
  id: number;
  node_name: string;
  kind: string;
  revision: number;
  filename: string;
  format: "markdown" | "html";
  content: string;
  created_at: string;
};
