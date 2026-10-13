import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import type { ScheduledCheck, ScheduledCheckPrompt, ScheduledCheckView } from "../types";
import ScheduledCheckControls from "./ScheduledCheckControls";

const empty: ScheduledCheckView = {
  check: null, runs: [], jobs: [{ id: "job-1", name: "train-test", status: "Running", provider: "ltp" }],
  default_prompt: "检查任务【填写明确的任务名或 Job ID】的最新进展。\n检查结果：无变化",
  notification: null, notifications_enabled: true,
  worker: { enabled: true, last_tick_at: new Date().toISOString(), error: null },
};
const check: ScheduledCheck = {
  id: "check-1", branch_id: "branch-1", status: "active", phase: "scheduled",
  prompt: "Check job-123 once", interval_minutes: 10, stop_mode: "manual", stop_at: null,
  job_id: null, notification_mode: "important", execution: { identity: "api", model: "frozen-model", reasoning_effort: "max" },
  next_run_at: "2030-01-01T00:10:00Z", active_run_id: null, last_completed_at: "2030-01-01T00:00:00Z",
  last_outcome: "unchanged", last_summary: "Still running", consecutive_errors: 0, reason: null,
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function show(value = empty) {
  const read = vi.spyOn(api, "scheduledCheck").mockResolvedValue(value);
  const draft = vi.spyOn(api, "draftScheduledCheckPrompt").mockImplementation(() => new Promise(() => {}));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ScheduledCheckControls branchId="branch-1" /></QueryClientProvider>);
  return { draft, read, client };
}
async function open() {
  const button = await screen.findByRole("button", { name: "Scheduled checks" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

test("native Claude wakeup is visible and cancellable without an AI draft or periodic schedule", async () => {
  const wakeup = { id: "wakeup-1", status: "scheduled", prompt: "Check the debug receipt once.", due_at: "2030-01-01T00:20:00Z", reason: null, source_turn_id: "turn-1", message_id: null };
  const cancel = vi.spyOn(api, "cancelClaudeWakeup").mockResolvedValue({ ...empty, wakeups: [{ ...wakeup, status: "cancelled", reason: "Cancelled by user" }] });
  const save = vi.spyOn(api, "saveScheduledCheck");
  const { draft } = show({ ...empty, wakeups: [wakeup] });
  expect(await screen.findByText("Claude wakeup · Scheduled")).toBeInTheDocument();
  await open();
  expect(screen.getByText("Check the debug receipt once.")).toBeInTheDocument();
  expect(draft).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel this wakeup" }));
  await waitFor(() => expect(cancel).toHaveBeenCalledWith("branch-1", "wakeup-1"));
  expect(await screen.findByText("Cancelled by user")).toBeInTheDocument();
  expect(save).not.toHaveBeenCalled();
});

test("clock opens the complete form without starting work", async () => {
  const save = vi.spyOn(api, "saveScheduledCheck");
  show(); await open();
  expect(screen.getByLabelText("Interval")).toHaveValue("10");
  expect(screen.getByLabelText("Notifications")).toHaveValue("important");
  expect(screen.getByRole("button", { name: "Start schedule" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(save).not.toHaveBeenCalled();
});

test("custom interval saves the user's exact prompt and never a Goal", async () => {
  const save = vi.spyOn(api, "saveScheduledCheck").mockResolvedValue({ ...empty, check });
  show(); await open();
  const prompt = "  Check my job.\nNo automatic retries.\n";
  fireEvent.change(screen.getByRole("textbox", { name: "Check prompt" }), { target: { value: prompt } });
  fireEvent.change(screen.getByLabelText("Interval"), { target: { value: "custom" } });
  fireEvent.change(screen.getByLabelText("Minutes"), { target: { value: "17" } });
  fireEvent.click(screen.getByRole("button", { name: "Start schedule" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("branch-1", {
    prompt, interval_minutes: 17, stop_mode: "manual", stop_at: null, job_id: null, notification_mode: "important",
  }));
  expect(await screen.findByRole("status")).toHaveTextContent("Scheduled");
});

test("Shanghai deadline is persisted as UTC and a job selection is required", async () => {
  const save = vi.spyOn(api, "saveScheduledCheck").mockResolvedValue({ ...empty, check });
  show(); await open();
  fireEvent.change(screen.getByRole("textbox", { name: "Check prompt" }), { target: { value: "Check job" } });
  fireEvent.change(screen.getByLabelText("Stop condition"), { target: { value: "job" } });
  expect(screen.getByRole("button", { name: "Start schedule" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Linked job" }), { target: { value: "job-1" } });
  expect(screen.getByRole("button", { name: "Start schedule" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Stop condition"), { target: { value: "deadline" } });
  fireEvent.change(screen.getByLabelText(/Stop time.*Shanghai/), { target: { value: "2030-01-01T09:30" } });
  fireEvent.click(screen.getByRole("button", { name: "Start schedule" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("branch-1", expect.objectContaining({ stop_at: "2030-01-01T01:30:00.000Z", job_id: null })));
});

test("pause and stop affect only future checks; running prompts cannot be edited", async () => {
  const running = { ...empty, check: { ...check, active_run_id: "run-1", phase: "checking" } };
  const control = vi.spyOn(api, "controlScheduledCheck").mockResolvedValue({ ...running, check: { ...running.check, status: "paused" } });
  show(running); await open();
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Check now" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Pause future checks" }));
  await waitFor(() => expect(control).toHaveBeenCalledWith("branch-1", "pause"));
  const stop = screen.getByRole("button", { name: /Stop future checks/ });
  await waitFor(() => expect(stop).toBeEnabled());
  fireEvent.click(stop);
  await waitFor(() => expect(control).toHaveBeenCalledWith("branch-1", "stop"));
});

test("worker health and notification errors remain visible", async () => {
  show({ ...empty, check, notification: { status: "failed", error: "Recipient unavailable" }, worker: { ...empty.worker, enabled: false } });
  await open();
  expect(await screen.findByText("Scheduler is disabled")).toBeInTheDocument();
  expect(screen.getByText(/Notification failed: Recipient unavailable/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save schedule" })).toBeDisabled();
  expect(screen.getByText(/frozen-model.*max/)).toBeInTheDocument();
});

test("opening refreshes worker health immediately instead of alarming on cached state", async () => {
  const stale: ScheduledCheckView = {
    ...empty,
    worker: { ...empty.worker, last_tick_at: "2000-01-01T00:00:00Z", status: "stale" },
  };
  const { read } = show(stale);
  await waitFor(() => expect(screen.getByRole("button", { name: "Scheduled checks" })).toBeEnabled());
  let resolve!: (value: ScheduledCheckView) => void;
  read.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));

  await open();

  expect(read).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Checking scheduler status...")).toBeInTheDocument();
  expect(screen.queryByText("Scheduler heartbeat is unavailable")).not.toBeInTheDocument();
  resolve({ ...empty, worker: { ...empty.worker, status: "healthy" } });
  await waitFor(() => expect(screen.queryByText("Checking scheduler status...")).not.toBeInTheDocument());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("server health is authoritative when the browser clock differs", async () => {
  show({ ...empty, worker: { ...empty.worker, last_tick_at: "2000-01-01T00:00:00Z", status: "healthy" } });
  await open();
  await waitFor(() => expect(screen.queryByText("Checking scheduler status...")).not.toBeInTheDocument());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each([
  ["stale", null, "Scheduler heartbeat is unavailable"],
  ["error", "OperationalError", "Scheduler error: OperationalError"],
  ["starting", null, "Scheduler is starting"],
] as const)("a fresh %s worker status stays visible", async (status, error, message) => {
  show({ ...empty, worker: { ...empty.worker, status, error } });
  await open();
  expect(await screen.findByText(message)).toBeInTheDocument();
  if (status === "starting") expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("worker refresh failure is distinct from a stopped scheduler and retry preserves edits", async () => {
  const { read } = show();
  await waitFor(() => expect(screen.getByRole("button", { name: "Scheduled checks" })).toBeEnabled());
  read.mockRejectedValueOnce(new Error("Network unavailable"));

  await open();

  expect(await screen.findByText("Unable to refresh scheduler status. Retry to confirm.")).toBeInTheDocument();
  expect(screen.queryByText("Scheduler heartbeat is unavailable")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Check prompt" }), { target: { value: "Keep my check prompt" } });
  fireEvent.click(screen.getByRole("button", { name: "Retry status" }));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
  await waitFor(() => expect(screen.queryByText("Checking scheduler status...")).not.toBeInTheDocument());
  expect(screen.queryByText("Unable to refresh scheduler status. Retry to confirm.")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue("Keep my check prompt");
});

test("network failure keeps the editable draft and offers a retry", async () => {
  const save = vi.spyOn(api, "saveScheduledCheck").mockRejectedValueOnce(new Error("Temporary failure")).mockResolvedValueOnce({ ...empty, check });
  show(); await open();
  fireEvent.change(screen.getByRole("textbox", { name: "Check prompt" }), { target: { value: "Keep this prompt" } });
  fireEvent.click(screen.getByRole("button", { name: "Start schedule" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Temporary failure");
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue("Keep this prompt");
  fireEvent.click(screen.getByRole("button", { name: "Start schedule" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
});

const generated: ScheduledCheckPrompt = {
  prompt: "检查 train-current 的最新进展。检查结果：无变化／有进展／失败／完成／需要决定。",
  provider: "oneapi", model: "DeepSeek-V4.1-Flash", generated_at: new Date().toISOString(),
  context_updated_at: new Date().toISOString(), context_turns: 4, context_messages: 1, cached: false,
};

test("opening the clock drafts from context, without starting a check or calling on render", async () => {
  const save = vi.spyOn(api, "saveScheduledCheck");
  const control = vi.spyOn(api, "controlScheduledCheck");
  const { draft } = show();
  expect(draft).not.toHaveBeenCalled();
  draft.mockResolvedValue(generated);
  await open();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(generated.prompt));
  expect(draft).toHaveBeenCalledWith("branch-1", "", expect.any(AbortSignal));
  expect(screen.getByText("OneAPI · DeepSeek-V4.1-Flash")).toBeInTheDocument();
  expect(save).not.toHaveBeenCalled();
  expect(control).not.toHaveBeenCalled();
});

test("an existing prompt is preserved until the suggestion is explicitly applied", async () => {
  const { draft } = show({ ...empty, check });
  draft.mockResolvedValue(generated);
  await open();
  const apply = await screen.findByRole("button", { name: "Apply suggested prompt" });
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(check.prompt);
  expect(draft).toHaveBeenCalledWith("branch-1", check.prompt, expect.any(AbortSignal));
  fireEvent.click(apply);
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(generated.prompt);
});

test("a late draft never replaces a manual edit", async () => {
  const { draft } = show();
  let resolve!: (value: ScheduledCheckPrompt) => void;
  draft.mockImplementation(() => new Promise((done) => { resolve = done; }));
  await open();
  fireEvent.change(screen.getByRole("textbox", { name: "Check prompt" }), { target: { value: "My exact prompt" } });
  resolve(generated);
  await screen.findByRole("button", { name: "Apply suggested prompt" });
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue("My exact prompt");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
  expect(screen.queryByText("Suggested prompt")).not.toBeInTheDocument();
});

test("closing and reopening ignores the old response and reads fresh context", async () => {
  const { draft } = show();
  let resolveFirst!: (value: ScheduledCheckPrompt) => void;
  draft.mockImplementationOnce(() => new Promise((done) => { resolveFirst = done; })).mockResolvedValue(generated);
  await open();
  const signal = draft.mock.calls[0][2];
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(signal?.aborted).toBe(true);
  await open();
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(generated.prompt));
  resolveFirst({ ...generated, prompt: "OLD RESPONSE" });
  await waitFor(() => expect(draft).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(generated.prompt);
});

test.each([
  "OneAPI returned HTTP 429",
  "OneAPI exhausted its output budget before completing the draft. Your prompt is unchanged; retry or edit it manually.",
])("OneAPI errors retain the draft and allow explicit regeneration: %s", async (message) => {
  const { draft } = show({ ...empty, check });
  draft.mockRejectedValueOnce(new Error(message)).mockResolvedValueOnce(generated);
  await open();
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.getByRole("textbox", { name: "Check prompt" })).toHaveValue(check.prompt);
  fireEvent.click(screen.getByRole("button", { name: "Generate prompt from recent conversation" }));
  await screen.findByRole("button", { name: "Apply suggested prompt" });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("a running check keeps the prompt read-only and does not request a draft", async () => {
  const { draft } = show({ ...empty, check: { ...check, phase: "checking", active_run_id: "running" } });
  await open();
  expect(draft).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Generate prompt from recent conversation" })).toBeDisabled();
});
