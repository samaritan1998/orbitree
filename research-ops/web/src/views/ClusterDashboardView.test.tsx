import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ClusterOverview } from "../types";
import ClusterDashboardView, { filterClusters, formatDuration } from "./ClusterDashboardView";

const overview: ClusterOverview = {
  schema_version: 1,
  mode: "shadow",
  observed_at: "2026-08-20T10:00:00Z",
  resources_observed_at: "2026-08-20T10:00:00Z",
  jobs_observed_at: "2026-08-20T10:00:02Z",
  source: {
    kind: "live",
    fetched_at: "2026-08-20T10:00:03Z",
    cache_hit: false,
    degraded: false,
    stale: false,
    age_seconds: 1,
  },
  coverage: {
    source: "ltp_user_directory",
    complete: true,
    requested_users: 129,
    successful_users: 129,
    failed_users: 0,
    failed_user_names: [],
  },
  summary: {
    clusters: 1,
    resource_pools: 1,
    resource_domains: 1,
    available_cards: 32,
    free_nodes: 4,
    running_jobs: 1,
    queued_jobs: 1,
    running_nodes: 4,
    queued_nodes: 1,
    profiled_jobs: 1,
    estimated_jobs: 1,
    checkpoint_interval_jobs: 1,
  },
  categories: {
    large_training: { running: 1, queued: 0 },
    unknown: { running: 0, queued: 1 },
  },
  clusters: [{
    name: "cluster-a",
    resource: {
      name: "cluster-a",
      resource_pool: "physical-pool-a",
      resource_domain: "physical-pool-a",
      hardware: "B",
      scheduling_class: "regular",
      quota_cards: 64,
      used_cards: 32,
      available_cards: 32,
      gpus_per_node: 8,
      free_nodes: 4,
      fragment_cards: 0,
      utilization_pct: 50,
      stable_window_seconds: 3600,
      volatility: 0.2,
      forecast_source: "history",
      forecast_samples: 24,
      available_formula: "max(quota - used, 0)",
    },
    running_jobs: [{
      id: "user-a/glm-train-a",
      name: "glm-train-a",
      user: "user-a",
      cluster: "cluster-a",
      state: "running",
      provider_status: "running",
      category: "large_training",
      nodes: 4,
      worker_nodes: 3,
      age: "2h",
      profiled: true,
      profile_rule: "glm-training",
      eta_kind: "remaining",
      eta_seconds: 7200,
      estimate_confidence: 0.78,
      checkpoint_interval_seconds: 300,
      checkpoint_source: "scheduler_decision",
      next_checkpoint_at: "2099-08-20T10:03:00Z",
    }],
    queued_jobs: [{
      id: "user-b/opaque-job",
      name: "opaque-job",
      user: "user-b",
      cluster: "cluster-a",
      state: "queued",
      provider_status: "created",
      category: "unknown",
      nodes: 1,
      worker_nodes: 0,
      age: "8m",
      profiled: false,
      blocked_reason: "No explicit profile",
      eta_kind: null,
      eta_seconds: null,
      estimate_confidence: null,
      checkpoint_interval_seconds: null,
      next_checkpoint_at: null,
    }],
    running_nodes: 4,
    queued_nodes: 1,
    running_nodes_known_jobs: 1,
    queued_nodes_known_jobs: 1,
    profiled_jobs: 1,
  }],
  warnings: [],
};

test("renders all-user jobs and exposes unknown estimate coverage", async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(
    JSON.stringify(overview),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ClusterDashboardView /></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "Cluster Control" })).toBeInTheDocument();
  expect(screen.getByText("glm-train-a")).toBeInTheDocument();
  expect(screen.getByText("opaque-job")).toBeInTheDocument();
  expect(screen.getByText("Awaiting profile")).toBeInTheDocument();
  expect(screen.getByText("5m")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("tab", { name: /Queued/ }));
  expect(screen.queryByText("glm-train-a")).not.toBeInTheDocument();
  expect(screen.getByText("opaque-job")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/clusters/overview?refresh=true",
    expect.objectContaining({ cache: "no-store" }),
  ));
});

test("filters jobs without dropping a resource-only cluster by default", () => {
  expect(filterClusters(overview, {
    status: "all",
    cluster: "all",
    category: "all",
    search: "USER-B",
  })[0].queued_jobs).toHaveLength(1);
  expect(filterClusters(overview, {
    status: "all",
    cluster: "all",
    category: "inference",
    search: "",
  })).toHaveLength(0);
  expect(formatDuration(7260)).toBe("2h 1m");
});
