import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import MemoryView from "./MemoryView";

const completedReflection = {
  id: "reflection-1",
  reflection_date: "2026-07-27",
  timezone: "Asia/Shanghai",
  status: "completed",
  source_started_at: "2026-07-26T16:00:00Z",
  source_ended_at: "2026-07-27T16:00:00Z",
  source_counts: {
    completed_turns: 8,
    external_jobs: 3,
    work_items: ["HARN-0001", "ADAPT-0003"],
    work_item_titles: [
      { id: "HARN-0001", title: "PPT trajectory validation", program: "HARN" },
      { id: "ADAPT-0003", title: "GLM5.2 adaptation", program: "ADAPT" },
    ],
    unassigned_windows: 1,
  },
  evidence_refs: ["turn-1", "job-1"],
  report_json: {},
  report_markdown: "# 2026-07-27 每日总结与反思\n\n## 经验沉淀候选\n\n保留对照实验。",
  agent_model: "gpt-6-astra",
  attempts: 1,
  notification_status: "sent",
  notification_attempts: 1,
  notification_sent_at: "2026-07-27T16:05:00Z",
  completed_at: "2026-07-27T16:04:00Z",
  created_at: "2026-07-27T16:00:00Z",
  updated_at: "2026-07-27T16:05:00Z",
};

const failedReflection = {
  ...completedReflection,
  id: "reflection-2",
  reflection_date: "2026-07-26",
  status: "failed",
  report_markdown: "",
  error: "Selected model is at capacity",
  notification_status: "pending",
};

const memoryEntry = {
  id: "memory-1",
  program_id: "program-harn",
  scope_type: "branch",
  scope_id: "branch-1",
  kind: "procedure",
  status: "accepted",
  title: "Keep paired controls",
  content: "Run baseline and candidate with the same evaluation controls.",
  confidence: 0.9,
  source_branch_id: "branch-1",
  evidence_refs: ["turn-1"],
  version: 1,
  created_at: "2026-07-27T10:00:00Z",
  updated_at: "2026-07-27T10:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("shows daily reflections in Memory and retries a failed day", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let payload: unknown = url.endsWith("/daily-reflections")
      ? [completedReflection, failedReflection]
      : [memoryEntry];
    if (init?.method === "POST" && url.endsWith("/daily-reflections/run")) {
      payload = { ...failedReflection, status: "queued", error: undefined };
    }
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(
    <QueryClientProvider client={client}>
      <MemoryView programs={[]} />
    </QueryClientProvider>,
  );

  expect(await screen.findByRole("heading", { name: "经验沉淀候选" })).toBeInTheDocument();
  expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/memory"))).toBe(false);
  expect(screen.getAllByText("PPT trajectory validation")).toHaveLength(2);
  expect(screen.getAllByText("GLM5.2 adaptation")).toHaveLength(2);
  expect(screen.queryByText("8 turns")).not.toBeInTheDocument();
  expect(screen.getAllByText("8 recorded turns")).toHaveLength(2);
  expect(screen.getAllByText("1 unassigned windows")).toHaveLength(2);
  expect(screen.getByText("Selected model is at capacity")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/daily-reflections/run",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ reflection_date: "2026-07-26", force: true }),
    }),
  ));

  rerender(<QueryClientProvider client={client}><MemoryView programs={[]} section="ledger" /></QueryClientProvider>);
  expect(screen.getByRole("heading", { name: "记忆账本" })).toBeInTheDocument();
  expect(await screen.findByText("Keep paired controls")).toBeInTheDocument();
});
