import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import SessionsView from "./SessionsView";

const program = {
  id: "program-free",
  code: "FREE",
  slug: "free",
  name: "Free Work",
  objective: "Explore",
  color: "#a21caf",
  sort_order: 50,
  active: true,
};
const sessionWindow = {
  id: "window-1",
  thread_id: "019f59c9-a3fb-7512-a6c6-661ff6d8c42a",
  display_name: "Build trajectory verifier",
  title_source: "ai",
  cwd: "/srv/research/experiment",
  cli_version: "0.144.3",
  originator: "codex-tui",
  pid: 4242,
  tty: "/dev/pts/12",
  live: true,
  state: "unassigned",
  can_attach_to_parent: false,
  child_count: 0,
  match_confidence: "exact_cwd",
  ignored: false,
  started_at: "2026-07-14T10:00:00Z",
  last_activity_at: "2026-07-14T10:05:00Z",
  resume_command: "codex -C /srv/research/experiment resume 019f59c9-a3fb-7512-a6c6-661ff6d8c42a",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("registers a discovered session only after explicit work confirmation", async () => {
  const onOpenWork = vi.fn();
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let payload: unknown = [sessionWindow];
    if (url.endsWith("/attach-targets")) payload = [];
    if (init?.method === "PATCH" && url.endsWith("/title")) {
      payload = { ...sessionWindow, display_name: "PPT trajectory verifier", title_source: "manual" };
    }
    if (init?.method === "POST" && url.endsWith("/create-work")) {
      payload = {
        window: {
          ...sessionWindow,
          state: "managed",
          program_code: "FREE",
          work_id: "FREE-0003",
          branch_id: "branch-3",
        },
        work_item: {
          id: "work-3",
          human_id: "FREE-0003",
          program_id: program.id,
          title: "Codex session · experiment",
          objective: "",
          kind: "task",
          lifecycle: "active",
          flow: "running",
          priority: 50,
          acceptance_criteria: [],
          labels: ["imported_codex_session", "needs_definition"],
          revision: 1,
          created_at: "2026-07-14T10:06:00Z",
          updated_at: "2026-07-14T10:06:00Z",
        },
        branch: {
          id: "branch-3",
          work_item_id: "work-3",
          name: "Main",
          slug: "main",
          status: "active",
          mode: "write",
          cwd: sessionWindow.cwd,
          context_snapshot: {},
          memory_cursor: 0,
          created_at: "2026-07-14T10:06:00Z",
          updated_at: "2026-07-14T10:06:00Z",
        },
      };
    }
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SessionsView programs={[program]} onOpenBranch={() => {}} onOpenWork={onOpenWork} />
    </QueryClientProvider>,
  );

  expect(await screen.findByRole("heading", { name: "Sessions" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("live");
  expect(screen.getByText("Unassigned")).toBeInTheDocument();
  expect(screen.getByText("Build trajectory verifier")).toBeInTheDocument();
  expect(screen.getByText("PID 4242 · /dev/pts/12")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Copy resume command" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(sessionWindow.resume_command));

  fireEvent.click(screen.getByRole("button", { name: "Rename Build trajectory verifier" }));
  const renameDialog = await screen.findByRole("dialog");
  fireEvent.change(within(renameDialog).getByRole("textbox", { name: "Window name" }), {
    target: { value: "PPT trajectory verifier" },
  });
  fireEvent.click(within(renameDialog).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => (
    String(url).endsWith("/title") && (init as RequestInit | undefined)?.method === "PATCH"
  ))).toBe(true));

  fireEvent.click(screen.getByRole("button", { name: "Attach Branch" }));
  expect(await screen.findByText("No compatible Branch")).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "Compatible Branch" })).not.toBeInTheDocument();
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Attach to Program" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Attach Session to Program");
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

  fireEvent.click(screen.getByRole("button", { name: "Attach to Program" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("directory and its files stay in place");
  expect(fetchMock.mock.calls.filter(([url, init]) => (
    String(url).endsWith("/create-work") && (init as RequestInit | undefined)?.method === "POST"
  ))).toHaveLength(0);
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Attach to Program" }));

  await waitFor(() => expect(onOpenWork).toHaveBeenCalledWith("FREE", "FREE-0003"));
  expect(fetchMock.mock.calls.some(([url, init]) => (
    String(url).endsWith("/create-work") && (init as RequestInit | undefined)?.method === "POST"
  ))).toBe(true);
});

test("shows Codex fork lineage and attaches a child under its managed parent", async () => {
  const parent = {
    ...sessionWindow,
    id: "window-parent",
    thread_id: "thread-parent-1234567890",
    pid: undefined,
    tty: undefined,
    live: false,
    state: "managed",
    child_count: 1,
    branch_id: "branch-parent",
    branch_name: "Main",
    work_id: "HARN-0001",
    work_title: "Harness task",
    program_code: "HARN",
    display_name: "Harness main",
    title_source: "work",
  };
  const child = {
    ...sessionWindow,
    id: "window-child",
    thread_id: "thread-child-1234567890",
    parent_thread_id: parent.thread_id,
    parent_window_id: parent.id,
    parent_work_id: "HARN-0001",
    parent_program_code: "HARN",
    can_attach_to_parent: true,
    child_count: 0,
    cwd: "/srv/research/fork",
    display_name: "Harness fork",
    title_source: "work",
  };
  const adoption = {
    window: {
      ...child,
      state: "managed",
      branch_id: "branch-child",
      branch_name: "Codex fork",
      work_id: "HARN-0001",
      work_title: "Harness task",
      program_code: "HARN",
      can_attach_to_parent: false,
    },
    work_item: {
      id: "work-harn",
      human_id: "HARN-0001",
      program_id: "program-harn",
      title: "Harness task",
      objective: "Test",
      kind: "task",
      lifecycle: "active",
      flow: "actionable",
      priority: 50,
      acceptance_criteria: [],
      labels: [],
      revision: 1,
      created_at: "2026-07-14T10:00:00Z",
      updated_at: "2026-07-14T10:00:00Z",
    },
    branch: {
      id: "branch-child",
      work_item_id: "work-harn",
      parent_branch_id: "branch-parent",
      name: "Codex fork",
      slug: "codex-child",
      status: "active",
      mode: "write",
      cwd: child.cwd,
      context_snapshot: {},
      memory_cursor: 0,
      created_at: "2026-07-14T10:00:00Z",
      updated_at: "2026-07-14T10:00:00Z",
    },
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const payload = init?.method === "POST" && url.endsWith("/attach-parent")
      ? adoption
      : [parent, child];
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const onOpenBranch = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SessionsView programs={[program]} onOpenBranch={onOpenBranch} onOpenWork={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByLabelText("Codex session lineage")).toBeInTheDocument();
  expect(screen.getByText(/Root Thread · 1 child/)).toBeInTheDocument();
  expect(screen.getByText(/Fork of thread-par/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Attach under HARN-0001" }));

  await waitFor(() => expect(onOpenBranch).toHaveBeenCalledWith("HARN", "branch-child"));
  expect(fetchMock.mock.calls.some(([url, init]) => (
    String(url).endsWith("/attach-parent") && (init as RequestInit | undefined)?.method === "POST"
  ))).toBe(true);
});
