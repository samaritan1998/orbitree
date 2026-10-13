import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import SessionWorkflowPanel from "./SessionWorkflowPanel";
import type { WorkflowLinkView } from "../types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function show(links: WorkflowLinkView["links"] = []) {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ branch_id: "workflow-test", links }))));
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SessionWorkflowPanel branchId="workflow-test" /></QueryClientProvider>);
  return fetcher;
}

const links: WorkflowLinkView["links"] = ["Training workflow", "Evaluation workflow", "Deployment DAG"].map((title, index) => ({
  title, url: `http://octo.test/#workflow/fixture-${index}`, source: "conversation", record_id: `turn:${index}`, at: null,
}));

test("shows existing workflow links without automatic analysis or any write", async () => {
  const fetcher = show(links);
  const region = await screen.findByRole("region", { name: "Session workflows" });
  const link = within(region).getByRole("link", { name: "Training workflow" });
  expect(link).toHaveAttribute("href", links[0].url);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(within(region).getAllByRole("link")).toHaveLength(2);
  fireEvent.click(within(region).getByRole("button", { name: "Show all workflows" }));
  expect(within(region).getAllByRole("link")).toHaveLength(3);
  expect(within(region).getByRole("button", { name: "Collapse workflows" })).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(within(region).getByRole("button", { name: "Refresh workflow links" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  expect(fetcher.mock.calls.every(([url, init]) => String(url).endsWith("/workflow-links") && !init?.method)).toBe(true);
  expect(screen.queryByText(/Analysis/)).not.toBeInTheDocument();
});

test("no links means no empty strip or background registration", async () => {
  const fetcher = show();
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("region", { name: "Session workflows" })).not.toBeInTheDocument();
  expect(fetcher.mock.calls[0][0]).toBe("/api/branches/workflow-test/workflow-links");
});

test("failed refresh keeps existing links visible with an actionable retry", async () => {
  const fetcher = show(links.slice(0, 1));
  await screen.findByRole("link", { name: "Training workflow" });
  fetcher.mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ detail: "Unavailable" }), { status: 503 })));
  fireEvent.click(screen.getByRole("button", { name: "Refresh workflow links" }));
  expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent("Unavailable");
  expect(screen.getByRole("link", { name: "Training workflow" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh workflow links" })).toBeEnabled();
});
