import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import EvalHarnessView from "./EvalHarnessView";

const run = {
  id: "run-1",
  work_item_id: "work-1",
  name: "PresentBench R1",
  adapter: "presentbench",
  mode: "fast",
  status: "running",
  candidate_id: "candidate-r1",
  config_hash: "a".repeat(64),
  config_json: {},
  source_uri: "/data/tasks.jsonl",
  artifact_uri: "/data/results",
  tool_parser: true,
  batch_size: 16,
  max_attempts: 3,
  expected_units: 17492,
  started_at: "2026-08-14T00:00:00Z",
  created_at: "2026-08-14T00:00:00Z",
  updated_at: "2026-08-14T00:10:00Z",
};

const summary = {
  total_units: 17492,
  expected_units: 17492,
  total_cases: 238,
  status_counts: { succeeded: 1200, pending: 16292 },
  error_counts: {},
  completed_units: 1200,
  quarantined_units: 0,
  active_workers: 8,
  cache_hits: 100,
  attempts: 1300,
  completion_ratio: 1200 / 17492,
  mean_score: 0.7,
  aggregate_score: undefined,
  aggregate_cases: 0,
  throughput_units_per_minute: 25,
  eta_seconds: 39000,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("creates a fast PresentBench run with tool parser enabled", async () => {
  let createdPayload: Record<string, unknown> | undefined;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/eval-runs") && init?.method === "POST") {
      createdPayload = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ ...run, id: "run-2", name: "New run" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/import-presentbench")) {
      return new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (url.includes("/audit")) {
      return new Response(JSON.stringify({ run_id: "run-1", valid: true, issues: [], summary }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify([{ run, summary }]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><EvalHarnessView /></QueryClientProvider>);

  expect(await screen.findByText("parser on")).toBeInTheDocument();
  expect(screen.getByText(/1,200/)).toHaveTextContent("17,492");
  fireEvent.click(screen.getByRole("button", { name: "New run" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/Work item/), { target: { value: "HARN-0004" } });
  fireEvent.change(within(dialog).getByLabelText(/Run name/), { target: { value: "New run" } });
  fireEvent.change(within(dialog).getByLabelText(/Candidate ID/), { target: { value: "candidate-r2" } });
  fireEvent.change(within(dialog).getByLabelText(/Task JSONL/), { target: { value: "/data/tasks.jsonl" } });
  fireEvent.change(within(dialog).getByLabelText(/Result root/), { target: { value: "/data/results" } });
  expect(within(dialog).getByRole("checkbox", { name: "Tool parser" })).toBeChecked();
  fireEvent.click(within(dialog).getByRole("button", { name: "Create and start" }));

  await waitFor(() => expect(createdPayload).toBeDefined());
  expect(createdPayload).toMatchObject({
    work_item_id: "HARN-0004",
    mode: "fast",
    tool_parser: true,
    batch_size: 16,
  });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/eval-runs/run-2/import-presentbench",
    expect.objectContaining({ method: "POST" }),
  ));
});
