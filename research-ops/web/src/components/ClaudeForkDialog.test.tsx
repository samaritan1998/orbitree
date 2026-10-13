import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import ClaudeForkDialog from "./ClaudeForkDialog";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function setup(status = 200) {
  const fetchMock = vi.fn((_input: RequestInfo | URL) => Promise.resolve(new Response(JSON.stringify(status === 200 ? {
    handoff_hash: "frozen", turn_count: 6, prompt: "Approved source conversation", account: "Personal Claude", model: "sonnet", effort: "high",
  } : { detail: "Preview unavailable" }), { status, headers: { "Content-Type": "application/json" } })));
  vi.stubGlobal("fetch", fetchMock);
  const onSubmit = vi.fn();
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ClaudeForkDialog branchId="source" open submitting={false} onClose={vi.fn()} onSubmit={onSubmit} />
  </QueryClientProvider>);
  return { fetchMock, onSubmit };
}

test("previews the personal runtime and freezes the approved handoff only on confirmation", async () => {
  const { fetchMock, onSubmit } = setup();
  expect(await screen.findByText("Personal Claude · sonnet · high")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "First-turn context" })).toHaveValue("Approved source conversation");
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Create fork" }));
  expect(onSubmit).toHaveBeenCalledWith({ include_context: true, expected_handoff_hash: "frozen" });
  expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/claude-handoff");
});

test("supports an explicit context-free fork", async () => {
  const { onSubmit } = setup();
  await screen.findByText("Personal Claude · sonnet · high");
  fireEvent.click(screen.getByRole("checkbox"));
  expect(screen.queryByRole("textbox", { name: "First-turn context" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create fork" }));
  expect(onSubmit).toHaveBeenCalledWith({ include_context: false, expected_handoff_hash: undefined });
});

test("does not create a fork when the preview fails", async () => {
  const { onSubmit } = setup(503);
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Preview unavailable"));
  expect(screen.getByRole("button", { name: "Create fork" })).toBeDisabled();
  expect(onSubmit).not.toHaveBeenCalled();
});
