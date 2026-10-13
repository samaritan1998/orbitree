import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import GoalHistory from "./GoalHistory";
import type { NativeGoalHistoryEntry } from "../types";

const entry: NativeGoalHistoryEntry = {
  goal_key: "old-goal", thread_id: "historical-session", objective: "Original objective\nKeep all evidence.",
  status: "complete", token_budget: null, tokens_used: 42, time_used_seconds: 120,
  goal_created_at: null, goal_updated_at: null,
  first_recorded_at: "2026-09-12T01:00:00Z", last_recorded_at: "2026-09-13T02:00:00Z",
  cleared_at: null, last_summary: "Paired experiment completed.", last_error: null,
  turn_id: null, source: "notification",
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function showHistory() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><GoalHistory branchId="branch-1" /></QueryClientProvider>);
}

test("shows recovered history, Shanghai timestamps and a copyable session without execution controls", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [entry], total: 1, next_offset: null })));
  vi.stubGlobal("fetch", fetcher);
  showHistory();
  const region = await screen.findByRole("region", { name: "Goal history" });
  await within(region).findByText("Goal history (1)");
  const summary = region.querySelector("summary")!;
  expect(summary).toHaveTextContent("09:00");
  fireEvent.click(summary);
  expect(region.querySelector("details")).toHaveAttribute("open");
  expect(within(region).getByText("Last status: Complete")).toBeInTheDocument();
  expect(within(region).getByText("Recovered notification")).toBeInTheDocument();
  expect(within(region).getByText("First recorded")).toBeInTheDocument();
  expect(within(region).getByText("historical-session")).toBeInTheDocument();
  expect(within(region).getByText("Paired experiment completed.")).toBeInTheDocument();
  expect(within(region).queryByRole("button", { name: /Resume|Pause|Start goal|Complete|Clear/ })).not.toBeInTheDocument();
  fireEvent.click(within(region).getByRole("button", { name: "Copy session ID" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("historical-session"));
  expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});

test("loads older goals only when requested", async () => {
  const fetcher = vi.fn((input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(
    String(input).includes("offset=20")
      ? { items: [{ ...entry, goal_key: "older", objective: "Older goal" }], total: 21, next_offset: null }
      : { items: [entry], total: 21, next_offset: 20 },
  ))));
  vi.stubGlobal("fetch", fetcher);
  showHistory();
  fireEvent.click(await screen.findByRole("button", { name: "Load older goals" }));
  await waitFor(() => expect(screen.getAllByText("Older goal")).toHaveLength(2));
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("button", { name: "Load older goals" })).not.toBeInTheDocument();
});

test("keeps empty and failed history states distinct and supports retry", async () => {
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Temporarily unavailable" }), { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ items: [], total: 0, next_offset: null })));
  vi.stubGlobal("fetch", fetcher);
  showHistory();
  expect(await screen.findByRole("alert")).toHaveTextContent("Goal history unavailable: Temporarily unavailable");
  expect(screen.queryByText("No historical goals recorded.")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Refresh goal history" }));
  expect(await screen.findByText("No historical goals recorded.")).toBeInTheDocument();
});
