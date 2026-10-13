import type {
  AgentRuntime,
  Approval,
  Branch,
  BranchDetail,
  BranchExecution,
  ClaudeBranchExecution,
  ClaudeExecutionPreferences,
  ExecutionPreferences,
  IdentityStatus,
  IdentityModels,
  CodexWindow,
  CodexWindowAdoption,
  CodexWindowTarget,
  ClusterOverview,
  ConversationMessage,
  ConversationState,
  DailyReflection,
  EvalAudit,
  EvalRun,
  EvalRunOverview,
  EvalUnit,
  Memory,
  ExternalJob,
  GoalCompletion,
  GoalDetail,
  GoalPolicy,
  GoalRun,
  NativeGoalState,
  NativeGoalHistoryPage,
  InstalledSkill,
  LtpMonitorOverview,
  LtpMonitoredJob,
  MergeReview,
  Program,
  ProgramTree,
  Repository,
  RuntimeControlResponse,
  RuntimeModelSettings,
  RuntimeState,
  SkillProposal,
  Today,
  WorkItem,
  SessionWorkflowView,
  ScheduledCheckDraft,
  ScheduledCheckPrompt,
  ScheduledCheckView,
  WorkflowReport,
  WorkflowLinkView,
  WebTerminalStatus,
} from "./types";

const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = sessionStorage.getItem("rops_access_token");
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    if (response.status === 401 && window.__ropsTeamMode) {
      window.dispatchEvent(new Event("rops-auth-expired"));
    }
    const payload = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  terminalStatus: (key = "") => request<WebTerminalStatus>("/terminal", { cache: "no-store", headers: { "X-Rops-Terminal-Key": key } }),
  connectTerminal: (key = "") => request<{ ticket: string }>("/terminal/connect", { method: "POST", headers: { "X-Rops-Terminal-Key": key } }),
  stopTerminal: (key = "") => request<WebTerminalStatus>("/terminal/stop", { method: "POST", headers: { "X-Rops-Terminal-Key": key } }),
  interruptTerminal: (key = "") => request<{ accepted: boolean }>("/terminal/interrupt", { method: "POST", headers: { "X-Rops-Terminal-Key": key } }),
  workflowLinks: (id: string) => request<WorkflowLinkView>(`/branches/${id}/workflow-links`),
  sessionWorkflow: (id: string) => request<SessionWorkflowView>(`/branches/${id}/workflow`),
  observeSessionWorkflow: (id: string, retry = false) =>
    request<SessionWorkflowView>(`/branches/${id}/workflow/observe?retry=${retry}`, { method: "POST" }),
  workflowReports: (id: string, workflowId: string, nodeId: string) =>
    request<{ reports: WorkflowReport[] }>(`/branches/${id}/workflow/reports?${new URLSearchParams({ workflow_id: workflowId, node_id: nodeId })}`),
  identityStatus: () => request<IdentityStatus>("/settings/identities", { cache: "no-store" }),
  identityModels: (identity: ExecutionPreferences["identity"], refresh = false) =>
    request<IdentityModels>(`/settings/identities/${identity}/models${refresh ? "?refresh=true" : ""}`, { cache: "no-store" }),
  branchExecution: (id: string) => request<BranchExecution>(`/branches/${id}/execution`, { cache: "no-store" }),
  claudeBranchExecution: (id: string) => request<ClaudeBranchExecution>(`/branches/${id}/claude-execution`, { cache: "no-store" }),
  updateClaudeBranchExecution: (id: string, input: ClaudeExecutionPreferences) => request<ClaudeBranchExecution>(`/branches/${id}/claude-execution`, {
    method: "PUT", body: JSON.stringify(input),
  }),
  updateBranchExecution: (id: string, input: ExecutionPreferences) => request<BranchExecution>(`/branches/${id}/execution`, {
    method: "PUT", body: JSON.stringify(input),
  }),
  runtimeModelSettings: () => request<RuntimeModelSettings>("/settings/runtime-model"),
  updateRuntimeModelSettings: (input: Partial<Pick<RuntimeModelSettings, "codex_model" | "codex_reasoning_effort" | "pi_model" | "pi_thinking">>) =>
    request<RuntimeModelSettings>("/settings/runtime-model", {
      method: "PATCH",
      body: JSON.stringify(input),
    }),
  programs: () => request<Program[]>("/programs"),
  allPrograms: () => request<Program[]>("/programs?include_archived=true"),
  createProgram: (input: Pick<Program, "code" | "name" | "objective" | "color" | "sort_order">) =>
    request<Program>("/programs", { method: "POST", body: JSON.stringify(input) }),
  updateProgram: (code: string, input: Pick<Program, "name" | "objective" | "color" | "sort_order" | "active">) =>
    request<Program>(`/programs/${code}`, { method: "PUT", body: JSON.stringify(input) }),
  today: () => request<Today>("/today"),
  clusterOverview: (refresh = false) =>
    request<ClusterOverview>(`/clusters/overview${refresh ? "?refresh=true" : ""}`, {
      cache: "no-store",
    }),
  ltpMonitor: () => request<LtpMonitorOverview>("/ltp-monitor", { cache: "no-store" }),
  refreshLtpMonitor: () => request<LtpMonitorOverview>("/ltp-monitor/refresh", {
    method: "POST",
    cache: "no-store",
  }),
  trackLtpJob: (input: { job_name: string; user?: string; cluster?: string }) =>
    request<LtpMonitoredJob>("/ltp-monitor/tasks", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  codexWindows: (includeIgnored = true) =>
    request<CodexWindow[]>(`/codex-windows?include_ignored=${includeIgnored}`),
  scanCodexWindows: () =>
    request<CodexWindow[]>("/codex-windows/scan", { method: "POST" }),
  generateCodexWindowTitles: () =>
    request<CodexWindow[]>("/codex-windows/generate-titles", { method: "POST" }),
  renameCodexWindow: (id: string, title: string) =>
    request<CodexWindow>(`/codex-windows/${id}/title`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  codexWindowTargets: (id: string) =>
    request<CodexWindowTarget[]>(`/codex-windows/${id}/attach-targets`),
  attachCodexWindow: (id: string, branchId: string) =>
    request<CodexWindow>(`/codex-windows/${id}/attach`, {
      method: "POST",
      body: JSON.stringify({ branch_id: branchId }),
    }),
  attachCodexWindowToParent: (id: string) =>
    request<CodexWindowAdoption>(`/codex-windows/${id}/attach-parent`, {
      method: "POST",
    }),
  createWorkFromCodexWindow: (id: string, programCode: string) =>
    request<CodexWindowAdoption>(`/codex-windows/${id}/create-work`, {
      method: "POST",
      body: JSON.stringify({ program_code: programCode }),
    }),
  ignoreCodexWindow: (id: string) =>
    request<CodexWindow>(`/codex-windows/${id}/ignore`, { method: "POST" }),
  restoreCodexWindow: (id: string) =>
    request<CodexWindow>(`/codex-windows/${id}/restore`, { method: "POST" }),
  tree: (program: string) => request<ProgramTree>(`/programs/${program}/tree`),
  programDesk: (program: string) => request<BranchDetail>(`/programs/${program}/desk`),
  branch: (id: string) => request<BranchDetail>(`/branches/${id}`),
  hideBranch: (id: string) =>
    request<Branch>(`/branches/${id}/hide`, { method: "POST" }),
  restoreBranch: (id: string) =>
    request<Branch>(`/branches/${id}/restore`, { method: "POST" }),
  conversationState: (branchId: string) =>
    // This is the composer lock signal. Never let an intermediary/browser
    // cache a previous `local_turn_active=true` response after the CLI exits.
    request<ConversationState>(`/branches/${branchId}/conversation-state`, {
      cache: "no-store",
    }),
  runtimeState: (branchId: string) =>
    request<RuntimeState>(`/branches/${branchId}/runtime-state`, {
      cache: "no-store",
    }),
  sendMessage: (branchId: string, content: string) =>
    request<ConversationMessage>(`/branches/${branchId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  interruptWebTurn: (branchId: string, messageId: string) =>
    request<ConversationMessage>(`/branches/${branchId}/messages/${messageId}/interrupt`, {
      method: "POST",
    }),
  steerRuntime: (branchId: string, content: string) =>
    request<RuntimeControlResponse>(`/branches/${branchId}/runtime/steer`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  followUpRuntime: (branchId: string, content: string) =>
    request<RuntimeControlResponse>(`/branches/${branchId}/runtime/follow-up`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  abortRuntime: (branchId: string) =>
    request<RuntimeControlResponse>(`/branches/${branchId}/runtime/abort`, {
      method: "POST",
    }),
  createWork: (input: {
    program_code: string;
    title?: string;
    objective?: string;
    definition_mode?: "defined" | "emerging";
    branch_mode: string;
    runtime?: AgentRuntime;
    priority: number;
    repository_id?: string;
    primary_parent_id?: string;
    acceptance_criteria?: Array<{ description: string; kind: string }>;
  }) => request<WorkItem>("/work-items", { method: "POST", body: JSON.stringify(input) }),
  quickStart: (programCode = "FREE") => request<WorkItem>("/work-items", {
    method: "POST",
    body: JSON.stringify({
      program_code: programCode,
      definition_mode: "emerging",
      branch_mode: "write",
      priority: 50,
    }),
  }),
  updateWork: (workId: string, input: Record<string, unknown>) =>
    request<WorkItem>(`/work-items/${workId}`, { method: "PATCH", body: JSON.stringify(input) }),
  undoWork: (workId: string) =>
    request<WorkItem>(`/work-items/${workId}`, { method: "DELETE" }),
  fork: (branchId: string, input: {
    name: string;
    objective: string;
    mode: string;
    continuation: "continue" | "parallel";
  }) =>
    request<Branch>(`/branches/${branchId}/fork`, {
      method: "POST",
      body: JSON.stringify({
        ...input,
        start_session: false,
        manual_approval: true,
      }),
    }),
  continueBranch: (branchId: string, force = false) =>
    request<Record<string, unknown>>(`/branches/${branchId}/continue`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),
  repairFork: (branchId: string, continuation: "continue" | "parallel" = "parallel") =>
    request<Branch>(`/branches/${branchId}/repair-fork`, {
      method: "POST",
      body: JSON.stringify({ continuation, manual_approval: true }),
    }),
  continueWithPi: (branchId: string) =>
    request<Branch>(`/branches/${branchId}/continue-with-pi`, {
      method: "POST",
      body: JSON.stringify({ manual_approval: true, continuation: "parallel" }),
    }),
  continueWithCodex: (branchId: string) =>
    request<Branch>(`/branches/${branchId}/continue-with-codex`, {
      method: "POST",
      body: JSON.stringify({ manual_approval: true, continuation: "parallel" }),
    }),
  claudeHandoff: (branchId: string) =>
    request<{ handoff_hash: string; turn_count: number; prompt: string; account: string; model: string; effort: string }>(`/branches/${branchId}/claude-handoff`),
  continueWithClaude: (branchId: string, input: { include_context: boolean; expected_handoff_hash?: string }) =>
    request<Branch>(`/branches/${branchId}/continue-with-claude`, {
      method: "POST",
      body: JSON.stringify({ manual_approval: true, continuation: "parallel", ...input }),
    }),
  reopenBranch: (branchId: string) =>
    request<Branch>(`/branches/${branchId}/reopen`, { method: "POST" }),
  recoverCodexThread: (branchId: string) =>
    request<Branch>(`/branches/${branchId}/recover-codex-thread`, { method: "POST" }),
  terminalCommand: (branchId: string) =>
    request<{ command: string; codex_command?: string; thread_id?: string }>(`/branches/${branchId}/terminal-command`),
  handoff: (branchId: string, sessionId: string, input: Record<string, unknown>) =>
    request(`/branches/${branchId}/sessions/${sessionId}/handoff`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  createSkillProposal: (branchId: string, sessionId: string) =>
    request<SkillProposal>(`/branches/${branchId}/sessions/${sessionId}/skill-proposals`, {
      method: "POST",
      body: JSON.stringify({ requested: true }),
    }),
  installedSkills: () => request<InstalledSkill[]>("/skills"),
  skillProposals: (status?: string) =>
    request<SkillProposal[]>(`/skill-proposals${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  acceptSkillProposal: (id: string, reviewerNote?: string) =>
    request<SkillProposal>(`/skill-proposals/${id}/accept`, {
      method: "POST",
      body: JSON.stringify({ reviewer_note: reviewerNote || null }),
    }),
  rejectSkillProposal: (id: string, reviewerNote?: string) =>
    request<SkillProposal>(`/skill-proposals/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewer_note: reviewerNote || null }),
    }),
  retrySkillProposal: (id: string) =>
    request<SkillProposal>(`/skill-proposals/${id}/retry`, { method: "POST" }),
  submitResult: (branchId: string, input: Record<string, unknown>) =>
    request(`/branches/${branchId}/result`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  resolveApproval: (approvalId: string, decision: string | Record<string, unknown>) =>
    request<Approval>(`/approvals/${approvalId}/resolve`, {
      method: "POST",
      body: JSON.stringify(typeof decision === "string" ? { decision } : { response: decision }),
    }),
  mergeQueue: () => request<MergeReview[]>("/merge-queue"),
  resolveMerge: (id: string, resolution: string) =>
    request<MergeReview>(`/merge-queue/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolution, selected_changes: {} }),
    }),
  memory: (program?: string) =>
    request<Memory[]>(`/memory${program ? `?program=${program}` : ""}`),
  dailyReflections: () => request<DailyReflection[]>("/daily-reflections"),
  runDailyReflection: (reflectionDate?: string, force = false) =>
    request<DailyReflection>("/daily-reflections/run", {
      method: "POST",
      body: JSON.stringify({ reflection_date: reflectionDate || null, force }),
    }),
  createMemory: (input: Record<string, unknown>) =>
    request<Memory>("/memory", { method: "POST", body: JSON.stringify(input) }),
  createExternalJob: (input: {
    work_item_id: string;
    provider: string;
    remote_job_id: string;
    status: string;
    url?: string;
  }) => request<ExternalJob>("/external-jobs", { method: "POST", body: JSON.stringify(input) }),
  updateExternalJob: (id: string, input: Record<string, unknown>) =>
    request<ExternalJob>(`/external-jobs/${id}`, { method: "PATCH", body: JSON.stringify(input) }),
  scheduledCheck: (branchId: string) => request<ScheduledCheckView>(`/branches/${branchId}/scheduled-check`, { cache: "no-store" }),
  cancelClaudeWakeup: (branchId: string, wakeupId: string) => request<ScheduledCheckView>(`/branches/${branchId}/claude-wakeups/${wakeupId}/cancel`, { method: "POST" }),
  draftScheduledCheckPrompt: (branchId: string, currentPrompt?: string, signal?: AbortSignal) => request<ScheduledCheckPrompt>(`/branches/${branchId}/scheduled-check/prompt`, { method: "POST", body: JSON.stringify({ current_prompt: currentPrompt || null }), signal }),
  saveScheduledCheck: (branchId: string, value: ScheduledCheckDraft) => request<ScheduledCheckView>(`/branches/${branchId}/scheduled-check`, { method: "PUT", body: JSON.stringify(value) }),
  controlScheduledCheck: (branchId: string, action: "pause" | "resume" | "stop" | "run_now") => request<ScheduledCheckView>(`/branches/${branchId}/scheduled-check/control`, { method: "POST", body: JSON.stringify({ action }) }),
  branchGoals: (branchId: string) => request<GoalRun[]>(`/branches/${branchId}/goals`),
  createGoal: (branchId: string, input: {
    title: string;
    objective: string;
    executor: "agent" | "observer";
    cadence_seconds?: number;
    max_agent_turns: number;
    policy: GoalPolicy;
    completion: GoalCompletion;
    manual_approval: true;
  }) => request<GoalRun>(`/branches/${branchId}/goals`, {
    method: "POST",
    body: JSON.stringify(input),
  }),
  goal: (goalId: string) => request<GoalDetail>(`/goals/${goalId}`),
  controlGoal: (goalId: string, action: "pause" | "resume" | "stop" | "complete" | "run_now") =>
    request<GoalRun>(`/goals/${goalId}/control`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  nativeGoal: (branchId: string) =>
    request<NativeGoalState>(`/branches/${branchId}/native-goal`, { cache: "no-store" }),
  nativeGoalHistory: (branchId: string, offset = 0) =>
    request<NativeGoalHistoryPage>(`/branches/${branchId}/native-goal/history?offset=${offset}&limit=20`, { cache: "no-store" }),
  setNativeGoal: (branchId: string, input: {
    objective: string;
    token_budget: number | null;
  }) => request<NativeGoalState>(`/branches/${branchId}/native-goal`, {
    method: "PUT",
    body: JSON.stringify(input),
  }),
  controlNativeGoal: (
    branchId: string,
    action: "pause" | "resume" | "complete" | "clear",
  ) => request<NativeGoalState>(`/branches/${branchId}/native-goal/control`, {
    method: "POST",
    body: JSON.stringify({ action }),
  }),
  evalRuns: () => request<EvalRunOverview[]>("/eval-runs", { cache: "no-store" }),
  createEvalRun: (input: {
    work_item_id: string;
    name: string;
    adapter: string;
    mode: "canonical" | "fast";
    candidate_id: string;
    source_uri?: string;
    artifact_uri?: string;
    tool_parser: boolean;
    batch_size: number;
    max_attempts: number;
    config_json: Record<string, unknown>;
  }) => request<EvalRun>("/eval-runs", { method: "POST", body: JSON.stringify(input) }),
  importPresentBench: (runId: string, input: {
    task_file: string;
    result_root: string;
    candidate_filename?: string;
    auto_start?: boolean;
  }) => request<Record<string, unknown>>(`/eval-runs/${runId}/import-presentbench`, {
    method: "POST",
    body: JSON.stringify(input),
  }),
  startEvalRun: (runId: string) =>
    request<EvalRunOverview>(`/eval-runs/${runId}/start`, { method: "POST" }),
  pauseEvalRun: (runId: string) =>
    request<EvalRunOverview>(`/eval-runs/${runId}/pause`, { method: "POST" }),
  requeueEvalRun: (runId: string) =>
    request<EvalRunOverview & { requeued_units: number }>(`/eval-runs/${runId}/requeue`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  auditEvalRun: (runId: string) => request<EvalAudit>(`/eval-runs/${runId}/audit`, {
    cache: "no-store",
  }),
  evalUnits: (runId: string, input: { status?: string; error_category?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (input.status) query.set("status", input.status);
    if (input.error_category) query.set("error_category", input.error_category);
    if (input.limit) query.set("limit", String(input.limit));
    return request<EvalUnit[]>(`/eval-runs/${runId}/units?${query}`, { cache: "no-store" });
  },
  reviewMemory: (id: string, status: string) =>
    request<Memory>(`/memory/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ status }),
    }),
  repositories: () => request<Repository[]>("/repositories"),
  createRepository: (input: {
    name: string;
    canonical_path: string;
    remote_url?: string;
    default_ref: string;
    program_codes: string[];
  }) => request<Repository>("/repositories", { method: "POST", body: JSON.stringify(input) }),
};

export function branchSocketUrl(branchId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = sessionStorage.getItem("rops_access_token");
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${protocol}//${window.location.host}${API}/ws/branches/${branchId}${query}`;
}
