import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Textarea, Tooltip } from "@fluentui/react-components";
import { ArrowSync20Regular, Checkmark20Regular, Clock20Regular, Dismiss20Regular, Pause20Regular, Play20Regular, Sparkle20Regular, Stop20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime, formatTime } from "../time";
import type { ScheduledCheckDraft, ScheduledCheckPrompt, ScheduledCheckView } from "../types";
import { ExecutionBadge } from "./ExecutionControls";

const INTERVALS = [5, 10, 30, 60];
const LABELS: Record<string, string> = {
  scheduled: "Scheduled", checking: "Checking", dispatching: "Starting check",
  waiting_session: "Waiting for session", retry_scheduled: "Retry scheduled",
  paused: "Paused", stopped: "Stopped", completed: "Completed",
};
const OUTCOMES: Record<string, string> = {
  unchanged: "No change", changed: "Progress", failed: "Task failed", completed: "Task completed",
  needs_input: "Decision needed", unclassified: "Unclassified result", error: "Check failed",
  interrupted: "Interrupted", deferred: "Deferred",
};
const WAKEUP_LABELS: Record<string, string> = {
  awaiting_turn: "After this turn", scheduled: "Scheduled", claimed: "Starting",
  running: "Running", completed: "Completed", cancelled: "Cancelled", failed: "Failed", missed: "Missed",
};
const pendingWakeup = (status: string) => ["awaiting_turn", "scheduled", "claimed"].includes(status);

function shanghaiInput(value: string) {
  return new Date(new Date(value).getTime() + 8 * 3600_000).toISOString().slice(0, 16);
}

export default function ScheduledCheckControls({ branchId }: { branchId: string }) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [interval, setInterval] = useState("10");
  const [customMinutes, setCustomMinutes] = useState("10");
  const [stopMode, setStopMode] = useState<ScheduledCheckDraft["stop_mode"]>("manual");
  const [stopAt, setStopAt] = useState("");
  const [jobId, setJobId] = useState("");
  const [notifications, setNotifications] = useState<ScheduledCheckDraft["notification_mode"]>("important");
  const [generating, setGenerating] = useState(false);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [generated, setGenerated] = useState<ScheduledCheckPrompt | null>(null);
  const [suggestion, setSuggestion] = useState<ScheduledCheckPrompt | null>(null);
  const [refreshingHealth, setRefreshingHealth] = useState(false);
  const generationId = useRef(0);
  const edits = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const editingBlockedRef = useRef(false);
  useEffect(() => () => { generationId.current += 1; requestController.current?.abort(); }, []);
  const query = useQuery({
    queryKey: ["scheduled-check", branchId], queryFn: () => api.scheduledCheck(branchId),
    refetchInterval: (result) => result.state.data?.check?.status === "active" || result.state.data?.wakeups?.some((item) => pendingWakeup(item.status) || item.status === "running") || open ? 15_000 : 60_000,
    retry: false,
  });
  const data = query.data?.worker ? query.data : undefined;
  const check = data?.check;
  const wakeup = data?.wakeups?.find((item) => pendingWakeup(item.status) || item.status === "running");
  const worker = data?.worker;
  // Older servers omit status. Judge their snapshot at receipt, never by how
  // long it has sat in the browser cache while the dialog was closed.
  const workerStatus = worker?.status ?? (!worker?.enabled ? "disabled" : worker.error ? "error"
    : !worker.last_tick_at || query.dataUpdatedAt - Date.parse(worker.last_tick_at) > 60_000
      ? "stale" : "healthy");
  const refreshHealth = async () => {
    setRefreshingHealth(true);
    try {
      await query.refetch({ cancelRefetch: false });
    } finally {
      setRefreshingHealth(false);
    }
  };
  const refresh = (value: ScheduledCheckView) => cache.setQueryData(["scheduled-check", branchId], value);
  const save = useMutation({
    mutationFn: (value: ScheduledCheckDraft) => api.saveScheduledCheck(branchId, value),
    onSuccess: (value) => { refresh(value); close(); },
  });
  const control = useMutation({ mutationFn: (action: "pause" | "resume" | "stop" | "run_now") => api.controlScheduledCheck(branchId, action), onSuccess: refresh });
  const cancelWakeup = useMutation({ mutationFn: (id: string) => api.cancelClaudeWakeup(branchId, id), onSuccess: refresh });
  const minutes = Number(interval === "custom" ? customMinutes : interval);
  const date = stopAt ? new Date(`${stopAt}:00+08:00`) : null;
  const valid = prompt.trim() && !prompt.includes("【填写明确的任务名或 Job ID】")
    && Number.isInteger(minutes) && minutes >= 1 && minutes <= 10080
    && (stopMode !== "deadline" || (date && Number.isFinite(date.getTime()) && date.getTime() > Date.now()))
    && (stopMode !== "job" || jobId);
  const pending = save.isPending || control.isPending;
  const editingBlocked = Boolean(check?.active_run_id);
  editingBlockedRef.current = editingBlocked;
  const close = () => {
    generationId.current += 1;
    requestController.current?.abort();
    setGenerating(false);
    setOpen(false);
  };
  const generate = async (currentPrompt: string, autoFill: boolean) => {
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    const id = ++generationId.current;
    const editVersion = edits.current;
    setGenerating(true); setGenerationError(null); setSuggestion(null);
    try {
      const result = await api.draftScheduledCheckPrompt(branchId, currentPrompt, controller.signal);
      if (generationId.current !== id) return;
      if (!result?.prompt) throw new Error("No draft returned. Retry or edit the prompt manually.");
      if (autoFill && edits.current === editVersion && !editingBlockedRef.current) {
        setPrompt(result.prompt); setGenerated(result);
      } else setSuggestion(result);
    } catch (error) {
      if (generationId.current === id && !controller.signal.aborted) {
        setGenerationError(error instanceof Error ? error.message : "Unable to draft the check prompt");
      }
    } finally {
      if (generationId.current === id) setGenerating(false);
    }
  };
  const edit = () => {
    const initialPrompt = check?.prompt || data?.default_prompt || "";
    setPrompt(initialPrompt);
    edits.current = 0;
    setGenerated(null); setSuggestion(null); setGenerationError(null);
    const value = check?.interval_minutes || 10;
    setInterval(INTERVALS.includes(value) ? String(value) : "custom");
    setCustomMinutes(String(value));
    setStopMode(check?.stop_mode || "manual");
    setStopAt(check?.stop_at ? shanghaiInput(check.stop_at) : "");
    setJobId(check?.job_id || "");
    setNotifications(check?.notification_mode || "important");
    save.reset(); control.reset(); setOpen(true);
    void refreshHealth();
    if (!editingBlocked && !wakeup) void generate(check?.prompt || "", !check);
  };
  if (query.error?.message === "Not Found") return null;
  const label = check ? (check.status !== "active" && !check.active_run_id ? LABELS[check.status] : LABELS[check.phase]) || check.phase : "";
  return <div className="scheduled-check-controls">
    <Tooltip content="Scheduled checks" relationship="label">
      <Button icon={<Clock20Regular />} size="small" appearance="subtle" disabled={!data} onClick={edit} />
    </Tooltip>
    {query.error && <Button size="small" icon={<ArrowSync20Regular />} onClick={() => query.refetch()}>Checks unavailable</Button>}
    {check && <div className="scheduled-check-summary" role="status">
      <span>{label}{check.active_run_id && check.status !== "active" ? ` · Future checks ${check.status}` : ""}</span>
      {check.last_completed_at && <span>Last {formatTime(check.last_completed_at)}</span>}
      {check.next_run_at && <span>Next {formatTime(check.next_run_at)}</span>}
    </div>}
    {wakeup && <div className="scheduled-check-summary" role="status">
      <span>Claude wakeup · {WAKEUP_LABELS[wakeup.status]}</span>
      {wakeup.status !== "running" && <span>{formatTime(wakeup.due_at)}</span>}
    </div>}
    <Dialog open={open} onOpenChange={(_, state) => { if (!pending && !state.open) close(); }}>
      <DialogSurface className="scheduled-check-dialog">
        <DialogBody>
          <DialogTitle>Scheduled checks</DialogTitle>
          <DialogContent>
            {data?.wakeups?.length ? <section className="scheduled-check-history" aria-label="Claude wakeups">
              <strong>Claude wakeups</strong>
              {data.wakeups.map((item) => <details key={item.id} open={pendingWakeup(item.status) || item.status === "running"}>
                <summary>{WAKEUP_LABELS[item.status] || item.status} · {formatDateTime(item.due_at)}</summary>
                <pre>{item.prompt}</pre>
                {item.reason && <p>{item.reason}</p>}
                {pendingWakeup(item.status) && <Tooltip content="Cancel this wakeup" relationship="label"><Button size="small" icon={<Stop20Regular />} disabled={cancelWakeup.isPending} onClick={() => cancelWakeup.mutate(item.id)} /></Tooltip>}
              </details>)}
              {cancelWakeup.error && <p role="alert">{cancelWakeup.error.message}</p>}
            </section> : null}
            {check && <div className="scheduled-check-state">
              <div><strong>{label}</strong><span>{check.reason}</span></div>
              <div className="scheduled-check-actions">
                <Tooltip content={check.status === "active" ? "Pause future checks" : "Resume scheduled checks"} relationship="label"><Button size="small" icon={check.status === "active" ? <Pause20Regular /> : <Play20Regular />} disabled={pending || (check.status !== "active" && Boolean(check.active_run_id))} onClick={() => control.mutate(check.status === "active" ? "pause" : "resume")} /></Tooltip>
                <Tooltip content="Check now" relationship="label"><Button size="small" icon={<ArrowSync20Regular />} disabled={pending || check.status !== "active" || Boolean(check.active_run_id)} onClick={() => control.mutate("run_now")} /></Tooltip>
                <Tooltip content="Stop future checks; external jobs and the current turn are not stopped" relationship="label"><Button size="small" icon={<Stop20Regular />} disabled={pending || ["stopped", "completed"].includes(check.status)} onClick={() => control.mutate("stop")} /></Tooltip>
              </div>
            </div>}
            {check && <ExecutionBadge value={check.execution} />}
            {refreshingHealth ? <p role="status">Checking scheduler status...</p>
              : query.error ? <div>
                <p role="alert">Unable to refresh scheduler status. Retry to confirm.</p>
                <Button size="small" icon={<ArrowSync20Regular />} onClick={() => void refreshHealth()}>Retry status</Button>
              </div>
                : worker && (workerStatus === "starting" ? <p role="status">Scheduler is starting</p>
                  : workerStatus !== "healthy" && <p role="alert">{workerStatus === "disabled" ? "Scheduler is disabled" : workerStatus === "error" ? `Scheduler error: ${worker.error}` : "Scheduler heartbeat is unavailable"}</p>)}
            {data?.worker?.last_tick_at && <small className="scheduled-check-worker">Scheduler last checked {formatTime(data.worker.last_tick_at)}</small>}
            <div className="scheduled-check-form">
              <Field label="Interval">
                <Select value={interval} disabled={editingBlocked} onChange={(_, value) => setInterval(value.value)}>
                  {INTERVALS.map((value) => <option key={value} value={value}>{value} minutes</option>)}
                  <option value="custom">Custom</option>
                </Select>
              </Field>
              {interval === "custom" && <Field label="Minutes"><Input type="number" min={1} max={10080} value={customMinutes} disabled={editingBlocked} onChange={(_, value) => setCustomMinutes(value.value)} /></Field>}
              <Field label="Stop condition">
                <Select value={stopMode} disabled={editingBlocked} onChange={(_, value) => setStopMode(value.value as ScheduledCheckDraft["stop_mode"])}>
                  <option value="manual">Until stopped manually</option>
                  <option value="deadline">At a specified time</option>
                  <option value="job">When a linked job ends</option>
                </Select>
              </Field>
              {stopMode === "deadline" && <Field label="Stop time · Shanghai (UTC+8)" required><Input type="datetime-local" value={stopAt} disabled={editingBlocked} onChange={(_, value) => setStopAt(value.value)} /></Field>}
              {stopMode === "job" && <Field label="Linked job" required><Select value={jobId} disabled={editingBlocked} onChange={(_, value) => setJobId(value.value)}>
                <option value="" disabled>{data?.jobs.length ? "Select job" : "No jobs registered"}</option>
                {data?.jobs.map((job) => <option key={job.id} value={job.id}>{job.name} · {job.status}</option>)}
              </Select></Field>}
              <div className="scheduled-check-prompt">
                <div className="scheduled-check-prompt-tools">
                  <span>{generated ? `OneAPI · ${generated.model}` : "OneAPI"}</span>
                  <Tooltip content="Generate prompt from recent conversation" relationship="label"><Button size="small" appearance="subtle" icon={<Sparkle20Regular />} disabled={generating || editingBlocked || pending} onClick={() => void generate(prompt, false)} /></Tooltip>
                </div>
                <Field label="Check prompt" required><Textarea rows={8} resize="vertical" value={prompt} disabled={editingBlocked} onChange={(_, value) => { edits.current += 1; setPrompt(value.value); }} /></Field>
                {generating && <span className="scheduled-check-draft-status" role="status">Drafting from recent conversation...</span>}
                {generationError && <p role="alert">{generationError}</p>}
                {generated && <small className="scheduled-check-draft-status">Context through {formatDateTime(generated.context_updated_at)}</small>}
                {suggestion && <div className="scheduled-check-suggestion">
                  <div className="scheduled-check-prompt-tools"><strong>Suggested prompt</strong><div>
                    <Tooltip content="Apply suggested prompt" relationship="label"><Button size="small" appearance="subtle" icon={<Checkmark20Regular />} disabled={editingBlocked || pending} onClick={() => { edits.current += 1; setPrompt(suggestion.prompt); setGenerated(suggestion); setSuggestion(null); }} /></Tooltip>
                    <Tooltip content="Dismiss suggestion" relationship="label"><Button size="small" appearance="subtle" icon={<Dismiss20Regular />} onClick={() => setSuggestion(null)} /></Tooltip>
                  </div></div>
                  <pre>{suggestion.prompt}</pre>
                  <small>OneAPI · {suggestion.model} · {formatDateTime(suggestion.context_updated_at)}</small>
                </div>}
              </div>
              <Field label="Notifications" className="scheduled-check-prompt"><Select value={notifications} disabled={editingBlocked} onChange={(_, value) => setNotifications(value.value as ScheduledCheckDraft["notification_mode"])}>
                <option value="important">Changes and action needed</option>
                <option value="all">Every check</option>
                <option value="none">Off</option>
              </Select></Field>
            </div>
            {data && !data.notifications_enabled && notifications !== "none" && <p role="alert">Ruliu notifications are unavailable. Check server notification settings.</p>}
            {data?.notification?.error && <p role="alert">Notification failed: {data.notification.error}</p>}
            {(save.error || control.error) && <p role="alert">{save.error?.message || control.error?.message}</p>}
            {data?.runs.length ? <details className="scheduled-check-history"><summary>Recent checks · {data.runs.length}</summary>
              {data.runs.map((run) => <details key={run.id}><summary><time>{formatDateTime(run.created_at)}</time><span>{OUTCOMES[run.outcome || run.status] || run.status}</span></summary><pre>{run.summary || "Waiting for a result"}</pre></details>)}
            </details> : null}
          </DialogContent>
          <DialogActions>
            <Button disabled={pending} onClick={close}>Cancel</Button>
            <Button appearance="primary" icon={<Checkmark20Regular />} disabled={pending || !valid || editingBlocked || !data?.worker.enabled} onClick={() => save.mutate({ prompt, interval_minutes: minutes, stop_mode: stopMode, stop_at: stopMode === "deadline" ? date!.toISOString() : null, job_id: stopMode === "job" ? jobId : null, notification_mode: notifications })}>{check?.status === "active" ? "Save schedule" : "Start schedule"}</Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  </div>;
}
