import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, test, vi } from "vitest";
import NewWorkDialog from "./NewWorkDialog";

const program = {
  id: "program-1", code: "HARN", slug: "harness", name: "Harness",
  objective: "Build harness tasks", color: "#087f5b", sort_order: 10, active: true,
};

test("requires a title and discards form state explicitly", async () => {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const tree = { program, work_items: [], branches: [], sessions: [], edges: [] };
    const body = String(input).endsWith("/repositories") ? [] : tree;
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <NewWorkDialog
        open
        programs={[program]}
        initialProgram="HARN"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />
    </QueryClientProvider>,
  );

  const create = screen.getByRole("button", { name: "Create" });
  const runtime = screen.getByRole("combobox", { name: /Agent runtime/ });
  expect(create).toBeDisabled();
  expect(runtime).toHaveValue("codex");
  expect(screen.getByRole("option", { name: "Claude Code" })).toBeInTheDocument();
  const title = screen.getByRole("textbox", { name: /Title/ });
  fireEvent.change(title, { target: { value: "Draft task" } });
  fireEvent.change(screen.getByRole("textbox", { name: /Objective/ }), {
    target: { value: "A draft objective" },
  });
  fireEvent.change(runtime, { target: { value: "pi" } });
  expect(create).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));

  expect(title).toHaveValue("");
  expect(screen.getByRole("textbox", { name: /Objective/ })).toHaveValue("");
  expect(runtime).toHaveValue("codex");
  expect(create).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
});

test("creates a Pi work item when Pi Agent is selected", async () => {
  let createPayload: Record<string, unknown> | undefined;
  const onCreated = vi.fn();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/work-items") && init?.method === "POST") {
      createPayload = JSON.parse(String(init.body)) as Record<string, unknown>;
      return Promise.resolve(new Response(JSON.stringify({
        id: "work-pi",
        human_id: "HARN-0001",
        program_id: program.id,
        title: "Pi task",
        objective: "Run with Pi",
        kind: "task",
        lifecycle: "ready",
        flow: "actionable",
        priority: 50,
        acceptance_criteria: [],
        labels: [],
        revision: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    const body = url.endsWith("/repositories")
      ? []
      : { program, work_items: [], branches: [], sessions: [], edges: [] };
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <NewWorkDialog
        open
        programs={[program]}
        initialProgram="HARN"
        onClose={vi.fn()}
        onCreated={onCreated}
      />
    </QueryClientProvider>,
  );

  fireEvent.change(screen.getByRole("textbox", { name: /Title/ }), {
    target: { value: "Pi task" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /Objective/ }), {
    target: { value: "Run with Pi" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: /Agent runtime/ }), {
    target: { value: "pi" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create" }));

  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("HARN", "HARN-0001"));
  expect(createPayload).toMatchObject({ runtime: "pi", branch_mode: "write" });
});
