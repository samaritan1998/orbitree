import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import type { BranchExecution, IdentityModels } from "../types";
import ExecutionControls, { ExecutionBadge } from "./ExecutionControls";

const data: BranchExecution = {
  runtime: "codex",
  preferences: {},
  effective: { identity: "api", label: "Company API", model: "gateway-model", reasoning_effort: "max", provider: "gateway", base_url: "https://company.invalid" },
  identities: [
    { id: "api", label: "Company API", ready: true },
    { id: "chatgpt", label: "Personal Codex", ready: true },
    { id: "oneapi", label: "Company OneAPI", ready: true, default_model: "DeepSeek-V4.1-Flash", default_reasoning_effort: "high" },
  ],
  api_isolated: true,
  login_command: "rops auth login",
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const catalogs: Record<string, IdentityModels> = {
  api: { identity: "api", allow_custom: true, models: [{ id: "gateway-model", name: "Gateway model", reasoning_efforts: ["high", "max"], default_reasoning_effort: "max", is_default: true }] },
  chatgpt: { identity: "chatgpt", allow_custom: false, models: [
    { id: "account-model", name: "Account model", reasoning_efforts: ["high", "max", "ultra"], default_reasoning_effort: "high", is_default: true },
    { id: "limited-model", name: "Limited model", reasoning_efforts: ["low", "high"], default_reasoning_effort: "low", is_default: false },
  ] },
  oneapi: { identity: "oneapi", allow_custom: true, models: [
    { id: "DeepSeek-V4.1-Flash", name: "DeepSeek-V4.1-Flash", reasoning_efforts: [], default_reasoning_effort: null, is_default: true },
    { id: "gpt-6-astra", name: "gpt-6-astra", reasoning_efforts: [], default_reasoning_effort: null, is_default: false },
  ] },
};

function show(value = data) {
  vi.spyOn(api, "branchExecution").mockResolvedValue(value);
  const models = vi.spyOn(api, "identityModels").mockImplementation(async (identity) => catalogs[identity]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { ...render(<QueryClientProvider client={client}><ExecutionControls branchId="branch-one" /></QueryClientProvider>), models };
}

test("changing identity requires explicit model and personal context consent", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue({
    ...data, effective: { ...data.effective, identity: "chatgpt", label: "Personal Codex", model: "account-model", reasoning_effort: "high" },
  });
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  await screen.findByRole("option", { name: "Account model" });
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("");
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "account-model" } });
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /personal account/ }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", {
    identity: "chatgpt", model: "account-model", reasoning_effort: "high",
  }));
  expect(await screen.findByText("Saved for the next turn")).toBeInTheDocument();
});

test("cancel does not switch or send a message", async () => {
  const update = vi.spyOn(api, "updateBranchExecution");
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(update).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: /Company API/ })).toBeInTheDocument();
});

test("missing personal login blocks Apply and offers an explicit login command", async () => {
  show({ ...data, identities: [data.identities[0], { id: "chatgpt", label: "Personal Codex", ready: false, reason: "Personal Codex is not signed in" }] });
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  expect(screen.getByText("Personal Codex is not signed in")).toBeInTheDocument();
  expect(screen.getByText("rops auth login")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});

test("execution badge uses the recorded identity, not current preferences", () => {
  render(<ExecutionBadge value={data.effective} />);
  expect(screen.getByText(/Company API.*gateway-model.*max/)).toBeInTheDocument();
});

test("model discovery is lazy and preserves the existing model and effort", async () => {
  const { models } = show();
  const edit = await screen.findByRole("button", { name: /Company API/ });
  expect(models).not.toHaveBeenCalled();
  fireEvent.click(edit);
  await screen.findByRole("option", { name: "Gateway model" });
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gateway-model");
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("max");
});

test("reasoning options follow the selected model, including ultra", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  await screen.findByRole("option", { name: "Account model" });
  const model = screen.getByRole("combobox", { name: "Model" });
  const effort = screen.getByRole("combobox", { name: "Reasoning effort" });
  fireEvent.change(model, { target: { value: "account-model" } });
  expect(screen.getByRole("option", { name: "ultra" })).toBeInTheDocument();
  fireEvent.change(effort, { target: { value: "ultra" } });
  fireEvent.change(model, { target: { value: "limited-model" } });
  expect(effort).toHaveValue("low");
  expect(screen.queryByRole("option", { name: "ultra" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "max" })).not.toBeInTheDocument();
});

test("failed catalog is visible and refresh recovers without changing identity", async () => {
  const { models } = show();
  models.mockRejectedValueOnce(new Error("Model list unavailable"));
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Model list unavailable");
  fireEvent.click(screen.getByRole("button", { name: "Refresh model list" }));
  await screen.findByRole("option", { name: "Gateway model" });
  expect(models).toHaveBeenLastCalledWith("api", true);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Identity")).toHaveValue("api");
});

test("a custom company model remains an explicit fallback", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue(data);
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  await screen.findByRole("option", { name: "Gateway model" });
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "__custom_model__" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Custom model ID" }), { target: { value: "new-gateway-model" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: "max" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", { identity: "api", model: "new-gateway-model", reasoning_effort: "max" }));
});

test("a late response cannot replace another identity's model list", async () => {
  const { models } = show();
  let finishApi!: (value: IdentityModels) => void;
  models.mockImplementation(async (identity) => identity === "api"
    ? new Promise<IdentityModels>((resolve) => { finishApi = resolve; })
    : catalogs.chatgpt);
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  await waitFor(() => expect(models).toHaveBeenCalledWith("api"));
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  await screen.findByRole("option", { name: "Account model" });
  finishApi(catalogs.api);
  await waitFor(() => expect(screen.getByLabelText("Identity")).toHaveValue("chatgpt"));
  expect(screen.queryByRole("option", { name: "Gateway model" })).not.toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Account model" })).toBeInTheDocument();
});

test("unsupported saved effort needs a new explicit choice without saving automatically", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue(data);
  show({ ...data, effective: { ...data.effective, identity: "chatgpt", label: "Personal Codex", model: "limited-model", reasoning_effort: "max" } });
  fireEvent.click(await screen.findByRole("button", { name: /Personal Codex/ }));
  await screen.findByRole("option", { name: "Limited model" });
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("");
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  expect(update).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: "high" } });
  expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
  expect(update).not.toHaveBeenCalled();
});

test("personal catalog failure cannot fall back to a company or custom model", async () => {
  const { models } = show();
  models.mockImplementation(async (identity) => {
    if (identity === "chatgpt") throw new Error("Personal models unavailable");
    return catalogs.api;
  });
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  expect(await screen.findByRole("alert")).toHaveTextContent("Personal models unavailable");
  expect(screen.queryByRole("option", { name: "Gateway model" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Custom model..." })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});

test("OneAPI defaults to DeepSeek and switches only on explicit Apply", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue({
    ...data, effective: { ...data.effective, identity: "oneapi", label: "Company OneAPI", model: "DeepSeek-V4.1-Flash", reasoning_effort: "high" },
  });
  const { models } = show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled());
  expect(models).toHaveBeenCalledWith("oneapi");
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("DeepSeek-V4.1-Flash");
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("high");
  expect(screen.queryByRole("checkbox", { name: /personal account/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Gateway model" })).not.toBeInTheDocument();
  expect(update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", {
    identity: "oneapi", model: "DeepSeek-V4.1-Flash", reasoning_effort: "high",
  }));
  expect(await screen.findByText("Saved for the next turn")).toBeInTheDocument();
});

test("canceling OneAPI preserves the original identity and model", async () => {
  const update = vi.spyOn(api, "updateBranchExecution");
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  fireEvent.click(screen.getByRole("button", { name: /Company API/ }));
  expect(screen.getByLabelText("Identity")).toHaveValue("api");
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gateway-model");
  expect(update).not.toHaveBeenCalled();
});

test.each(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])("OneAPI allows an explicit %s effort without model capability metadata", async (effort) => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue(data);
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled());
  expect(screen.getByRole("option", { name: effort })).toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: effort } });
  expect(update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", {
    identity: "oneapi", model: "DeepSeek-V4.1-Flash", reasoning_effort: effort,
  }));
});

test("OneAPI preserves saved effort and draft effort across unadvertised models", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue(data);
  show({ ...data, effective: { ...data.effective, identity: "oneapi", label: "Company OneAPI", model: "DeepSeek-V4.1-Flash", reasoning_effort: "max" } });
  fireEvent.click(await screen.findByRole("button", { name: /Company OneAPI/ }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toBeEnabled());
  const effort = screen.getByRole("combobox", { name: "Reasoning effort" });
  expect(effort).toHaveValue("max");
  fireEvent.change(effort, { target: { value: "xhigh" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "gpt-6-astra" } });
  expect(effort).toHaveValue("xhigh");
  expect(update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", {
    identity: "oneapi", model: "gpt-6-astra", reasoning_effort: "xhigh",
  }));
});

test("OneAPI uses advertised capabilities when present instead of manual fallback", async () => {
  const { models } = show();
  models.mockImplementation(async (identity) => identity !== "oneapi" ? catalogs[identity] : {
    ...catalogs.oneapi, models: [...catalogs.oneapi.models, {
      id: "limited-oneapi-model", name: "Limited OneAPI model", reasoning_efforts: ["low", "medium"], default_reasoning_effort: "medium", is_default: false,
    }],
  });
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  await screen.findByRole("option", { name: "Limited OneAPI model" });
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: "max" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "limited-oneapi-model" } });
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("medium");
  expect(screen.queryByRole("option", { name: "max" })).not.toBeInTheDocument();
});

test("OneAPI custom models retain a manually chosen effort", async () => {
  const update = vi.spyOn(api, "updateBranchExecution").mockResolvedValue(data);
  show();
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled());
  fireEvent.change(screen.getByRole("combobox", { name: "Reasoning effort" }), { target: { value: "ultra" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "__custom_model__" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Custom model ID" }), { target: { value: "another-oneapi-model" } });
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveValue("ultra");
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("branch-one", {
    identity: "oneapi", model: "another-oneapi-model", reasoning_effort: "ultra",
  }));
});

test("unconfigured OneAPI blocks Apply without using personal or API credentials", async () => {
  const { models } = show({ ...data, identities: data.identities.map((item) => item.id === "oneapi"
    ? { ...item, ready: false, reason: "OneAPI credentials are not configured" } : item) });
  fireEvent.click(await screen.findByRole("button", { name: /Company API/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "oneapi" } });
  expect(screen.getByText("OneAPI credentials are not configured")).toBeInTheDocument();
  expect(screen.queryByText("rops auth login")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  expect(models).not.toHaveBeenCalledWith("oneapi");
});

test("switching away from OneAPI does not carry DeepSeek into Personal Codex", async () => {
  show({ ...data, effective: { ...data.effective, identity: "oneapi", label: "Company OneAPI", model: "DeepSeek-V4.1-Flash", reasoning_effort: "high" } });
  fireEvent.click(await screen.findByRole("button", { name: /Company OneAPI/ }));
  fireEvent.change(screen.getByLabelText("Identity"), { target: { value: "chatgpt" } });
  await screen.findByRole("option", { name: "Account model" });
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("");
  expect(screen.queryByRole("option", { name: "DeepSeek-V4.1-Flash" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});
