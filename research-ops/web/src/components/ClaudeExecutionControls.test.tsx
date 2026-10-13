import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import type { ClaudeBranchExecution } from "../types";
import ClaudeExecutionControls from "./ClaudeExecutionControls";

const data: ClaudeBranchExecution = {
  runtime: "claude", preferences: {},
  effective: { identity: "claude", label: "Personal Claude", provider: "claude-cli", model: "sonnet", reasoning_effort: "high" },
  models: [{ id: "sonnet", name: "Sonnet" }, { id: "opus", name: "Opus" }, { id: "haiku", name: "Haiku" }],
  reasoning_efforts: ["low", "medium", "high", "xhigh", "max"],
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function show(value: ClaudeBranchExecution | Error = data) {
  const load = vi.spyOn(api, "claudeBranchExecution");
  if (value instanceof Error) load.mockRejectedValue(value);
  else load.mockResolvedValue(value);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ClaudeExecutionControls branchId="claude-one" /></QueryClientProvider>);
  return client;
}

test("model and effort dropdowns persist only the selected Branch's next turn", async () => {
  const update = vi.spyOn(api, "updateClaudeBranchExecution").mockResolvedValue({ ...data, effective: { ...data.effective, model: "opus", reasoning_effort: "max" } });
  const send = vi.spyOn(api, "sendMessage");
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Personal Claude/ }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("sonnet");
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("high");
  expect(screen.queryByRole("combobox", { name: "Identity" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "opus" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: "max" } });
  expect(update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("claude-one", { model: "opus", reasoning_effort: "max" }));
  expect(await screen.findByText("Saved for the next turn")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Personal Claude.*opus.*max/ })).toBeInTheDocument();
  expect(send).not.toHaveBeenCalled();
});

test("cancel discards unsaved selections", async () => {
  const update = vi.spyOn(api, "updateClaudeBranchExecution");
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Personal Claude/ }));
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "opus" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: /Personal Claude/ }));
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("sonnet");
  expect(update).not.toHaveBeenCalled();
});

test("custom model IDs and saved efforts are preserved", async () => {
  const update = vi.spyOn(api, "updateClaudeBranchExecution").mockResolvedValue(data);
  show({ ...data, effective: { ...data.effective, model: "claude-opus-custom", reasoning_effort: "xhigh" } });
  fireEvent.click(await screen.findByRole("button", { name: /Personal Claude/ }));
  expect(screen.getByRole("textbox", { name: "Custom model ID" })).toHaveValue("claude-opus-custom");
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("xhigh");
  fireEvent.change(screen.getByRole("textbox", { name: "Custom model ID" }), { target: { value: "" } });
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: "Custom model ID" }), { target: { value: "claude-opus-test" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("claude-one", { model: "claude-opus-test", reasoning_effort: "xhigh" }));
});

test("save failure leaves the dialog and original selection intact", async () => {
  vi.spyOn(api, "updateClaudeBranchExecution").mockRejectedValue(new Error("Save failed"));
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Personal Claude/ }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Personal Claude.*sonnet.*high/ })).toBeInTheDocument();
});

test("loading failure offers a retry", async () => {
  show(new Error("Offline"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Claude settings unavailable");
  vi.mocked(api.claudeBranchExecution).mockResolvedValue(data);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("button", { name: /Personal Claude/ })).toBeInTheDocument();
});
