import { useEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Select,
  Switch,
  Tab,
  TabList,
  Textarea,
  Tooltip,
} from "@fluentui/react-components";
import {
  ArrowExit20Regular,
  ArrowRight20Regular,
  ArrowSync20Regular,
  BrainCircuit20Regular,
  Checkmark20Regular,
  Copy20Regular,
  Dismiss20Regular,
  Edit20Regular,
  Eye20Regular,
  EyeOff20Regular,
  Flag20Regular,
  Clock20Regular,
  Open20Regular,
  Play20Regular,
  Send20Regular,
} from "@fluentui/react-icons";
import { api, branchSocketUrl } from "../api";
import { copyText } from "../clipboard";
import { upsertConversationMessage } from "../conversation";
import { operationalStatus, sessionStatus } from "../status";
import { formatDateTime, formatTime, timestampMillis } from "../time";
import type { AgentRuntime, Branch, BranchDetail, CodexTurn, ConversationMessage, ConversationState, ExternalJob, NativeGoal, NativeGoalState, Program, RuntimeState, Session } from "../types";
import type { InspectorTab } from "../router";
import ExecutionControls, { ExecutionBadge } from "./ExecutionControls";
import ClaudeExecutionControls from "./ClaudeExecutionControls";
import ClaudeApprovals from "./ClaudeApprovals";
import CodexQuestions from "./CodexQuestions";
import GoalHistory from "./GoalHistory";
import SessionWorkflowPanel from "./SessionWorkflowPanel";
import ScheduledCheckControls from "./ScheduledCheckControls";
import ClaudeForkDialog from "./ClaudeForkDialog";

type ResultsView = "results" | "live";

type LiveDraft = {
  id: string;
  turnId?: string;
  externalItemId?: string;
  role: "assistant" | "tool" | "system";
  kind: string;
  content: string;
  createdAt: string;
  metadata: Record<string, unknown>;
  streaming: boolean;
};

type LiveFeedEntry = {
  id: string;
  role: "assistant" | "tool" | "system";
  kind: string;
  content: string;
  createdAt: string;
  metadata: Record<string, unknown>;
  streaming: boolean;
};

type ResultHistoryEntry =
  | { kind: "turn"; id: string; occurredAt: string; turn: CodexTurn }
  | { kind: "query"; id: string; occurredAt: string; message: ConversationMessage };

type ComposerDraft = {
  requestId: string;
  content: string;
};

type GoalCreateInput = Parameters<typeof api.setNativeGoal>[1];

const RESULTS_VIEW_STORAGE_KEY = "research-ops.results-view";
const MAX_LIVE_ENTRIES = 80;
const INITIAL_VISIBLE_TURNS = 5;
const TURN_PAGE_SIZE = 10;

function runtimeLabel(runtime: AgentRuntime | undefined): string {
  if (runtime === "pi") return "Pi";
  if (runtime === "claude") return "Claude";
  return "Codex";
}

export default function BranchPanel({ branchId, program, initialTab = "results", onTabChange, onClose, onOpenBranch }: {
  branchId: string;
  program: Program;
  initialTab?: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  onClose: () => void;
  onOpenBranch?: (branchId: string) => void;
}) {
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["branch", branchId],
    queryFn: () => api.branch(branchId),
    // The WebSocket carries turn/message changes. Keep polling as a fallback,
    // but do not repeatedly reload a several-hundred-KB result history while
    // the Branch is idle or the tab is in the background.
    refetchInterval: (query) => {
      const state = query.state.data?.conversation_state;
      return state?.local_turn_active || state?.web_turn_status === "running"
        ? 30_000
        : 120_000;
    },
    refetchIntervalInBackground: false,
  });
  const conversationStateQuery = useQuery({
    queryKey: ["conversation-state", branchId],
    queryFn: () => api.conversationState(branchId),
    // This payload is tiny and is deliberately polled independently from the
    // potentially large branch transcript, so a finished local CLI turn cannot
    // leave the composer disabled for a full transcript refresh interval.
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.local_turn_active || state?.web_turn_status === "running"
        ? 5_000
        : 20_000;
    },
    refetchIntervalInBackground: false,
  });
  const runtimeStateQuery = useQuery({
    queryKey: ["runtime-state", branchId],
    queryFn: () => api.runtimeState(branchId),
    enabled: detail.data?.runtime === "pi" || detail.data?.runtime === "claude",
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });
  const nativeGoalQuery = useQuery({
    queryKey: ["native-goal", branchId],
    queryFn: () => api.nativeGoal(branchId),
    enabled: detail.data?.runtime === "codex" && detail.data.branch.mode !== "capture",
    refetchInterval: (query) => query.state.data?.goal?.status === "active" ? 10_000 : 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const [tab, setTab] = useState<InspectorTab>(initialTab);
  const [handoffOpen, setHandoffOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [workStateOpen, setWorkStateOpen] = useState(false);
  const [goalOpen, setGoalOpen] = useState(false);
  const [claudeForkOpen, setClaudeForkOpen] = useState(false);
  const [actionNotice, setActionNotice] = useState("");
  const [createdRuntimeFork, setCreatedRuntimeFork] = useState<{
    branch: Branch;
    runtime: AgentRuntime;
  }>();
  const [resultsView, setResultsView] = useState<ResultsView>(readResultsView);
  const [liveDrafts, setLiveDrafts] = useState<Record<string, LiveDraft>>({});
  const [visibleHistoryCount, setVisibleHistoryCount] = useState(INITIAL_VISIBLE_TURNS);
  const [composerDraft, setComposerDraft] = useState<ComposerDraft>();
  const repairFork = useMutation({
    mutationFn: () => api.repairFork(branchId),
    onSuccess: async () => {
      setActionNotice("Fork repaired. This Branch now has an independent Codex thread.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const continueBranch = useMutation({
    mutationFn: () => api.continueBranch(branchId, true),
    onSuccess: async () => {
      setActionNotice("Child continuation requested. The first Codex turn is now queued by your explicit action.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const continueWithPi = useMutation({
    mutationFn: () => api.continueWithPi(branchId),
    onSuccess: async (child) => {
      setCreatedRuntimeFork({ branch: child, runtime: "pi" });
      setActionNotice("Created a parallel Pi fork with a frozen Codex handoff. The Codex Branch remains available.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const continueWithCodex = useMutation({
    mutationFn: () => api.continueWithCodex(branchId),
    onSuccess: async (child) => {
      setCreatedRuntimeFork({ branch: child, runtime: "codex" });
      setActionNotice(detail.data?.runtime === "claude"
        ? "Created a parallel Codex fork with a frozen Claude handoff. The Claude Branch remains available."
        : "Created a parallel Codex fork with a frozen Pi handoff. The Pi Branch remains available.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const continueWithClaude = useMutation({
    mutationFn: (input: Parameters<typeof api.continueWithClaude>[1]) => api.continueWithClaude(branchId, input),
    onSuccess: async (child) => {
      setClaudeForkOpen(false);
      setCreatedRuntimeFork({ branch: child, runtime: "claude" });
      setActionNotice("Claude fork created. The source Branch remains available.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const branchVisibility = useMutation({
    mutationFn: (hidden: boolean) => (
      hidden ? api.hideBranch(branchId) : api.restoreBranch(branchId)
    ),
    onSuccess: async (branch) => {
      setActionNotice(branch.hidden
        ? "Branch hidden. Its workspace, Codex thread, and lineage were preserved."
        : "Branch restored to the Program tree.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const reopenPath = useMutation({
    mutationFn: () => api.reopenBranch(branchId),
    onSuccess: async () => {
      setActionNotice("Parent path reopened. New turns can run here again.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", program.code] }),
      ]);
    },
  });
  const recoverCodexThread = useMutation({
    mutationFn: () => api.recoverCodexThread(branchId),
    onSuccess: async () => {
      setActionNotice("A fresh Codex session is ready. Previous results remain available; the next message will continue from a bounded recovery record.");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
      ]);
    },
  });
  const createGoal = useMutation({
    mutationFn: (input: GoalCreateInput) => api.setNativeGoal(branchId, input),
    onSuccess: async (state) => {
      setGoalOpen(false);
      setActionNotice(`Goal saved. Execution: ${nativeGoalExecutionLabel(state.runtime?.status)}.`);
      await Promise.all([
        queryClient.setQueryData(["native-goal", branchId], state),
        queryClient.invalidateQueries({ queryKey: ["native-goal-history", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
      ]);
    },
  });
  const controlGoal = useMutation({
    mutationFn: (action: "pause" | "resume" | "complete" | "clear") => (
      api.controlNativeGoal(branchId, action)
    ),
    onSuccess: async (state, action) => {
      const status = state.goal?.status ?? "cleared";
      setActionNotice(`Codex Native Goal ${action === "clear" ? "cleared" : status}.${state.goal ? ` Execution: ${nativeGoalExecutionLabel(state.runtime?.status)}.` : ""}`);
      queryClient.invalidateQueries({ queryKey: ["native-goal-history", branchId] });
      await Promise.all([
        queryClient.setQueryData(["native-goal", branchId], state),
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] }),
      ]);
    },
  });

  useEffect(() => {
    setTab(initialTab);
  }, [branchId, initialTab]);

  useEffect(() => {
    setLiveDrafts({});
    setVisibleHistoryCount(INITIAL_VISIBLE_TURNS);
    setCreatedRuntimeFork(undefined);
    setClaudeForkOpen(false);
  }, [branchId]);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | undefined;
    let reconnectDelay = 1_000;
    let socket: WebSocket | undefined;

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== undefined) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
    };

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(branchSocketUrl(branchId));
      socket.onopen = () => {
        reconnectDelay = 1_000;
        // The server sends a bounded result snapshot on connect. The
        // invalidate also repairs any response that arrived while this page
        // was disconnected, without polling the large transcript rapidly.
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
      };
      socket.onerror = () => socket?.close();
      socket.onclose = scheduleReconnect;
      socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as Record<string, unknown>;
      if (["approval.requested", "approval.resolved", "approval.expired", "workspace.snapshot_created"].includes(String(payload.type))) {
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      }
      if (payload.type === "turn.synced") {
        const turn = payload.turn as CodexTurn;
        queryClient.setQueryData<BranchDetail>(["branch", branchId], (current) => {
          if (!current) return current;
          const index = current.turns.findIndex((item) => item.id === turn.id);
          const turns = [...current.turns];
          if (index >= 0) turns[index] = turn;
          else turns.push(turn);
          return { ...current, turns };
        });
      }
      const localSnapshot = payload.events;
      if (payload.type === "local.live.snapshot" && Array.isArray(localSnapshot)) {
        setLiveDrafts((current) => upsertLocalLiveEvents(current, localSnapshot));
      }
      if (payload.type === "local.turn.started") {
        setLiveDrafts({});
      }
      if (payload.type === "turn.started") {
        // A Web turn owns a new live stream. Drop any transient events that a
        // reconnect kept from the preceding turn before this turn emits items.
        setLiveDrafts({});
      }
      if (payload.type === "local.live") {
        setLiveDrafts((current) => upsertLocalLiveEvents(current, [payload.event]));
      }
      const localBatch = payload.events;
      if (payload.type === "local.live.batch" && Array.isArray(localBatch)) {
        setLiveDrafts((current) => upsertLocalLiveEvents(current, localBatch));
      }
      if (payload.type === "local.turn.completed") {
        const turnId = stringValue(payload.turn_id);
        const status = stringValue(payload.status) ?? "completed";
        setLiveDrafts((current) => finishTurnDrafts(current, turnId, status));
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
      }
      if (payload.type === "message.created" || payload.type === "message.updated") {
        const message = payload.message as BranchDetail["messages"][number];
        queryClient.setQueryData<BranchDetail>(
          ["branch", branchId],
          (current) => upsertConversationMessage(current, message),
        );
        if (
          message.role === "user"
          && ["web", "goal"].includes(String(message.metadata_json.origin))
          && message.metadata_json.send_status === "pending"
        ) {
          setLiveDrafts({});
        }
        const externalItemId = message.external_item_id;
        if (externalItemId) {
          setLiveDrafts((current) => removeLiveDraft(current, externalItemId));
        }
      }
      if (payload.type === "assistant.delta" || payload.type === "command.delta") {
        const role = payload.type === "assistant.delta" ? "assistant" : "tool";
        const kind = role === "assistant" ? "message" : "command";
        setLiveDrafts((current) => appendLiveDraft(current, {
          turnId: stringValue(payload.turn_id),
          externalItemId: stringValue(payload.item_id),
          role,
          kind,
          delta: stringValue(payload.delta) ?? "",
        }));
      }
      if (payload.type === "item.started") {
        const item = objectValue(payload.item);
        const draft = startedItemDraft(item, stringValue(payload.turn_id));
        if (draft) {
          setLiveDrafts((current) => ({ ...current, [draft.id]: current[draft.id] ?? draft }));
        }
      }
      if (payload.type === "turn.completed") {
        const turn = objectValue(payload.turn);
        const turnId = stringValue(turn.id);
        if (turnId) setLiveDrafts((current) => removeTurnDrafts(current, turnId));
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
      }
      if (payload.type === "codex.error") {
        const content = codexErrorText(payload.error);
        const id = `error:${Date.now()}`;
        setLiveDrafts((current) => ({
          ...current,
          [id]: {
            id,
            role: "system",
            kind: "error",
            content,
            createdAt: new Date().toISOString(),
            metadata: { status: "failed" },
            streaming: false,
          },
        }));
      }
      if (payload.type === "conversation.state") {
        const state = payload.conversation_state as ConversationState;
        queryClient.setQueryData<ConversationState>(["conversation-state", branchId], state);
        queryClient.setQueryData<BranchDetail>(["branch", branchId], (current) => (
          current
            ? { ...current, conversation_state: state }
            : current
        ));
      }
      if (payload.type === "session.status") {
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
      }
      if (payload.type === "goal.updated") {
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["today"] });
      }
      if (payload.type === "scheduled_check.updated") {
        queryClient.invalidateQueries({ queryKey: ["scheduled-check", branchId] });
      }
      if (payload.type === "native_goal.updated" || payload.type === "native_goal.cleared") {
        queryClient.invalidateQueries({ queryKey: ["native-goal", branchId] });
        queryClient.invalidateQueries({ queryKey: ["native-goal-history", branchId] });
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
        queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
      }
      const piEvent = payload.type === "codex.event"
        && ["pi/queue_update", "pi/settled"].includes(String(payload.method));
      if (piEvent || ["runtime.command_submitted", "turn.completed"].includes(String(payload.type))) {
        queryClient.invalidateQueries({ queryKey: ["runtime-state", branchId] });
      }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [branchId, queryClient]);

  if (detail.isLoading) return <div className="inspector-loading">Loading branch</div>;
  if (detail.error || !detail.data) return <div className="inspector-error">{detail.error?.message}</div>;
  const data = detail.data;
  const agentRuntime = data.runtime ?? "codex";
  const conversationState = isConversationState(conversationStateQuery.data)
    ? conversationStateQuery.data
    : data.conversation_state ?? {
    can_send: false,
    web_turn_status: "idle" as const,
    local_turn_active: false,
    reason: "Conversation state is unavailable. Refresh this branch.",
  };
  const runtimeState = runtimeStateQuery.data?.runtime ? runtimeStateQuery.data : undefined;
  const webTurnRunning = conversationState.web_turn_status === "running";
  const conversationRunning = webTurnRunning || conversationState.local_turn_active;
  const pendingWebMessage = data.messages
    .slice()
    .reverse()
    .find((message) => (
      message.role === "user"
      && ["web", "continuation", "goal"].includes(String(message.metadata_json.origin))
      && message.metadata_json.send_status === "pending"
    ));
  const currentTurnUpdate = pendingWebMessage
    ? latestAssistantUpdate(data.messages, pendingWebMessage, Object.values(liveDrafts))
    : undefined;
  const isProgramDesk = data.work_item.kind === "program_desk";
  const isPrimaryPath = !data.branch.parent_branch_id;
  const branchVisibilitySupported = typeof data.branch.hidden === "boolean";
  const title = isProgramDesk
    ? `${program.name} Desk`
    : isPrimaryPath ? data.work_item.title : data.branch.name;
  const lastSessionStatus = sessionStatus(data.session);
  const workStatus = conversationRunning
    ? "Running"
    : operationalStatus({ work: data.work_item, externalJobs: data.external_jobs });
  // Keep the latest Session outcome visible, while exposing availability as a
  // separate signal so an Error/Expired record does not look like a lock.
  const currentSessionStatus = conversationRunning ? "Running" : lastSessionStatus;
  const active = currentSessionStatus === "Running";
  const webSessionActive = active && data.session?.lease_owner === "web";
  const provisioning = data.branch.context_snapshot.provisioning as Record<string, unknown> | undefined;
  const continuation = data.branch.context_snapshot.auto_continuation as Record<string, unknown> | undefined;
  const continuationMode = data.branch.context_snapshot.continuation;
  const continuationStatus = typeof continuation?.status === "string" ? continuation.status : undefined;
  const childContinuationNeedsStart = Boolean(
    data.branch.parent_branch_id
    && !data.branch.hidden
    && continuationMode === "continue"
    && data.branch.status === "active"
    && continuationStatus !== "queued"
    && continuationStatus !== "running"
    && continuationStatus !== "completed",
  );
  const nativeGoalState = nativeGoalQuery.data;
  const visibleGoal = nativeGoalState?.goal ?? null;
  const hasNativeGoal = Boolean(visibleGoal);
  const forkNeedsRepair = Boolean(
    data.branch.parent_branch_id
    && data.branch.mode !== "capture"
    && !data.branch.codex_thread_id,
  );
  const closedPath = data.branch.hidden || [
    "review",
    "parked",
    "abandoned",
    "expired",
    "continued",
    "provisioning",
    "blocked",
  ].includes(data.branch.status) || forkNeedsRepair;
  const sessionCanContinue = !active && conversationState.can_send && !closedPath;
  const sessionStatusDetail = lastSessionStatus === "Error"
    ? data.session?.error || "The previous session ended with an error."
    : lastSessionStatus === "Expired"
      ? "The previous session lease expired."
      : undefined;
  const resultDisabledReason = active
    ? `Wait for the active ${runtimeLabel(agentRuntime)} turn before submitting a result.`
    : closedPath
      ? `This path is already ${data.branch.status}.`
      : "";
  const legacyTurns: CodexTurn[] = data.messages
    .filter((message) => message.role === "assistant" && message.kind === "turn_result")
    .map((message) => ({
      id: message.id,
      branch_id: message.branch_id,
      session_id: message.session_id,
      thread_id: String(message.metadata_json.codex_thread_id ?? data.branch.codex_thread_id ?? ""),
      external_turn_id: message.turn_id ?? message.id,
      status: "completed",
      final_answer: message.content,
      completed_at: message.created_at,
      duration_ms: typeof message.metadata_json.duration_ms === "number" ? message.metadata_json.duration_ms : undefined,
      metadata_json: message.metadata_json,
  }));
  const turnResults = (data.turns?.length ? data.turns : legacyTurns).slice().reverse();
  const incompleteQueries = data.messages.filter(isIncompleteWebQuery);
  const repeatedGatewayFailures = incompleteQueries.filter((message) => (
    message.metadata_json.send_status === "failed"
    && isGatewayDisconnect(String(message.metadata_json.error ?? ""))
  )).length;
  const resultHistory: ResultHistoryEntry[] = [
    ...turnResults.map((turn): ResultHistoryEntry => ({
      kind: "turn",
      id: `turn:${turn.id}`,
      occurredAt: turn.completed_at,
      turn,
    })),
    ...incompleteQueries.map((message): ResultHistoryEntry => ({
      kind: "query",
      id: `query:${message.id}`,
      occurredAt: message.created_at,
      message,
    })),
  ].sort((left, right) => timestampMillis(right.occurredAt) - timestampMillis(left.occurredAt));
  // A result-heavy Branch can bury an interrupted Web query below the first
  // page. Keep the newest incomplete query visible until pagination reaches
  // it, so an interruption never appears to have been silently discarded.
  const pagedResultHistory = resultHistory.slice(0, visibleHistoryCount);
  const newestIncompleteQuery = resultHistory.find((entry) => entry.kind === "query");
  const visibleResultHistory = newestIncompleteQuery
    && !pagedResultHistory.some((entry) => entry.id === newestIncompleteQuery.id)
    ? [...pagedResultHistory, newestIncompleteQuery]
    : pagedResultHistory;
  const hiddenHistoryCount = Math.max(0, resultHistory.length - visibleResultHistory.length);
  const canHandoff = Boolean(
    data.session
    && !active
    && !Object.keys(data.session.handoff).length
    && ["completed", "error", "expired"].includes(data.session.status),
  );
  const canContinueWithPi = agentRuntime === "codex"
    && !isProgramDesk
    && !active
    && !closedPath
    && !data.branch.continued_by_branch_id
    && Boolean(data.branch.codex_thread_id && !data.branch.codex_thread_id.startsWith("deferred:"))
    && turnResults.length > 0;
  const canContinueWithCodex = (agentRuntime === "pi" || agentRuntime === "claude")
    ? !isProgramDesk
      && !active
      && !closedPath
      && !data.branch.continued_by_branch_id
      && Boolean(data.branch.codex_thread_id)
      && turnResults.length > 0
    : false;
  const canContinueWithClaude = agentRuntime !== "claude"
    && !isProgramDesk
    && !active
    && !closedPath
    && !data.branch.continued_by_branch_id
    && Boolean(data.branch.codex_thread_id)
    && turnResults.length > 0;
  const changeResultsView = (value: ResultsView) => {
    setResultsView(value);
    try {
      window.localStorage.setItem(RESULTS_VIEW_STORAGE_KEY, value);
    } catch {
      // The view still works when browser storage is disabled.
    }
  };

  return (
    <div className="branch-panel">
      <header className="branch-panel-header">
        <div>
          <span>{data.work_item.human_id}</span>
          <h2>{title}</h2>
          <small>{isProgramDesk ? `${program.code} · coordinator` : isPrimaryPath ? `${data.branch.mode} · primary path` : (data.branch.git_branch ?? data.branch.mode)}</small>
        </div>
        <Tooltip content="Close inspector" relationship="label">
          <Button appearance="subtle" icon={<Dismiss20Regular />} onClick={onClose} />
        </Tooltip>
      </header>

      <div className={`branch-statusbar${visibleGoal ? " has-native-goal" : ""}`}>
        <div className="branch-status-pair" aria-label="Work, session, and Goal status">
          <span>{isProgramDesk ? "Desk" : "Work"} <strong className={`operational-status status-${workStatus.toLowerCase().replaceAll(" ", "-")}`}>{workStatus}</strong></span>
          <span>
            Session {sessionStatusDetail ? <Tooltip content={sessionStatusDetail} relationship="description"><strong className={`operational-status status-${currentSessionStatus.toLowerCase().replaceAll(" ", "-")}`}>{currentSessionStatus}</strong></Tooltip> : <strong className={`operational-status status-${currentSessionStatus.toLowerCase().replaceAll(" ", "-")}`}>{currentSessionStatus}</strong>}
          </span>
          {visibleGoal && <GoalRuntimeStatus goal={visibleGoal} runtime={nativeGoalState?.runtime} />}
          {data.branch.status === "continued" && <span>Path <strong className="operational-status status-continued">Continued</strong></span>}
          {data.branch.hidden && <span>Visibility <strong className="operational-status">Hidden</strong></span>}
          {sessionCanContinue && <span className="branch-session-availability">Ready to continue</span>}
        </div>
        <div className="branch-status-actions">
          {agentRuntime !== "pi" && data.branch.mode !== "capture" && !active && !closedPath && <LocalCodexButton branchId={branchId} hasThread={Boolean(data.branch.codex_thread_id && !data.branch.codex_thread_id.startsWith("deferred:"))} />}
          {canContinueWithPi && <Tooltip content="Create a parallel Pi fork from completed Codex results and the current workspace. The current Codex Branch remains available." relationship="description">
            <Button
              size="small"
              appearance="secondary"
              icon={<ArrowRight20Regular />}
              disabled={continueWithPi.isPending}
              onClick={() => continueWithPi.mutate()}
            >
              Fork with Pi
            </Button>
          </Tooltip>}
          {canContinueWithClaude && <Tooltip content="Create a parallel Claude fork from completed results and the current workspace. The current Branch remains available." relationship="description">
            <Button
              size="small"
              appearance="secondary"
              icon={<ArrowRight20Regular />}
              disabled={continueWithClaude.isPending}
              onClick={() => { continueWithClaude.reset(); setClaudeForkOpen(true); }}
            >
              Fork with Claude
            </Button>
          </Tooltip>}
          {canContinueWithCodex && <Tooltip content="Create a parallel Codex fork from completed Pi results and the current workspace. The current Pi Branch remains available." relationship="description">
            <Button
              size="small"
              appearance="secondary"
              icon={<ArrowRight20Regular />}
              disabled={continueWithCodex.isPending}
              onClick={() => continueWithCodex.mutate()}
            >
              Continue with Codex
            </Button>
          </Tooltip>}
          {active && <Button size="small" appearance="primary" icon={<Play20Regular />} disabled>{webSessionActive ? "Running on Web" : "Running locally"}</Button>}
          {canHandoff && <Button size="small" appearance="subtle" icon={<ArrowExit20Regular />} onClick={() => setHandoffOpen(true)}>End this round</Button>}
          {!isProgramDesk && ["Blocked", "Waiting external", "Needs decision", "Review"].includes(workStatus) && (
            <Button size="small" appearance="primary" icon={<Edit20Regular />} onClick={() => setWorkStateOpen(true)}>Update</Button>
          )}
          {!isProgramDesk && agentRuntime === "codex" && <Tooltip content={hasNativeGoal ? "Inspect and control this thread's Codex Native Goal" : "Start a Codex Native Goal on this thread"} relationship="description">
            <Button
              size="small"
              appearance="subtle"
              icon={<Flag20Regular />}
              onClick={() => {
                if (hasNativeGoal) {
                  setTab("goals");
                  onTabChange("goals");
                } else {
                  setGoalOpen(true);
                }
              }}
            >
              {hasNativeGoal ? "Goal" : "Set goal"}
            </Button>
          </Tooltip>}
          <Tooltip content="Record memory" relationship="label">
            <Button size="small" appearance="subtle" icon={<BrainCircuit20Regular />} onClick={() => setMemoryOpen(true)} />
          </Tooltip>
          {data.branch.parent_branch_id && branchVisibilitySupported && <Tooltip
            content={data.branch.hidden
              ? "Restore this Branch to the Program tree"
              : "Hide this Branch without deleting its files, thread, or history"}
            relationship="description"
          >
            <Button
              size="small"
              appearance="subtle"
              icon={data.branch.hidden ? <Eye20Regular /> : <EyeOff20Regular />}
              disabled={active || branchVisibility.isPending}
              onClick={() => branchVisibility.mutate(!data.branch.hidden)}
            >
              {data.branch.hidden ? "Restore" : "Hide"}
            </Button>
          </Tooltip>}
          {!isProgramDesk && <Tooltip content={resultDisabledReason || "Submit a durable result for this path"} relationship="description">
            <span className="tooltip-button-wrap">
              <Button
                size="small"
                appearance="subtle"
                icon={<Flag20Regular />}
                disabled={Boolean(resultDisabledReason)}
                aria-label={resultDisabledReason ? `Submit result unavailable: ${resultDisabledReason}` : "Submit result"}
                onClick={() => setResultOpen(true)}
              >
                Submit result
              </Button>
            </span>
          </Tooltip>}
        </div>
      </div>
      {actionNotice && <div className="action-feedback" role="status">
        <Checkmark20Regular />
        <span>{actionNotice}</span>
        {createdRuntimeFork && <Button
          appearance="subtle"
          size="small"
          onClick={() => onOpenBranch?.(createdRuntimeFork.branch.id)}
        >
          Open {runtimeLabel(createdRuntimeFork.runtime)} fork
        </Button>}
        <Button appearance="subtle" size="small" icon={<Dismiss20Regular />} aria-label="Dismiss result confirmation" onClick={() => setActionNotice("")} />
      </div>}
      {branchVisibility.error && <div className="inline-error" role="alert">
        {branchVisibility.error.message}
      </div>}
      {continueWithPi.error && <div className="inline-error" role="alert">
        {continueWithPi.error.message}
      </div>}
      {continueWithCodex.error && <div className="inline-error" role="alert">
        {continueWithCodex.error.message}
      </div>}
      {agentRuntime === "claude" && objectValue(data.branch.context_snapshot.claude_handoff_delivery)?.approved === true && <details className="continuation-notice">
        <summary>Conversation handoff · first-turn context</summary>
        <pre style={{ maxHeight: "20rem", overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(data.branch.context_snapshot.runtime_handoff, null, 2)}</pre>
      </details>}
      {agentRuntime === "claude" && objectValue(data.branch.context_snapshot.workspace_snapshot)?.state === "ready" && <details className="continuation-notice workspace-snapshot">
        <summary>Workspace snapshot · {String(objectValue(data.branch.context_snapshot.workspace_snapshot).file_count)} files</summary>
        <pre style={{ maxHeight: "14rem", overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(data.branch.context_snapshot.workspace_snapshot, null, 2)}</pre>
      </details>}
      {agentRuntime === "claude" && <ClaudeApprovals branchId={branchId} approvals={data.approvals} />}
      {agentRuntime === "codex" && <CodexQuestions branchId={branchId} approvals={data.approvals} />}
      {continueWithClaude.error && !claudeForkOpen && <div className="inline-error" role="alert">
        {continueWithClaude.error.message}
      </div>}
      {agentRuntime === "codex" && repeatedGatewayFailures >= 2 && !active && !closedPath && !data.branch.codex_thread_id?.startsWith("deferred:") && <div className="continuation-notice fork-failed" role="alert">
        <ArrowSync20Regular />
        <div>
          <strong>Codex session needs recovery</strong>
          <span>{repeatedGatewayFailures} recent Web requests disconnected before any final result. Starting a fresh session keeps this Work, Branch, directory, and historical results.</span>
          {recoverCodexThread.error && <small>{recoverCodexThread.error.message}</small>}
        </div>
        <Button
          size="small"
          appearance="primary"
          disabled={recoverCodexThread.isPending}
          onClick={() => recoverCodexThread.mutate()}
        >
          Start fresh Codex session
        </Button>
      </div>}
      {data.branch.status === "continued" && data.branch.continued_by_branch_id && <div className="continuation-notice" role="status">
        <ArrowRight20Regular />
        <div>
          <strong>This conversation continued in a child Branch</strong>
          <span>Future turns belong there unless you explicitly reopen this parent path.</span>
        </div>
        <Button
          size="small"
          appearance="primary"
          onClick={() => onOpenBranch?.(data.branch.continued_by_branch_id!)}
        >
          Open child
        </Button>
        <Button
          size="small"
          appearance="subtle"
          disabled={reopenPath.isPending}
          onClick={() => reopenPath.mutate()}
        >
          Reopen parent
        </Button>
      </div>}
      {data.branch.parent_branch_id && continuationMode === "continue" && continuationStatus && continuationStatus !== "completed" && <div className="continuation-notice" role={continuationStatus === "failed" ? "alert" : "status"}>
        <ArrowRight20Regular />
        <div>
          <strong>Child continuation: {continuationStatus}</strong>
          <span>{continuationStatus === "failed"
            ? String(continuation?.error ?? "The requested child turn failed.")
            : continuationStatus === "queued"
              ? "The child turn is queued and will start when the dispatcher picks it up."
              : "The child Codex turn is running."}</span>
        </div>
        {continuationStatus === "failed" && <Button
          size="small"
          appearance="primary"
          disabled={continueBranch.isPending}
          onClick={() => continueBranch.mutate()}
        >
          Retry child
        </Button>}
      </div>}
      {childContinuationNeedsStart && <div className="continuation-notice fork-failed" role="alert">
        <ArrowRight20Regular />
        <div>
          <strong>Child Branch is ready but has not executed</strong>
          <span>The fork has a Codex thread but no first turn. Start the continuation to execute its objective.</span>
        </div>
        <Button
          size="small"
          appearance="primary"
          disabled={continueBranch.isPending}
          onClick={() => continueBranch.mutate()}
        >
          Start child continuation
        </Button>
      </div>}
      {forkNeedsRepair && <div className="continuation-notice fork-failed" role="alert">
        <ArrowSync20Regular />
        <div>
          <strong>Codex fork was not prepared</strong>
          <span>{String(provisioning?.error ?? "The Branch exists, but it has no independent Codex thread.")}</span>
          {repairFork.error && <small>{repairFork.error.message}</small>}
        </div>
        <Button
          size="small"
          appearance="primary"
          disabled={repairFork.isPending}
          onClick={() => repairFork.mutate()}
        >
          Repair fork
        </Button>
      </div>}

      <nav className="inspector-tabs" role="tablist" aria-label="Branch details">
        {(["results", "brief", "goals", "jobs", "memory", "activity"] as InspectorTab[]).map((item) => (
          <button key={item} role="tab" aria-selected={tab === item} className={tab === item ? "active" : ""} onClick={() => { setTab(item); onTabChange(item); }}>
            {item}
          </button>
        ))}
      </nav>

      {tab === "results" && (
        <div className="conversation-pane">
          <CodexSessionBar
            branchId={branchId}
            threadId={data.branch.codex_thread_id}
            runtime={agentRuntime}
          />
          <SessionWorkflowPanel key={branchId} branchId={branchId} />
          <div className="results-viewbar">
            <TabList
              size="small"
              selectedValue={resultsView}
              onTabSelect={(_event, value) => changeResultsView(value.value as ResultsView)}
              aria-label={`${runtimeLabel(agentRuntime)} output view`}
            >
              <Tab value="results">Results</Tab>
              <Tab value="live">
                <span className="results-tab-label">Live activity{webTurnRunning && <span className="live-status-dot" aria-hidden="true" />}</span>
              </Tab>
            </TabList>
            <div className="results-view-actions">
              <span className={`results-view-state ${webTurnRunning ? "running" : ""}`}>
                {webTurnRunning && <span className="live-status-dot" aria-hidden="true" />}
                {webTurnRunning
                  ? "Web turn running"
                  : formatResultHistorySummary(turnResults.length, incompleteQueries)}
              </span>
              {webTurnRunning && conversationState.can_interrupt && pendingWebMessage && (
                <InterruptWebTurnButton branchId={branchId} messageId={pendingWebMessage.id} />
              )}
            </div>
          </div>
          {resultsView === "results" ? (
            <div className="turn-results">
              {webTurnRunning && (
                <CurrentTurnProgress
                  branchId={branchId}
                  update={currentTurnUpdate}
                  onOpenLive={() => changeResultsView("live")}
                />
              )}
              {!resultHistory.length && !webTurnRunning && (
                <div className="results-empty">
                  <strong>No completed {runtimeLabel(agentRuntime)} turns yet</strong>
                  <span>{agentRuntime === "pi"
                    ? "Send one Web turn below. Pi's final answer appears here after the complete run settles."
                    : `Use the local ${runtimeLabel(agentRuntime)} terminal for the full workflow, or send one Web turn below. Final answers appear here automatically.`}</span>
                </div>
              )}
              {visibleResultHistory.map((entry, index) => (
                entry.kind === "turn"
                  ? <TurnResult key={entry.id} turn={entry.turn} latest={index === 0} currentThreadId={data.branch.codex_thread_id} />
                  : <IncompleteQuery
                      key={entry.id}
                      message={entry.message}
                      updates={assistantUpdatesForQuery(data.messages, entry.message)}
                      latest={index === 0 || entry.id === newestIncompleteQuery?.id}
                      onUseAsDraft={(message) => {
                        setComposerDraft({
                          requestId: `${message.id}:${Date.now()}`,
                          content: message.content,
                        });
                        changeResultsView("results");
                        setActionNotice("The failed query was restored as a draft. Review it before sending again.");
                      }}
                    />
              ))}
              {hiddenHistoryCount > 0 && (
                <div className="turn-history-more">
                  <Button
                    size="small"
                    appearance="subtle"
                    onClick={() => setVisibleHistoryCount((current) => current + TURN_PAGE_SIZE)}
                  >
                    Show earlier turns
                  </Button>
                  <span>{hiddenHistoryCount} not shown</span>
                </div>
              )}
            </div>
          ) : (
            <LiveActivity
              branchId={branchId}
              messages={data.messages}
              pendingMessage={pendingWebMessage}
              drafts={Object.values(liveDrafts)}
              state={conversationState}
            />
          )}
          {runtimeState?.runtime === "pi" && runtimeState.state === "running"
            ? <PiRuntimeControls branchId={branchId} runtimeState={runtimeState} />
            : <WebTurnComposer
                branchId={branchId}
                runtime={agentRuntime}
                state={conversationState}
                pendingMessage={pendingWebMessage}
                onTurnStarted={() => changeResultsView("live")}
                draft={composerDraft}
              />}
        </div>
      )}

      {tab === "brief" && <BriefTab detail={data} program={program} />}
      {tab === "goals" && <div className="inspector-section goal-stack"><GoalsTab
        state={nativeGoalState}
        loading={nativeGoalQuery.isLoading}
        onCreate={() => setGoalOpen(true)}
        onControl={(action) => controlGoal.mutate(action)}
        pending={controlGoal.isPending}
        error={controlGoal.error?.message || nativeGoalQuery.error?.message}
      /><GoalHistory key={branchId} branchId={branchId} legacyGoals={data.goals} /></div>}
      {tab === "jobs" && <JobsTab entries={data.external_jobs} workItemId={data.work_item.id} branchId={branchId} />}
      {tab === "memory" && <MemoryTab entries={data.memory} />}
      {tab === "activity" && <ActivityTab
        entries={data.activity}
        onOpenResults={() => { setTab("results"); onTabChange("results"); }}
      />}
      {data.session && <HandoffDialog branchId={branchId} session={data.session} latestTurn={turnResults[0]} jobs={data.external_jobs} acceptance={data.work_item.acceptance_criteria} open={handoffOpen} onClose={() => setHandoffOpen(false)} onSubmitted={setActionNotice} />}
      {!isProgramDesk && <ResultDialog
        branchId={branchId}
        hasParent={Boolean(data.branch.parent_branch_id)}
        open={resultOpen}
        onClose={() => setResultOpen(false)}
        onSubmitted={setActionNotice}
      />}
      <MemoryDialog
        branchId={branchId}
        programCode={program.code}
        programId={program.id}
        programDesk={isProgramDesk}
        open={memoryOpen}
        onClose={() => setMemoryOpen(false)}
      />
      <ClaudeForkDialog
        branchId={branchId}
        open={claudeForkOpen}
        submitting={continueWithClaude.isPending}
        error={continueWithClaude.error?.message}
        onClose={() => setClaudeForkOpen(false)}
        onSubmit={(input) => continueWithClaude.mutate(input)}
      />
      {!isProgramDesk && <WorkStateDialog workItem={data.work_item} programCode={program.code} branchId={branchId} open={workStateOpen} onClose={() => setWorkStateOpen(false)} />}
      {!isProgramDesk && <GoalDialog
        open={goalOpen}
        initialObjective={visibleGoal?.status === "complete" ? "" : visibleGoal?.objective || stringValue(data.branch.context_snapshot.fork_objective) || data.work_item.objective}
        initialTokenBudget={visibleGoal?.status === "complete" ? null : visibleGoal?.token_budget}
        editing={Boolean(visibleGoal && visibleGoal.status !== "complete")}
        submitting={createGoal.isPending}
        error={createGoal.error?.message}
        onClose={() => setGoalOpen(false)}
        onSubmit={(input) => createGoal.mutate(input)}
      />}
    </div>
  );
}

function LiveActivity({
  branchId,
  messages,
  pendingMessage,
  drafts,
  state,
}: {
  branchId: string;
  messages: ConversationMessage[];
  pendingMessage?: ConversationMessage;
  drafts: LiveDraft[];
  state: ConversationState;
}) {
  const [follow, setFollow] = useState(true);
  const [showToolActivity, setShowToolActivity] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const persisted = useMemo(
    () => selectLiveMessages(messages, pendingMessage),
    [messages, pendingMessage],
  );
  const currentDrafts = useMemo(
    () => selectLiveDrafts(drafts, pendingMessage),
    [drafts, pendingMessage],
  );
  const allEntries = useMemo(() => {
    const completedItems = new Set(persisted.map((message) => message.external_item_id).filter(Boolean));
    const combined: LiveFeedEntry[] = persisted.map((message) => ({
      id: message.id,
      role: message.role === "assistant" ? "assistant" : message.role === "system" ? "system" : "tool",
      kind: message.kind,
      content: message.content,
      createdAt: message.created_at,
      metadata: message.metadata_json,
      streaming: false,
    }));
    for (const draft of currentDrafts) {
      if (draft.externalItemId && completedItems.has(draft.externalItemId)) continue;
      if (!draft.content.trim() && draft.role === "assistant") continue;
      combined.push({
        id: draft.id,
        role: draft.role,
        kind: draft.kind,
        content: draft.content,
        createdAt: draft.createdAt,
        metadata: draft.metadata,
        streaming: draft.streaming,
      });
    }
    return combined.sort((left, right) => timestampMillis(left.createdAt) - timestampMillis(right.createdAt));
  }, [currentDrafts, persisted]);
  const entries = useMemo(() => allEntries
    .filter((entry) => (
      showToolActivity
      || entry.role !== "tool"
      || entry.metadata.source !== "local_cli"
      || entry.metadata.status === "failed"
    ))
    .slice(-MAX_LIVE_ENTRIES), [allEntries, showToolActivity]);
  const feedVersion = entries.map((entry) => `${entry.id}:${entry.content.length}`).join("|");
  const webRunning = state.web_turn_status === "running";
  const running = webRunning || state.local_turn_active;

  useEffect(() => {
    if (!follow || !feedRef.current) return;
    feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [feedVersion, follow, running]);

  const scopeLabel = pendingMessage
    ? "Current Web turn"
    : state.local_turn_active
      ? "Current local CLI turn"
      : entries.length ? "Latest recorded turn" : "Activity stream";

  return <div className="live-activity-pane">
    <div className="live-activity-toolbar">
      <div>
        <strong>{scopeLabel}</strong>
        <span>{entries.length} shown{allEntries.length !== entries.length ? ` · ${allEntries.length} captured` : ""}</span>
      </div>
      <div className="live-activity-controls">
        <Switch
          checked={showToolActivity}
          label="Tool activity"
          onChange={(_event, data) => setShowToolActivity(data.checked)}
        />
        <Switch
          checked={follow}
          label="Follow live"
          onChange={(_event, data) => setFollow(data.checked)}
        />
      </div>
    </div>
    <div
      className="live-activity-feed"
      ref={feedRef}
      onScroll={(event) => {
        if (!follow) return;
        const node = event.currentTarget;
        if (node.scrollHeight - node.scrollTop - node.clientHeight > 80) setFollow(false);
      }}
    >
      {!entries.length && (
        <div className="live-activity-empty">
          <strong>{running ? "Waiting for the first update" : "No activity recorded for this turn"}</strong>
          <span>{state.local_turn_active ? "Visible updates from the local CLI will appear here." : "Completed model updates and tool calls will appear here."}</span>
        </div>
      )}
      {entries.map((entry) => <LiveActivityEntry key={entry.id} branchId={branchId} entry={entry} />)}
      {running && <div className={`live-working ${state.local_turn_active ? "local" : ""}`} role="status">
        <span className="live-working-pulse" aria-hidden="true" />
        <div>
          <strong>{state.local_turn_active ? "Local CLI live" : "Codex is working"}</strong>
          <span>{state.local_turn_active ? "The terminal remains the control surface; visible updates are mirrored here." : "New updates appear here as the turn progresses."}</span>
        </div>
      </div>}
    </div>
    <span className="sr-only" aria-live="polite">{entries.length} activity updates</span>
  </div>;
}

function CurrentTurnProgress({
  branchId,
  update,
  onOpenLive,
}: {
  branchId: string;
  update?: LiveFeedEntry;
  onOpenLive: () => void;
}) {
  return <section className="current-turn-progress" aria-live="polite">
    <header>
      <span className="live-status-dot" aria-hidden="true" />
      <div>
        <strong>Current turn progress</strong>
        <span>{update ? `Latest update · ${formatTime(update.createdAt)}` : "Waiting for the first visible update"}</span>
      </div>
      <Button
        size="small"
        appearance="subtle"
        icon={<Open20Regular />}
        onClick={onOpenLive}
      >
        Open live activity
      </Button>
    </header>
    {update
      ? <div className="current-turn-progress-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents(branchId)}>
            {update.content}
          </ReactMarkdown>
        </div>
      : <p>Codex is working. Intermediate updates will appear here without waiting for the final result.</p>}
  </section>;
}

function latestAssistantUpdate(
  messages: ConversationMessage[],
  pendingMessage: ConversationMessage,
  drafts: LiveDraft[],
): LiveFeedEntry | undefined {
  const persisted: LiveFeedEntry[] = selectLiveMessages(messages, pendingMessage)
    .filter((message) => message.role === "assistant")
    .map((message) => ({
      id: message.id,
      role: "assistant",
      kind: message.kind,
      content: message.content,
      createdAt: message.created_at,
      metadata: message.metadata_json,
      streaming: false,
    }));
  const streaming: LiveFeedEntry[] = selectLiveDrafts(drafts, pendingMessage)
    .filter((draft) => draft.role === "assistant" && draft.content.trim())
    .map((draft) => ({
      id: draft.id,
      role: "assistant",
      kind: draft.kind,
      content: draft.content,
      createdAt: draft.createdAt,
      metadata: draft.metadata,
      streaming: draft.streaming,
    }));
  return [...persisted, ...streaming]
    .sort((left, right) => timestampMillis(left.createdAt) - timestampMillis(right.createdAt))
    .at(-1);
}

function LiveActivityEntry({ branchId, entry }: { branchId: string; entry: LiveFeedEntry }) {
  const time = formatTime(entry.createdAt);
  if (entry.role === "assistant") {
    return <article className={`live-event live-assistant ${entry.streaming ? "streaming" : ""}`}>
      <header>
        <span className="live-event-marker" aria-hidden="true" />
        <strong>Codex update</strong>
        <time dateTime={entry.createdAt}>{time}</time>
      </header>
      <div className="live-assistant-content">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents(branchId)}>
          {entry.content}
        </ReactMarkdown>
      </div>
    </article>;
  }

  if (entry.role === "system") {
    return <article className="live-event live-system">
      <header>
        <span className="live-event-marker" aria-hidden="true" />
        <strong>Codex error</strong>
        <time dateTime={entry.createdAt}>{time}</time>
      </header>
      <p>{entry.content}</p>
    </article>;
  }

  const label = entry.kind === "file_change" ? "File change" : entry.kind === "tool" ? "Tool" : "Command";
  return <details className={`live-event live-tool ${entry.streaming ? "streaming" : ""}`}>
    <summary>
      <span className="live-event-marker" aria-hidden="true" />
      <span className="live-tool-summary">
        <strong>{label}{entry.streaming ? " running" : toolStatus(entry.metadata)}</strong>
        <code>{compactToolContent(entry.content)}</code>
      </span>
      <time dateTime={entry.createdAt}>{time}</time>
    </summary>
    <pre>{entry.content}</pre>
  </details>;
}

function readResultsView(): ResultsView {
  try {
    return window.localStorage.getItem(RESULTS_VIEW_STORAGE_KEY) === "live" ? "live" : "results";
  } catch {
    return "results";
  }
}

function selectLiveMessages(
  messages: ConversationMessage[],
  pendingMessage?: ConversationMessage,
): ConversationMessage[] {
  const candidates = messages
    .filter((message) => (
      ["assistant", "tool", "system"].includes(message.role)
      && message.kind !== "turn_result"
    ))
    .sort((left, right) => timestampMillis(left.created_at) - timestampMillis(right.created_at));

  if (pendingMessage?.turn_id) {
    return candidates.filter((message) => message.turn_id === pendingMessage.turn_id);
  }
  if (pendingMessage) {
    const startedAt = timestampMillis(pendingMessage.created_at);
    return candidates.filter((message) => timestampMillis(message.created_at) >= startedAt);
  }
  const latestTurnId = candidates.slice().reverse().find((message) => message.turn_id)?.turn_id;
  return latestTurnId
    ? candidates.filter((message) => message.turn_id === latestTurnId)
    : candidates.slice(-30);
}

function selectLiveDrafts(
  drafts: LiveDraft[],
  pendingMessage?: ConversationMessage,
): LiveDraft[] {
  if (!pendingMessage) return drafts;

  const startedAt = timestampMillis(pendingMessage.created_at);
  if (pendingMessage.turn_id) {
    return drafts.filter((draft) => (
      draft.turnId === pendingMessage.turn_id
      // Official `codex exec --json` creates a transport turn ID before the
      // session JSONL records its own durable turn ID. The local JSONL mirror
      // is nevertheless scoped to one active turn and carries its source.
      // Admit only that fresh, explicitly tagged mirror; never use this as a
      // fallback for arbitrary stale WebSocket drafts.
      || (
        draft.metadata.source === "local_cli"
        && Boolean(draft.turnId)
        && timestampMillis(draft.createdAt) >= startedAt
      )
      // Some transport-level errors are not associated with an item. Keep a
      // newly-created error visible, while still excluding old error drafts.
      || (draft.role === "system" && !draft.turnId && timestampMillis(draft.createdAt) >= startedAt)
    ));
  }

  return drafts.filter((draft) => timestampMillis(draft.createdAt) >= startedAt);
}

function liveDraftId(turnId: string | undefined, itemId: string | undefined, role: string): string {
  return `${turnId ?? "turn"}:${itemId ?? role}`;
}

function appendLiveDraft(
  current: Record<string, LiveDraft>,
  update: {
    turnId?: string;
    externalItemId?: string;
    role: "assistant" | "tool";
    kind: string;
    delta: string;
  },
): Record<string, LiveDraft> {
  const id = liveDraftId(update.turnId, update.externalItemId, update.role);
  const existing = current[id];
  const content = `${existing?.content ?? ""}${update.delta}`;
  const limit = update.role === "tool" ? 16_000 : 60_000;
  return {
    ...current,
    [id]: {
      id,
      turnId: update.turnId,
      externalItemId: update.externalItemId,
      role: update.role,
      kind: update.kind,
      content: content.length > limit ? content.slice(-limit) : content,
      createdAt: existing?.createdAt ?? new Date().toISOString(),
      metadata: existing?.metadata ?? {},
      streaming: true,
    },
  };
}

function startedItemDraft(item: Record<string, unknown>, turnId?: string): LiveDraft | undefined {
  const itemType = stringValue(item.type);
  const externalItemId = stringValue(item.id);
  let role: LiveDraft["role"];
  let kind: string;
  let content = "";
  if (itemType === "agentMessage") {
    role = "assistant";
    kind = "message";
    content = stringValue(item.text) ?? "";
  } else if (itemType === "commandExecution") {
    role = "tool";
    kind = "command";
    const command = stringValue(item.command) ?? "Command started";
    content = `$ ${command}\n`;
  } else if (itemType === "fileChange") {
    role = "tool";
    kind = "file_change";
    content = "File change started";
  } else {
    return undefined;
  }
  const id = liveDraftId(turnId, externalItemId, role);
  return {
    id,
    turnId,
    externalItemId,
    role,
    kind,
    content,
    createdAt: new Date().toISOString(),
    metadata: {},
    streaming: true,
  };
}

function upsertLocalLiveEvents(
  current: Record<string, LiveDraft>,
  values: unknown[],
): Record<string, LiveDraft> {
  const next = { ...current };
  for (const value of values) {
    const event = objectValue(value);
    const id = stringValue(event.id);
    const role = stringValue(event.role);
    const kind = stringValue(event.kind);
    const content = stringValue(event.content);
    const createdAt = stringValue(event.created_at);
    if (
      !id
      || !kind
      || content === undefined
      || !createdAt
      || !role
      || !["assistant", "tool", "system"].includes(role)
    ) continue;
    const existing = next[id];
    const status = stringValue(event.status);
    next[id] = {
      id,
      turnId: stringValue(event.turn_id),
      externalItemId: id,
      role: role as LiveDraft["role"],
      kind,
      content,
      createdAt: existing?.createdAt ?? createdAt,
      metadata: { status, source: "local_cli" },
      streaming: status === "running",
    };
  }
  const entries = Object.entries(next);
  return entries.length <= MAX_LIVE_ENTRIES
    ? next
    : Object.fromEntries(entries.slice(-MAX_LIVE_ENTRIES));
}

function finishTurnDrafts(
  current: Record<string, LiveDraft>,
  turnId: string | undefined,
  status: string,
): Record<string, LiveDraft> {
  if (!turnId) return current;
  return Object.fromEntries(Object.entries(current).map(([id, draft]) => [
    id,
    draft.turnId === turnId
      ? {
          ...draft,
          streaming: false,
          metadata: {
            ...draft.metadata,
            status: stringValue(draft.metadata.status) === "running" ? status : draft.metadata.status,
          },
        }
      : draft,
  ]));
}

function removeLiveDraft(current: Record<string, LiveDraft>, externalItemId: string): Record<string, LiveDraft> {
  const remaining = Object.fromEntries(
    Object.entries(current).filter(([, draft]) => draft.externalItemId !== externalItemId),
  );
  return Object.keys(remaining).length === Object.keys(current).length ? current : remaining;
}

function removeTurnDrafts(current: Record<string, LiveDraft>, turnId: string): Record<string, LiveDraft> {
  return Object.fromEntries(Object.entries(current).filter(([, draft]) => draft.turnId !== turnId));
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function isConversationState(value: unknown): value is ConversationState {
  const state = objectValue(value);
  return typeof state.can_send === "boolean"
    && typeof state.local_turn_active === "boolean"
    && typeof state.web_turn_status === "string";
}

function codexErrorText(value: unknown): string {
  if (typeof value === "string") return value;
  const object = objectValue(value);
  if (typeof object.message === "string") return object.message;
  if (object.error !== undefined) return codexErrorText(object.error);
  return "Codex reported an error. Open Activity for details.";
}

function compactToolContent(content: string): string {
  const firstLine = content.split("\n", 1)[0]?.trim() || "Tool activity";
  return firstLine.length > 110 ? `${firstLine.slice(0, 107)}...` : firstLine;
}

function toolStatus(metadata: Record<string, unknown>): string {
  const status = stringValue(metadata.status);
  return status ? ` · ${status}` : "";
}

function InterruptWebTurnButton({ branchId, messageId }: { branchId: string; messageId: string }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => api.interruptWebTurn(branchId, messageId),
    onSuccess: (message) => {
      queryClient.setQueryData<BranchDetail>(
        ["branch", branchId],
        (current) => upsertConversationMessage(current, message),
      );
      queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
    },
  });
  return <div className="interrupt-web-turn">
    <Tooltip content="Stop the current Web turn; its completed updates remain available" relationship="description">
      <Button
        size="small"
        appearance="subtle"
        icon={<Dismiss20Regular />}
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? "Stopping" : "Stop"}
      </Button>
    </Tooltip>
    {mutation.error && <span role="alert">{mutation.error.message}</span>}
  </div>;
}

function PiRuntimeControls({
  branchId,
  runtimeState,
}: {
  branchId: string;
  runtimeState?: RuntimeState;
}) {
  const queryClient = useQueryClient();
  const [content, setContent] = useState("");
  const mutation = useMutation({
    mutationFn: ({ command, message }: { command: "steer" | "follow_up" | "abort"; message?: string }) => {
      if (command === "steer") return api.steerRuntime(branchId, message ?? "");
      if (command === "follow_up") return api.followUpRuntime(branchId, message ?? "");
      return api.abortRuntime(branchId);
    },
    onSuccess: (_result, variables) => {
      if (variables.command !== "abort") setContent("");
      queryClient.invalidateQueries({ queryKey: ["runtime-state", branchId] });
      queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
    },
  });
  if (runtimeState?.runtime !== "pi" || runtimeState.state !== "running") return null;
  const queueCount = (runtimeState.queue.steering?.length ?? 0)
    + (runtimeState.queue.follow_up?.length ?? 0);
  const message = content;

  return <div className="pi-runtime-controls" aria-label="Pi runtime controls">
    <div className="pi-runtime-controls-header">
      <span><span className="live-status-dot" aria-hidden="true" />Pi running</span>
      {queueCount > 0 && <small>{queueCount} queued</small>}
    </div>
    <div className="pi-runtime-controls-composer">
      <Textarea
        aria-label="Pi control message"
        placeholder="Message to Pi"
        resize="vertical"
        value={content}
        disabled={mutation.isPending}
        onChange={(_event, data) => setContent(data.value)}
      />
      <div className="pi-runtime-control-actions">
        <Tooltip content="Deliver after the current tool batch" relationship="description">
          <Button
            size="small"
            appearance="primary"
            icon={<Send20Regular />}
            disabled={!message.trim() || mutation.isPending}
            onClick={() => mutation.mutate({ command: "steer", message })}
          >
            Steer now
          </Button>
        </Tooltip>
        <Tooltip content="Run after Pi finishes its current work" relationship="description">
          <Button
            size="small"
            appearance="secondary"
            icon={<Clock20Regular />}
            disabled={!message.trim() || mutation.isPending}
            onClick={() => mutation.mutate({ command: "follow_up", message })}
          >
            Queue next
          </Button>
        </Tooltip>
        <Tooltip content="Abort the active Pi run" relationship="description">
          <Button
            size="small"
            appearance="subtle"
            icon={<Dismiss20Regular />}
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ command: "abort" })}
          >
            Abort
          </Button>
        </Tooltip>
      </div>
    </div>
    {mutation.error && <span className="pi-runtime-control-error" role="alert">
      {mutation.error.message}
    </span>}
  </div>;
}

function WebTurnComposer({
  branchId,
  runtime,
  state,
  pendingMessage,
  onTurnStarted,
  draft,
}: {
  branchId: string;
  runtime: AgentRuntime;
  state: ConversationState;
  pendingMessage?: ConversationMessage;
  onTurnStarted: () => void;
  draft?: ComposerDraft;
}) {
  const queryClient = useQueryClient();
  const [content, setContent] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const appliedDraftRequest = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!draft || appliedDraftRequest.current === draft.requestId) return;
    appliedDraftRequest.current = draft.requestId;
    setContent(draft.content);
    inputRef.current?.focus();
  }, [draft]);
  const mutation = useMutation({
    mutationFn: async (message: string) => {
      const superseding = state.web_turn_status === "running" && Boolean(pendingMessage);
      if (superseding && pendingMessage) {
        await api.interruptWebTurn(branchId, pendingMessage.id);
      }
      return sendWebMessageAfterInterrupt(branchId, message, superseding);
    },
    onMutate: async () => {
      onTurnStarted();
      setContent("");
      queryClient.setQueryData<BranchDetail>(["branch", branchId], (current) => (
        current
          ? {
            ...current,
            conversation_state: {
              can_send: false,
              web_turn_status: "running",
              local_turn_active: false,
              reason: "Wait for the current Web turn to finish",
            },
          }
          : current
      ));
      queryClient.setQueryData<ConversationState>(["conversation-state", branchId], {
        can_send: false,
        web_turn_status: "running",
        local_turn_active: false,
        reason: "Wait for the current Web turn to finish",
      });
    },
    onError: (_error, message) => {
      // A release race must never eat the user's draft. Restore it unless the
      // user has already started typing a replacement while the request ran.
      setContent((current) => current || message);
    },
    onSuccess: (message) => {
      // The request has been durably accepted even while the full Branch
      // transcript is still refetching. Reflect that acknowledgement at once
      // so a slow transcript cannot look like a dropped prompt.
      queryClient.setQueryData<BranchDetail>(
        ["branch", branchId],
        (current) => upsertConversationMessage(current, message),
      );
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      queryClient.invalidateQueries({ queryKey: ["conversation-state", branchId] });
    },
  });
  const webTurnRunning = mutation.isPending || state.web_turn_status === "running";
  const inputDisabled = !state.can_send && !state.local_turn_active && !webTurnRunning;
  const sendDisabled = (!state.can_send && !webTurnRunning) || !content.trim();
  const sendLabel = webTurnRunning ? "Interrupt & send" : "Send";
  const capacityRetry = pendingMessage
    ? capacityRetryInfo(pendingMessage.metadata_json.capacity_retry)
    : undefined;
  const transportRetry = pendingMessage
    ? capacityRetryInfo(pendingMessage.metadata_json.transport_retry)
    : undefined;
  const status = mutation.isPending
    ? "Web turn running. Stop it here or continue from the local CLI."
    : state.web_turn_status === "running"
      ? "Web turn running. Stop it here or continue from the local CLI."
    : state.local_turn_active
      ? "Local CLI turn running. You can draft here; Send unlocks when it finishes."
      : state.web_turn_status === "failed"
        ? "The previous Web turn failed. This Branch is ready for a new message."
      : state.web_turn_status === "interrupted"
        ? "The previous Web turn was interrupted by the local CLI."
        : state.reason;
  const announceStatus = mutation.isPending
    || Boolean(mutation.error)
    || state.local_turn_active
    || state.web_turn_status === "running"
    || state.web_turn_status === "failed"
    || state.web_turn_status === "interrupted";

  return <div className="web-turn-composer">
    <div className="composer-settings">
      {runtime === "codex" && <ExecutionControls key={branchId} branchId={branchId} />}
      {runtime === "claude" && <ClaudeExecutionControls key={branchId} branchId={branchId} />}
      <ScheduledCheckControls key={`check-${branchId}`} branchId={branchId} />
    </div>
    {pendingMessage && <div className="web-pending-prompt">
      <div>
        <strong>{pendingMessage.metadata_json.scheduled_check_id ? "Scheduled check" : "Submitted prompt"}</strong>
        <time dateTime={pendingMessage.created_at}>
          {formatTime(pendingMessage.created_at)}
        </time>
      </div>
      <p>{pendingMessage.content}</p>
      <ExecutionBadge value={pendingMessage.metadata_json.execution} />
      {capacityRetry && <small className="capacity-retry-status">
        Model capacity is full. Retrying automatically ({capacityRetry.attempt}/{capacityRetry.maxAttempts}) at {formatTime(capacityRetry.nextRetryAt)}.
      </small>}
      {transportRetry && <small className="capacity-retry-status">
        The model gateway disconnected before a tool action. Retrying safely ({transportRetry.attempt}/{transportRetry.maxAttempts}) at {formatTime(transportRetry.nextRetryAt)}.
      </small>}
    </div>}
    {(status || mutation.error) && (
      <div
        className={`web-turn-state ${mutation.error ? "error" : ""}`}
        role={announceStatus ? "status" : undefined}
      >
        {mutation.error?.message ?? status}
      </div>
    )}
    <div className="composer">
      <Textarea
        ref={inputRef}
        aria-label="Web turn message"
        placeholder="Send one Web turn"
        resize="vertical"
        value={content}
        disabled={inputDisabled}
        onChange={(_event, data) => setContent(data.value)}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !sendDisabled) {
            event.preventDefault();
            mutation.mutate(content);
          }
        }}
      />
      <Tooltip
        content={webTurnRunning
          ? "Stop the current Web turn and continue with this message"
          : state.reason || "Send a new Web turn"}
        relationship="description"
      >
        <span className="tooltip-button-wrap">
          <Button
            appearance="primary"
            icon={<Send20Regular />}
            aria-label={webTurnRunning
              ? "Interrupt current Web turn and send message"
              : "Send Web turn"}
            disabled={sendDisabled}
            onClick={() => mutation.mutate(content)}
          >
            {sendLabel}
          </Button>
        </span>
      </Tooltip>
    </div>
  </div>;
}

async function sendWebMessageAfterInterrupt(
  branchId: string,
  content: string,
  waitForRelease = false,
) {
  let lastError: unknown;
  // The interrupt endpoint returns after updating durable state; give the old
  // in-flight CLI request a moment to observe that state and exit its process.
  if (waitForRelease) {
    await new Promise((resolve) => window.setTimeout(resolve, 600));
  }
  // A registry scan in an older backend can take up to ten seconds to clear a
  // cached managed-lease window. Keep the handoff alive across that interval;
  // the server-side fix makes the normal path effectively immediate.
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      return await api.sendMessage(branchId, content);
    } catch (error) {
      lastError = error;
      const releaseStillPending = error instanceof Error
        && /(current Web turn|current local CLI turn|previous Web turn)/i.test(error.message);
      if (!waitForRelease || !releaseStillPending) {
        throw error;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 150));
    }
  }
  throw lastError instanceof Error
    ? lastError
    : new Error("The previous Web turn did not release in time");
}

function LocalCodexButton({ branchId, hasThread }: { branchId: string; hasThread: boolean }) {
  const [status, setStatus] = useState<"idle" | "copying" | "copied" | "failed">("idle");
  const copy = async () => {
    setStatus("copying");
    try {
      const { command } = await api.terminalCommand(branchId);
      await copyText(command);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
    window.setTimeout(() => setStatus("idle"), 2200);
  };
  const label = status === "copying"
    ? "Copying"
    : status === "copied"
      ? "Command copied"
      : status === "failed"
        ? "Copy failed"
        : hasThread ? "Continue locally" : "Start locally";
  return <Tooltip content="Copy the managed Research Ops terminal command" relationship="description">
    <Button size="small" appearance="primary" icon={status === "copied" ? <Checkmark20Regular /> : <Play20Regular />} disabled={status === "copying"} onClick={copy}>
      {label}
    </Button>
  </Tooltip>;
}

function IncompleteQuery({
  message,
  updates,
  latest,
  onUseAsDraft,
}: {
  message: ConversationMessage;
  updates: ConversationMessage[];
  latest: boolean;
  onUseAsDraft: (message: ConversationMessage) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(latest);
  const status = message.metadata_json.send_status === "failed" ? "failed" : "interrupted";
  const reason = typeof message.metadata_json.error === "string"
    ? message.metadata_json.error
    : status === "interrupted"
      ? "This query was interrupted before Codex produced a final answer."
      : "Codex did not produce a final answer for this query.";
  const latestUpdate = updates.at(-1);
  useEffect(() => setOpen(latest), [latest, message.id]);
  const copy = async () => {
    await copyText(message.content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };
  return (
    <details
      className={`turn-result incomplete-query ${status} ${latest ? "latest" : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <div>
          <strong>{status === "interrupted" ? "Interrupted query" : "Failed query"}</strong>
          <span>{compactPrompt(message.content)}</span>
        </div>
        <time>{formatQueryMeta(message)}</time>
        <Tooltip content="Copy this query" relationship="description">
          <Button
            size="small"
            appearance="subtle"
            aria-label="Copy incomplete query"
            icon={copied ? <Checkmark20Regular /> : <Copy20Regular />}
            onClick={(event) => { event.preventDefault(); copy(); }}
          />
        </Tooltip>
        <Tooltip content="Restore this query in the composer without sending it" relationship="description">
          <Button
            size="small"
            appearance="subtle"
            aria-label="Use failed query as draft"
            icon={<ArrowSync20Regular />}
            onClick={(event) => { event.preventDefault(); onUseAsDraft(message); }}
          >
            Use as draft
          </Button>
        </Tooltip>
      </summary>
      {open && <>
        <div className="turn-prompt"><span>Query</span>{message.content}</div>
        {latestUpdate && <div className="incomplete-query-progress">
          <div>
            <strong>{updates.length} update{updates.length === 1 ? "" : "s"} recorded before interruption</strong>
            <time dateTime={latestUpdate.created_at}>{formatTime(latestUpdate.created_at)}</time>
          </div>
          <div className="turn-result-content">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents(message.branch_id)}>
              {latestUpdate.content}
            </ReactMarkdown>
          </div>
        </div>}
        <div className="incomplete-query-note" role="note">
          <strong>No final Codex answer was recorded</strong>
          <span>{reason}</span>
          <span>{failedQueryRecoveryNote(reason)}</span>
        </div>
      </>}
    </details>
  );
}

function assistantUpdatesForQuery(
  messages: ConversationMessage[],
  query: ConversationMessage,
): ConversationMessage[] {
  if (!query.turn_id) return [];
  return messages
    .filter((message) => (
      message.turn_id === query.turn_id
      && message.role === "assistant"
      && message.kind !== "turn_result"
    ))
    .sort((left, right) => timestampMillis(left.created_at) - timestampMillis(right.created_at));
}

function TurnResult({ turn, latest, currentThreadId }: {
  turn: CodexTurn;
  latest: boolean;
  currentThreadId?: string | null;
}) {
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(latest);
  const goalContinuation = turn.metadata_json.prompt_kind === "goal_continuation";
  const scheduledCheck = Boolean(turn.metadata_json.scheduled_check_id);
  const historicalSession = Boolean(currentThreadId && turn.thread_id !== currentThreadId);
  const prompt = (goalContinuation ? turn.prompt?.replace(/^Continue objective: /, "") : turn.prompt)?.trim()
    || "Original query unavailable in this historical Codex record.";
  useEffect(() => setOpen(latest), [latest, turn.id]);
  const copy = async () => {
    await copyText(turn.final_answer);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };
  return (
    <details
      className={`turn-result ${latest ? "latest" : ""}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <div>
          <strong>{scheduledCheck ? "Scheduled check" : goalContinuation ? "Goal continuation" : latest ? "Latest Codex result" : "Codex turn result"}</strong>
          <span>{compactPrompt(prompt)}</span>
          {historicalSession && <span title={turn.thread_id}>Previous session · {turn.thread_id.slice(0, 8)}</span>}
        </div>
        <time>{formatTurnMeta(turn)}</time>
        <Tooltip content="Copy this final answer" relationship="description">
          <Button size="small" appearance="subtle" icon={copied ? <Checkmark20Regular /> : <Copy20Regular />} onClick={(event) => { event.preventDefault(); copy(); }} />
        </Tooltip>
      </summary>
      {open && <>
        <ExecutionBadge value={turn.metadata_json.execution} />
        <div className="turn-prompt"><span>{goalContinuation ? "Objective" : "Query"}</span>{prompt}</div>
        <div className="turn-result-content">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents(turn.branch_id)}>
            {turn.final_answer}
          </ReactMarkdown>
        </div>
      </>}
    </details>
  );
}

function MarkdownLink({ href = "", children, ...props }: ComponentPropsWithoutRef<"a">) {
  const localPath = localFilePath(href);
  if (localPath) return <LocalFileReference path={localPath} label={children} />;
  if (/^https?:\/\//i.test(href)) {
    return (
      <a {...props} className="external-result-link" href={href} target="_blank" rel="noreferrer">
        <span>{children}</span>
        <Open20Regular aria-hidden="true" />
      </a>
    );
  }
  return <a {...props} href={href}>{children}</a>;
}

function markdownComponents(branchId: string) {
  return {
    a: MarkdownLink,
    img: (props: ComponentPropsWithoutRef<"img">) => (
      <MarkdownImage {...props} branchId={branchId} />
    ),
  };
}

function MarkdownImage({
  branchId,
  src = "",
  alt = "",
  node,
  ...props
}: ComponentPropsWithoutRef<"img"> & { branchId: string; node?: unknown }) {
  void node;
  const [failed, setFailed] = useState(false);
  const localPath = localImagePath(src);
  if (!localPath) {
    return <img {...props} className="result-image" src={src} alt={alt} loading="lazy" decoding="async" />;
  }
  if (failed) {
    return <span className="result-image-error" role="note">
      <span>Image preview unavailable</span>
      <LocalFileReference path={localPath} label={alt || "Image"} />
    </span>;
  }
  const query = new URLSearchParams({ path: localPath }).toString();
  const url = `/api/branches/${encodeURIComponent(branchId)}/result-file?${query}`;
  return <span className="result-image-frame">
    <a href={url} target="_blank" rel="noreferrer" title="Open image at full size">
      <img
        {...props}
        className="result-image"
        src={url}
        alt={alt}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
      />
    </a>
    {alt && <span className="result-image-caption">{alt}</span>}
  </span>;
}

function LocalFileReference({ path, label }: { path: string; label: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await copyText(path);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };
  return (
    <span className="local-file-reference">
      <span className="local-file-label">{label}</span>
      <code title={path}>{path}</code>
      <Tooltip content="Copy the local file path" relationship="description">
        <Button
          size="small"
          appearance="subtle"
          icon={copied ? <Checkmark20Regular /> : <Copy20Regular />}
          aria-label={copied ? `Copied path ${path}` : `Copy path ${path}`}
          onClick={copy}
        />
      </Tooltip>
    </span>
  );
}

function localFilePath(href: string): string | undefined {
  let value = href;
  if (/^file:\/\//i.test(value)) value = value.replace(/^file:\/\//i, "");
  try {
    value = decodeURIComponent(value);
  } catch {
    // Keep the original path if a producer emitted malformed percent encoding.
  }
  return /^(?:\/mnt\/|\/home\/|\/tmp\/|\/workspace\/|\.\.?\/|[A-Za-z]:[\\/])/.test(value)
    ? value
    : undefined;
}

function localImagePath(src: string): string | undefined {
  const explicit = localFilePath(src);
  if (explicit) return explicit;
  return !/^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(src) ? src : undefined;
}

function compactPrompt(prompt: string): string {
  const normalized = prompt.replace(/<research_ops_brief>[\s\S]*?<\/research_ops_brief>/, "Research Ops brief").replace(/\s+/g, " ").trim();
  return normalized.length > 96 ? `${normalized.slice(0, 93)}...` : normalized;
}

function formatTurnMeta(turn: CodexTurn): string {
  const time = formatDateTime(turn.completed_at, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  if (!turn.duration_ms) return time;
  const minutes = Math.floor(turn.duration_ms / 60_000);
  const seconds = Math.round((turn.duration_ms % 60_000) / 1000);
  return `${time} · ${minutes ? `${minutes}m ` : ""}${seconds}s`;
}

function formatQueryMeta(message: ConversationMessage): string {
  return formatDateTime(message.created_at, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isIncompleteWebQuery(message: ConversationMessage): boolean {
  const status = message.metadata_json.send_status;
  return message.role === "user"
    && ["web", "continuation", "goal"].includes(String(message.metadata_json.origin))
    && (status === "interrupted" || status === "failed");
}

function failedQueryRecoveryNote(reason: string): string {
  if (/request blocked|stream disconnected|websocket closed/i.test(reason)) {
    return "The model gateway did not settle this turn. The Branch is unlocked; the query was not replayed automatically because its remote execution state is unknown.";
  }
  return "The Branch is unlocked. Use this query as a draft if you want to review and send it again.";
}

function isGatewayDisconnect(reason: string): boolean {
  return /request blocked|stream disconnected|websocket closed|concurrency limit exceeded/i.test(reason);
}

function capacityRetryInfo(value: unknown): {
  attempt: number;
  maxAttempts: number;
  nextRetryAt: string;
} | undefined {
  if (!value || typeof value !== "object") return undefined;
  const retry = value as Record<string, unknown>;
  const attempt = retry.attempt;
  const maxAttempts = retry.max_attempts;
  const nextRetryAt = retry.next_retry_at;
  if (
    typeof attempt !== "number"
    || typeof maxAttempts !== "number"
    || typeof nextRetryAt !== "string"
  ) return undefined;
  return { attempt, maxAttempts, nextRetryAt };
}

function formatResultHistorySummary(
  completedCount: number,
  incompleteQueries: ConversationMessage[],
): string {
  const interruptedCount = incompleteQueries.filter(
    (message) => message.metadata_json.send_status === "interrupted",
  ).length;
  const failedCount = incompleteQueries.length - interruptedCount;
  const parts = [`${completedCount} completed`];
  if (interruptedCount) parts.push(`${interruptedCount} interrupted`);
  if (failedCount) parts.push(`${failedCount} failed`);
  return parts.join(" · ");
}

function CodexSessionBar({
  branchId,
  threadId,
  runtime = "codex",
}: {
  branchId: string;
  threadId?: string;
  runtime?: AgentRuntime;
}) {
  const label = runtimeLabel(runtime);
  const resumableThreadId = threadId && !threadId.startsWith("deferred:") ? threadId : undefined;
  const [status, setStatus] = useState<"idle" | "id" | "command" | "failed">("idle");
  const copy = async (kind: "id" | "command") => {
    try {
      if (kind === "id") {
        if (!resumableThreadId) return;
        await copyText(resumableThreadId);
      } else {
        const { codex_command, command } = await api.terminalCommand(branchId);
        await copyText(codex_command ?? command);
      }
      setStatus(kind);
    } catch {
      setStatus("failed");
    }
    window.setTimeout(() => setStatus("idle"), 2000);
  };

  if (!resumableThreadId) {
    return (
      <div className="codex-session-bar pending">
        <span>{label} session</span>
        <small>Available after the first {label} turn starts</small>
      </div>
    );
  }

  return (
    <div className="codex-session-bar">
      <span>{label} session ID</span>
      <code title={resumableThreadId}>{resumableThreadId}</code>
      <Tooltip content={`Copy only the ${label} session ID`} relationship="description">
        <Button
          size="small"
          appearance="subtle"
          icon={status === "id" ? <Checkmark20Regular /> : <Copy20Regular />}
          aria-label={status === "id" ? "Session ID copied" : "Copy session ID"}
          onClick={() => copy("id")}
        />
      </Tooltip>
      {runtime !== "pi" && <Tooltip content={`Advanced: copy the raw ${label} resume command without the managed Research Ops wrapper`} relationship="description">
          <Button
            size="small"
            appearance="subtle"
            icon={status === "command" ? <Checkmark20Regular /> : <Copy20Regular />}
            aria-label={status === "command" ? "Raw command copied" : "Copy raw resume command"}
            onClick={() => copy("command")}
          />
        </Tooltip>}
      {status === "failed" && <small className="copy-error">Copy failed</small>}
    </div>
  );
}

function GoalsTab({
  state,
  loading,
  onCreate,
  onControl,
  pending,
  error,
}: {
  state?: NativeGoalState;
  loading: boolean;
  onCreate: () => void;
  onControl: (action: "pause" | "resume" | "complete" | "clear") => void;
  pending: boolean;
  error?: string;
}) {
  if (loading && !state) {
    return <div className="inspector-loading">Reading Codex Native Goal</div>;
  }
  if (state && !state.supported) {
    return <div className="inspector-empty goal-empty">
      <Flag20Regular />
      <strong>Native Goal unavailable</strong>
      <span>{state.reason}</span>
    </div>;
  }
  if (!state?.goal) {
    return <div className="inspector-empty goal-empty">
      <Flag20Regular />
      <strong>No Codex Native Goal</strong>
      {error && <div className="inline-error" role="alert">{error}</div>}
      <Button appearance="primary" size="small" icon={<Flag20Regular />} onClick={onCreate}>Set native goal</Button>
    </div>;
  }
  const completed = state.goal.status === "complete";
  return <>
    <div className="goal-toolbar">
      <div>
        <strong>{completed ? "No active goal" : "Current goal"}</strong>
        <span>{completed ? "Latest goal completed" : `Codex Session ${state.thread_id}`}</span>
      </div>
      <Button size="small" appearance={completed ? "primary" : "secondary"} icon={completed ? <Flag20Regular /> : <Edit20Regular />} onClick={onCreate}>{completed ? "New goal" : "Edit"}</Button>
    </div>
    {error && <div className="inline-error" role="alert">{error}</div>}
    {!completed && <GoalCard
      goal={state.goal}
      runtime={state.runtime}
      pending={pending}
      onControl={onControl}
      onEdit={onCreate}
    />}
  </>;
}

function GoalCard({
  goal,
  runtime,
  pending,
  onControl,
  onEdit,
}: {
  goal: NativeGoal;
  runtime?: NativeGoalState["runtime"];
  pending: boolean;
  onControl: (action: "pause" | "resume" | "complete" | "clear") => void;
  onEdit: () => void;
}) {
  const canPause = goal.status === "active" || (goal.status === "paused" && Boolean(runtime?.last_error));
  const canResume = ["paused", "blocked", "usageLimited", "budgetLimited"].includes(goal.status);
  const canReconnect = goal.status === "active" && ["error", "detached"].includes(runtime?.status || "");
  const canComplete = goal.status !== "complete";
  const budgetProgress = goal.token_budget
    ? Math.min(100, Math.round((goal.tokens_used / goal.token_budget) * 100))
    : undefined;
  return <article className={`goal-card goal-${goal.status}`}>
    <header>
      <div>
        <span className={`goal-status status-${goal.status}`}>{nativeGoalStatusLabel(goal.status)}</span>
        <h3>{goal.objective}</h3>
      </div>
      <span className="goal-mode">Codex-owned</span>
    </header>
    <dl className="goal-facts">
      <div><dt>Tokens used</dt><dd>{formatGoalTokens(goal.tokens_used)}</dd></div>
      <div><dt>Token budget</dt><dd>{goal.token_budget ? formatGoalTokens(goal.token_budget) : "No goal cap"}</dd></div>
      <div><dt>Elapsed</dt><dd>{formatGoalDuration(goal.time_used_seconds)}</dd></div>
      <div><dt>Updated</dt><dd>{formatDateTime(new Date(goal.updated_at * 1_000).toISOString())}</dd></div>
      <div><dt>Execution</dt><dd role="status">{nativeGoalExecutionLabel(runtime?.status)}</dd></div>
      <div><dt>Last checked</dt><dd>{runtime?.last_checked_at ? formatDateTime(runtime.last_checked_at) : "Not checked"}</dd></div>
      <div><dt>Last activity</dt><dd>{runtime?.last_activity_at ? formatDateTime(runtime.last_activity_at) : "No activity recorded"}</dd></div>
      <div><dt>Progress notices</dt><dd>{!runtime?.notifications_enabled ? "Notifications off" : runtime.supervised ? `Every ${Math.round(runtime.progress_interval_seconds / 60)} min` : "Supervision off"}</dd></div>
      <div><dt>Last notification</dt><dd>{runtime?.notification_status || "None"}{runtime?.notification_sent_at && ` · ${formatDateTime(runtime.notification_sent_at)}`}</dd></div>
      <div><dt>Recovery attempts</dt><dd>{runtime?.retry_attempts ?? 0}</dd></div>
      {runtime?.next_retry_at && <div><dt>Next retry</dt><dd>{formatDateTime(runtime.next_retry_at)}</dd></div>}
    </dl>
    {runtime?.last_summary && <p className="goal-runtime-detail">{runtime.last_summary}</p>}
    {runtime?.last_error && <div className="inline-error goal-runtime-detail" role="alert">{runtime.last_error}</div>}
    {runtime?.notification_error && <div className="inline-error goal-runtime-detail" role="alert">Notification failed: {runtime.notification_error}</div>}
    {budgetProgress !== undefined && <div className="goal-budget" aria-label={`${budgetProgress}% of Goal token budget used`}>
      <span style={{ width: `${budgetProgress}%` }} />
    </div>}
    <div className="goal-actions">
      {canPause && <Tooltip content="Pause this Goal and interrupt its current Codex turn" relationship="description"><Button size="small" appearance="subtle" icon={<Clock20Regular />} disabled={pending} onClick={() => onControl("pause")}>Pause</Button></Tooltip>}
      {canResume && <Tooltip content="Resume this native Goal on the same Codex thread" relationship="description"><Button size="small" appearance="primary" icon={<Play20Regular />} disabled={pending} onClick={() => onControl("resume")}>Resume</Button></Tooltip>}
      {canReconnect && <Tooltip content="Reconnect this native Goal using its unchanged objective, identity, and model" relationship="description"><Button size="small" appearance="primary" icon={<Play20Regular />} disabled={pending} onClick={() => onControl("resume")}>Reconnect</Button></Tooltip>}
      {goal.status !== "complete" && <Tooltip content="Edit the objective or replace its token cap" relationship="description"><Button size="small" appearance="subtle" icon={<Edit20Regular />} disabled={pending} onClick={onEdit}>Edit</Button></Tooltip>}
      {canComplete && <Tooltip content="Mark the native Goal complete" relationship="description"><Button size="small" appearance="subtle" icon={<Checkmark20Regular />} disabled={pending} onClick={() => onControl("complete")}>Complete</Button></Tooltip>}
      <Tooltip content="Remove the Goal from this Codex thread; conversation history is preserved" relationship="description"><Button size="small" appearance="subtle" icon={<Dismiss20Regular />} disabled={pending} onClick={() => onControl("clear")}>Clear</Button></Tooltip>
    </div>
  </article>;
}

function nativeGoalExecutionLabel(status?: string): string {
  return ({
    running: "Running", idle: "Waiting for next turn", detached: "Runtime disconnected",
    recovering: "Recovering", retrying: "Retry scheduled", error: "Execution failed",
    unavailable: "Status unavailable",
    external_owner: "Owned by another Codex process", paused: "Paused", blocked: "Blocked",
    usageLimited: "Usage limited", budgetLimited: "Budget limited", complete: "Complete",
    cleared: "Cleared", unknown: "Not verified",
  } as Record<string, string>)[status || "unknown"] || "Not verified";
}

function GoalRuntimeStatus({ goal, runtime }: { goal: NativeGoal; runtime?: NativeGoalState["runtime"] }) {
  return <span className="goal-runtime-summary" title={goal.objective}>
    Goal
    <strong className={`goal-status status-${goal.status}`}>{nativeGoalStatusLabel(goal.status)}</strong>
    <small className={`goal-execution-state execution-${runtime?.status || "unknown"}`} role="status" aria-label="Goal execution">Execution: {nativeGoalExecutionLabel(runtime?.status)}</small>
    <small>{formatGoalTokens(goal.tokens_used)} · {formatGoalDuration(goal.time_used_seconds)}</small>
  </span>;
}

function nativeGoalStatusLabel(status: NativeGoal["status"]): string {
  return {
    active: "Active",
    paused: "Paused",
    blocked: "Blocked",
    usageLimited: "Usage limited",
    budgetLimited: "Budget limited",
    complete: "Complete",
  }[status];
}

function formatGoalTokens(value: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatGoalDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function BriefTab({ detail, program }: { detail: Awaited<ReturnType<typeof api.branch>>; program: Program }) {
  const criteria = detail.work_item.acceptance_criteria;
  const workspacePath = detail.branch.workspace_path || detail.branch.cwd;
  const hasLegacySource = workspacePath !== detail.branch.cwd;
  const forkObjective = stringValue(detail.branch.context_snapshot.fork_objective);
  const objective = forkObjective || detail.work_item.objective || "Pending definition";
  const parentSnapshot = objectValue(detail.branch.context_snapshot.parent_snapshot);
  const parentConclusion = stringValue(parentSnapshot.conclusion);
  const parentTurnId = stringValue(parentSnapshot.source_turn_id);
  return (
    <div className="inspector-section">
      <section>
        <h3>Objective</h3>
        <p>{objective}</p>
      </section>
      {detail.branch.parent_branch_id && <section>
        <h3>Fork context</h3>
        <code>parent {detail.branch.parent_branch_id}</code>
        {parentTurnId && <code>source turn {parentTurnId}</code>}
        {parentConclusion && <details className="fork-handoff">
          <summary>Parent handoff</summary>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{parentConclusion}</ReactMarkdown>
        </details>}
      </section>}
      <section>
        <h3>Program outcome</h3>
        <p>{program.objective}</p>
      </section>
      <section>
        <h3>Acceptance</h3>
        {criteria.length ? criteria.map((item, index) => <div className="criterion" key={index}><Checkmark20Regular /><span>{String(item.description ?? item)}</span></div>) : <div className="muted-row">Explicit review</div>}
      </section>
      <section>
        <h3>Workspace</h3>
        <code>{workspacePath}</code>
        {hasLegacySource && <>
          <small className="workspace-source-label">Original Branch location</small>
          <code>{detail.branch.cwd}</code>
        </>}
      </section>
    </div>
  );
}

function MemoryTab({ entries }: { entries: Awaited<ReturnType<typeof api.branch>>["memory"] }) {
  if (!entries.length) return <div className="inspector-empty">No branch memory</div>;
  return <div className="inspector-section memory-stack">{entries.map((entry) => (
    <article key={entry.id}>
      <div><span>{entry.kind}</span><small>{entry.status}</small></div>
      <h3>{entry.title}</h3>
      <p>{entry.content}</p>
    </article>
  ))}</div>;
}

function ActivityTab({ entries, onOpenResults }: {
  entries: Awaited<ReturnType<typeof api.branch>>["activity"];
  onOpenResults: () => void;
}) {
  const [filter, setFilter] = useState("all");
  const types = Array.from(new Set(entries.map((entry) => entry.event_type))).sort();
  const visible = filter === "all" ? entries : entries.filter((entry) => entry.event_type === filter);
  if (!entries.length) return <div className="inspector-empty">No activity</div>;
  return <div className="activity-pane">
    <div className="activity-filter"><Field label="Event type"><Select value={filter} onChange={(_, data) => setFilter(data.value)}>
      <option value="all">All events</option>{types.map((type) => <option value={type} key={type}>{type}</option>)}
    </Select></Field></div>
    <div className="activity-list">{visible.map((entry) => {
      return <details key={entry.id} className={entry.event_type.includes("error") || entry.event_type.includes("failed") ? "activity-error" : ""}>
        <summary><span><strong>{activityTitle(entry.event_type)}</strong><small>{activitySummary(entry.payload)}</small></span><time>{formatDateTime(entry.created_at)}</time></summary>
        <div className="activity-detail">
          <div className="activity-identifiers">
            {entry.correlation_id && <code>correlation {entry.correlation_id}</code>}
            {Object.entries(entry.payload).filter(([key]) => key.endsWith("_id")).map(([key, value]) => <code key={key}>{key} {String(value)}</code>)}
          </div>
          <pre>{JSON.stringify(entry.payload, null, 2)}</pre>
          <div className="activity-actions">
            <Button size="small" onClick={onOpenResults}>Results</Button>
          </div>
        </div>
      </details>;
    })}</div>
  </div>;
}

function activityTitle(eventType: string): string {
  const titles: Record<string, string> = {
    "codex.error": "Codex error",
    "conversation.send_failed": "Message failed",
    "session.failed": "Session failed",
    "session.expired": "Session expired",
    "session.started": "Session started",
    "session.handoff": "Session handed off",
    "codex.turn_completed": "Turn completed",
  };
  return titles[eventType] ?? eventType.replaceAll(".", " ");
}

function activitySummary(payload: Record<string, unknown>): string {
  const error = payload.error;
  if (typeof error === "string") return error;
  if (error && typeof error === "object" && "message" in error) return String((error as { message: unknown }).message);
  if (typeof payload.outcome === "string") return payload.outcome;
  if (typeof payload.status === "string") return payload.status;
  const identifier = Object.entries(payload).find(([key]) => key.endsWith("_id"));
  return identifier ? `${identifier[0]} ${String(identifier[1])}` : "No summary";
}

function JobsTab({ entries, workItemId, branchId }: { entries: ExternalJob[]; workItemId: string; branchId: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => api.updateExternalJob(id, { status }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
  });
  return <div className="inspector-section job-stack">
    <div className="section-toolbar"><span>{entries.length} tracked</span><Button size="small" icon={<Clock20Regular />} onClick={() => setOpen(true)}>Register job</Button></div>
    {entries.map((job) => <article key={job.id}>
      <div><strong>{job.provider}</strong><span>{job.status}</span></div>
      <code>{job.remote_job_id}</code>
      <div className="job-actions">
        {job.url && <Button size="small" as="a" href={job.url} target="_blank">Open</Button>}
        {!['completed', 'failed', 'cancelled'].includes(job.status) && <Button size="small" disabled={update.isPending} onClick={() => update.mutate({ id: job.id, status: "completed" })}>Complete</Button>}
      </div>
    </article>)}
    {!entries.length && <div className="inspector-empty small">No external jobs</div>}
    <JobDialog workItemId={workItemId} branchId={branchId} open={open} onClose={() => setOpen(false)} />
  </div>;
}

function HandoffDialog({ branchId, session, latestTurn, jobs, acceptance, open, onClose, onSubmitted }: {
  branchId: string;
  session: Session;
  latestTurn?: CodexTurn;
  jobs: ExternalJob[];
  acceptance: Array<Record<string, unknown>>;
  open: boolean;
  onClose: () => void;
  onSubmitted: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState("continue");
  const [conclusion, setConclusion] = useState("");
  const [nextAction, setNextAction] = useState("");
  const [extractSkill, setExtractSkill] = useState(false);
  useEffect(() => {
    if (open) setConclusion(latestTurn?.final_answer ?? "");
  }, [latestTurn?.id, open]);
  useEffect(() => {
    if (!open) setExtractSkill(false);
  }, [open]);
  const save = useMutation({
    mutationFn: async () => {
      await api.handoff(branchId, session.id, {
        lease_id: session.lease_id,
        outcome,
        conclusion: conclusion.trim(),
        next_action: nextAction.trim() || null,
        source_turn_ids: latestTurn ? [latestTurn.external_turn_id] : [],
        changes: [],
        external_jobs: jobs.map((job) => `${job.provider}:${job.remote_job_id}:${job.status}`),
        artifacts: [], metrics: [], decisions: [], failures: [],
        acceptance_results: acceptance.map((item) => ({
          description: String(item.description ?? item), status: "unverified",
        })),
        risks: [],
      });
      if (!extractSkill) return { extractionError: "" };
      try {
        await api.createSkillProposal(branchId, session.id);
        return { extractionError: "" };
      } catch (error) {
        return { extractionError: error instanceof Error ? error.message : "Skill extraction failed" };
      }
    },
    onSuccess: async ({ extractionError }) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
        queryClient.invalidateQueries({ queryKey: ["skill-proposals"] }),
      ]);
      onSubmitted(extractionError
        ? `Handoff saved. Skill proposal could not start: ${extractionError}`
        : extractSkill ? "Handoff saved. Skill proposal is analyzing in the Inbox." : "Handoff saved.");
      setConclusion(""); setNextAction(""); onClose();
    },
  });
  return <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
    <DialogSurface><DialogBody><DialogTitle>Handoff session</DialogTitle>
      <DialogContent className="dialog-form">
        <Field label="Outcome"><Select value={outcome} onChange={(_, data) => setOutcome(data.value)}>
          <option value="continue">Continue later</option><option value="completed">Ready for review</option>
          <option value="waiting_external">Waiting external</option><option value="blocked">Blocked</option>
        </Select></Field>
        <Field label="Conclusion" required><Textarea resize="vertical" value={conclusion} onChange={(_, data) => setConclusion(data.value)} /></Field>
        <Field label="Next action"><Input value={nextAction} onChange={(_, data) => setNextAction(data.value)} /></Field>
        <Checkbox
          checked={extractSkill}
          onChange={(_, data) => setExtractSkill(Boolean(data.checked))}
          label="Extract reusable skill for review"
        />
        {save.error && <div className="inline-error">{save.error.message}</div>}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Cancel</Button><Button appearance="primary" disabled={!conclusion.trim() || save.isPending} onClick={() => save.mutate()}>Save handoff</Button></DialogActions>
    </DialogBody></DialogSurface>
  </Dialog>;
}

function GoalDialog({
  open,
  initialObjective,
  initialTokenBudget,
  editing,
  submitting,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean;
  initialObjective: string;
  initialTokenBudget?: number | null;
  editing: boolean;
  submitting: boolean;
  error?: string;
  onClose: () => void;
  onSubmit: (input: GoalCreateInput) => void;
}) {
  const [objective, setObjective] = useState(initialObjective);
  const [tokenBudget, setTokenBudget] = useState(
    initialTokenBudget ? String(initialTokenBudget) : "",
  );

  useEffect(() => {
    if (!open) return;
    setObjective(initialObjective);
    setTokenBudget(initialTokenBudget ? String(initialTokenBudget) : "");
  }, [initialObjective, initialTokenBudget, open]);

  const close = () => {
    if (submitting) return;
    onClose();
  };
  const parsedBudget = tokenBudget.trim() ? Number(tokenBudget) : null;
  const budgetValid = parsedBudget === null
    || (Number.isInteger(parsedBudget) && parsedBudget > 0);
  const valid = Boolean(objective.trim() && objective.length <= 4_000 && budgetValid);
  const submit = () => {
    if (!valid) return;
    onSubmit({
      objective,
      token_budget: parsedBudget,
    });
  };
  return <Dialog open={open} onOpenChange={(_event, data) => !data.open && close()}>
    <DialogSurface>
      <DialogBody>
        <DialogTitle>{editing ? "Edit Codex Native Goal" : "Start Codex Native Goal"}</DialogTitle>
        <DialogContent className="dialog-form goal-dialog">
          <Field label="Objective" required>
            <Textarea resize="vertical" value={objective} onChange={(_event, data) => setObjective(data.value)} />
          </Field>
          <Field
            label="Token budget"
            hint="Optional. Leave blank for no Goal-specific cap."
            validationMessage={!budgetValid ? "Enter a positive whole number." : undefined}
          >
            <Input
              type="number"
              min="1"
              step="1000"
              placeholder="No cap"
              value={tokenBudget}
              onChange={(_event, data) => setTokenBudget(data.value)}
            />
          </Field>
          {error && <div className="inline-error" role="alert">{error}</div>}
        </DialogContent>
        <DialogActions>
          <Button appearance="secondary" disabled={submitting} onClick={close}>Cancel</Button>
          <Button appearance="primary" disabled={!valid || submitting} onClick={submit}>{editing ? "Update goal" : "Start goal"}</Button>
        </DialogActions>
      </DialogBody>
    </DialogSurface>
  </Dialog>;
}

function ResultDialog({ branchId, hasParent, open, onClose, onSubmitted }: {
  branchId: string;
  hasParent: boolean;
  open: boolean;
  onClose: () => void;
  onSubmitted: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState(hasParent ? "merge" : "park");
  const [conclusion, setConclusion] = useState("");
  const [nextAction, setNextAction] = useState("");
  const [confidence, setConfidence] = useState("0.7");
  useEffect(() => setOutcome(hasParent ? "merge" : "park"), [hasParent, open]);
  const submit = useMutation({
    mutationFn: () => api.submitResult(branchId, {
      outcome,
      conclusion: conclusion.trim(),
      confidence: Number(confidence),
      acceptance_results: [], artifact_refs: [], proposed_promotions: [],
      next_action: nextAction.trim() || null,
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["merge-queue"] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
      ]);
      const confirmation = {
        merge: "Result submitted. Merge review created.",
        park: "Result submitted. Path parked.",
        abandon: "Result submitted. Path abandoned.",
        expire: "Result submitted. Path expired.",
      }[outcome] ?? "Result submitted.";
      onSubmitted(confirmation);
      setConclusion(""); setNextAction(""); onClose();
    },
  });
  return <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
    <DialogSurface><DialogBody><DialogTitle>Submit branch result</DialogTitle>
      <DialogContent className="dialog-form">
        <Field label="Disposition"><Select value={outcome} onChange={(_, data) => setOutcome(data.value)}>
          {hasParent && <option value="merge">Request merge to parent</option>}
          <option value="park">Park</option><option value="abandon">Abandon</option><option value="expire">Expire</option>
        </Select></Field>
        <Field label="Conclusion" required><Textarea resize="vertical" value={conclusion} onChange={(_, data) => setConclusion(data.value)} /></Field>
        <Field label="Confidence" hint="0 to 1"><Input type="number" min="0" max="1" step="0.1" value={confidence} onChange={(_, data) => setConfidence(data.value)} /></Field>
        <Field label="Next action"><Input value={nextAction} onChange={(_, data) => setNextAction(data.value)} /></Field>
        {submit.error && <div className="inline-error">{submit.error.message}</div>}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Cancel</Button><Button appearance="primary" disabled={!conclusion.trim() || submit.isPending} onClick={() => submit.mutate()}>Submit result</Button></DialogActions>
    </DialogBody></DialogSurface>
  </Dialog>;
}

function MemoryDialog({ branchId, programCode, programId, programDesk, open, onClose }: {
  branchId: string;
  programCode: string;
  programId: string;
  programDesk: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("observation");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const save = useMutation({
    mutationFn: () => api.createMemory({
      program_code: programCode,
      scope_type: programDesk ? "program" : "branch",
      scope_id: programDesk ? programId : branchId,
      source_branch_id: branchId,
      kind, title: title.trim(), content: content.trim(), confidence: 0.7,
      status: "proposed", evidence_refs: [],
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["memory"] }),
      ]);
      setTitle(""); setContent(""); onClose();
    },
  });
  return <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
    <DialogSurface><DialogBody><DialogTitle>Propose memory</DialogTitle>
      <DialogContent className="dialog-form">
        <Field label="Kind"><Select value={kind} onChange={(_, data) => setKind(data.value)}>
          <option value="observation">Observation</option><option value="fact">Fact</option><option value="decision">Decision</option>
          <option value="hypothesis">Hypothesis</option><option value="failure">Failure</option><option value="procedure">Procedure</option>
        </Select></Field>
        <Field label="Title" required><Input value={title} onChange={(_, data) => setTitle(data.value)} /></Field>
        <Field label="Content" required><Textarea resize="vertical" value={content} onChange={(_, data) => setContent(data.value)} /></Field>
        {save.error && <div className="inline-error">{save.error.message}</div>}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Cancel</Button><Button appearance="primary" disabled={!title.trim() || !content.trim() || save.isPending} onClick={() => save.mutate()}>Propose</Button></DialogActions>
    </DialogBody></DialogSurface>
  </Dialog>;
}

function JobDialog({ workItemId, branchId, open, onClose }: { workItemId: string; branchId: string; open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [provider, setProvider] = useState("ltp");
  const [remoteId, setRemoteId] = useState("");
  const [status, setStatus] = useState("running");
  const [url, setUrl] = useState("");
  const create = useMutation({
    mutationFn: () => api.createExternalJob({
      work_item_id: workItemId, provider: provider.trim(), remote_job_id: remoteId.trim(),
      status, url: url.trim() || undefined,
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
      ]);
      setRemoteId(""); setUrl(""); onClose();
    },
  });
  return <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
    <DialogSurface><DialogBody><DialogTitle>Register external job</DialogTitle>
      <DialogContent className="dialog-form">
        <Field label="Provider" required><Input value={provider} onChange={(_, data) => setProvider(data.value)} /></Field>
        <Field label="Remote job ID" required><Input value={remoteId} onChange={(_, data) => setRemoteId(data.value)} /></Field>
        <Field label="Status"><Select value={status} onChange={(_, data) => setStatus(data.value)}><option value="queued">Queued</option><option value="running">Running</option><option value="completed">Completed</option><option value="failed">Failed</option></Select></Field>
        <Field label="Console URL"><Input value={url} onChange={(_, data) => setUrl(data.value)} /></Field>
        {create.error && <div className="inline-error">{create.error.message}</div>}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Cancel</Button><Button appearance="primary" disabled={!provider.trim() || !remoteId.trim() || create.isPending} onClick={() => create.mutate()}>Register</Button></DialogActions>
    </DialogBody></DialogSurface>
  </Dialog>;
}

function WorkStateDialog({ workItem, programCode, branchId, open, onClose }: {
  workItem: Awaited<ReturnType<typeof api.branch>>["work_item"];
  programCode: string;
  branchId: string;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [lifecycle, setLifecycle] = useState(workItem.lifecycle);
  const [flow, setFlow] = useState(workItem.flow);
  const [priority, setPriority] = useState(String(workItem.priority));
  useEffect(() => {
    if (open) {
      setLifecycle(workItem.lifecycle);
      setFlow(workItem.flow);
      setPriority(String(workItem.priority));
    }
  }, [open, workItem.flow, workItem.lifecycle, workItem.priority]);
  const update = useMutation({
    mutationFn: () => api.updateWork(workItem.id, {
      lifecycle, flow, priority: Number(priority), expected_revision: workItem.revision,
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["branch", branchId] }),
        queryClient.invalidateQueries({ queryKey: ["tree", programCode] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
      ]);
      onClose();
    },
  });
  return <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
    <DialogSurface><DialogBody><DialogTitle>Update work state</DialogTitle>
      <DialogContent className="dialog-form">
        <Field label="Lifecycle"><Select value={lifecycle} onChange={(_, data) => setLifecycle(data.value)}>
          <option value="draft">Draft</option><option value="ready">Ready</option><option value="active">Active</option>
          <option value="review">Review</option><option value="done">Done</option><option value="cancelled">Cancelled</option>
        </Select></Field>
        <Field label="Flow"><Select value={flow} onChange={(_, data) => setFlow(data.value)}>
          <option value="actionable">Actionable</option><option value="running">Running</option>
          <option value="waiting_external">Waiting external</option><option value="blocked">Blocked</option>
          <option value="needs_decision">Needs decision</option>
        </Select></Field>
        <Field label="Priority" hint="0 to 100"><Input type="number" min="0" max="100" value={priority} onChange={(_, data) => setPriority(data.value)} /></Field>
        {update.error && <div className="inline-error">{update.error.message}</div>}
      </DialogContent>
      <DialogActions><Button onClick={onClose}>Cancel</Button><Button appearance="primary" disabled={update.isPending} onClick={() => update.mutate()}>Update</Button></DialogActions>
    </DialogBody></DialogSurface>
  </Dialog>;
}
