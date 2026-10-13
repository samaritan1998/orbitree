import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import type { Approval } from "../types";
import ClaudeApprovals from "./ClaudeApprovals";

const approval: Approval = { id: "permission-1", branch_id: "claude-child", thread_id: "existing-thread", rpc_request_id: "claude:request", method: "claude/tool/requestApproval", status: "pending", params: { tool: "Bash", input: { command: "python inspect.py" }, blocked_path: "/source/reference" }, response: {}, created_at: "2026-10-03T00:00:00Z" };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function show(items = [approval]) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ClaudeApprovals branchId="claude-child" approvals={items} /></QueryClientProvider>);
}

test.each([["Allow once", "accept"], ["Deny", "decline"]])("%s requires an explicit user click", async (label, decision) => {
  const resolve = vi.spyOn(api, "resolveApproval").mockResolvedValue({ ...approval, status: "resolved" });
  show();
  expect(screen.getByText(/python inspect.py/)).toBeInTheDocument();
  expect(screen.getByText("/source/reference")).toBeInTheDocument();
  expect(resolve).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: label }));
  await waitFor(() => expect(resolve).toHaveBeenCalledWith("permission-1", decision));
});

test("expired approvals do not offer an allow button", () => {
  show([{ ...approval, status: "expired" }]);
  expect(screen.queryByRole("button", { name: "Allow once" })).not.toBeInTheDocument();
});

test("a disconnected permission channel is visible without automatic retries", async () => {
  const resolve = vi.spyOn(api, "resolveApproval").mockRejectedValue(new Error("Approval expired"));
  show();
  fireEvent.click(screen.getByRole("button", { name: "Allow once" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Approval expired");
  expect(resolve).toHaveBeenCalledTimes(1);
});
