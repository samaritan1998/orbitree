import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import type { Approval } from "../types";
import CodexQuestions from "./CodexQuestions";

const approval: Approval = {
  id: "question-1", branch_id: "branch-1", thread_id: "thread-1", rpc_request_id: "1",
  method: "item/tool/requestUserInput", status: "pending", response: {}, created_at: "2026-10-11T00:00:00Z",
  params: { questions: [
    { id: "target", header: "Target", question: "Which model?", options: [
      { label: "A", description: "Fast" }, { label: "B", description: "Accurate" },
    ] },
    { id: "note", header: "Note", question: "Any extra detail?", options: null },
  ] },
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test("Codex questions require answers and send the protocol-shaped response", async () => {
  const resolve = vi.spyOn(api, "resolveApproval").mockResolvedValue({ ...approval, status: "resolved" });
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><CodexQuestions branchId="branch-1" approvals={[approval]} /></QueryClientProvider>);
  const send = screen.getByRole("button", { name: "Send answer to Codex" });
  expect(screen.getByText(/Target · Which model\?/)).toBeInTheDocument();
  expect(send).toBeDisabled();
  fireEvent.click(screen.getByRole("radio", { name: /A Fast/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "Answer for Any extra detail?" }), { target: { value: "A short note" } });
  expect(send).toBeEnabled();
  fireEvent.click(send);
  await waitFor(() => expect(resolve).toHaveBeenCalledWith("question-1", {
    answers: { target: { answers: ["A"] }, note: { answers: ["A short note"] } },
  }));
});

test("expired questions are not offered for answering", () => {
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><CodexQuestions branchId="branch-1" approvals={[{ ...approval, status: "expired" }]} /></QueryClientProvider>);
  expect(screen.queryByRole("button", { name: "Send answer to Codex" })).not.toBeInTheDocument();
});
