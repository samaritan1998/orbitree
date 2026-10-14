import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

// Shell tests cover navigation; conversation and graph rendering run separately.
vi.mock("./components/BranchPanel", () => ({
  default: ({ branchId }: { branchId: string }) => (
    <section aria-label="Test branch panel" data-branch-id={branchId} />
  ),
}));
vi.mock("./components/TreeCanvas", () => ({ default: () => <div /> }));

const freeProgram = {
  id: "program-free", code: "FREE", slug: "free", name: "Free Work",
  objective: "Explore", color: "#a21caf", sort_order: 50, active: true,
};
const quickWork = {
  id: "work-quick", human_id: "FREE-0001", program_id: "program-free",
  title: "未命名工作 FREE-0001", objective: "", kind: "task", lifecycle: "ready",
  flow: "actionable", priority: 50, acceptance_criteria: [], labels: ["needs_definition"], revision: 1,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const quickBranch = {
  id: "branch-quick", work_item_id: quickWork.id, name: "Main", slug: "main",
  status: "active", mode: "write", cwd: "/tmp", context_snapshot: {}, memory_cursor: 0,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const deskWork = {
  ...quickWork,
  id: "work-desk",
  human_id: "FREE-DESK",
  title: "Free Work Desk",
  objective: "Explore",
  kind: "program_desk",
  labels: ["internal", "program_desk"],
};
const deskBranch = {
  ...quickBranch,
  id: "branch-desk",
  work_item_id: deskWork.id,
  name: "Program Desk",
  slug: "desk",
  cwd: "/tmp/FREE/_desk",
};

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
class TestWebSocket {
  close() {}
}

vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  if (init?.method === "DELETE" && url.endsWith("/work-items/work-quick")) {
    return new Response(JSON.stringify({ ...quickWork, hidden: true, lifecycle: "cancelled" }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.endsWith("/work-items")) {
    return new Response(JSON.stringify(quickWork), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.endsWith("/programs")) {
    return new Response(JSON.stringify([freeProgram]), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.endsWith("/programs/FREE/desk")) {
    return new Response(JSON.stringify({
      branch: deskBranch, work_item: deskWork, turns: [], messages: [], memory: [],
      external_jobs: [], goals: [], approvals: [], activity: [],
      conversation_state: { can_send: true, web_turn_status: "idle", local_turn_active: false },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.endsWith("/programs/FREE/tree")) {
    return new Response(JSON.stringify({
      program: freeProgram,
      work_items: [quickWork],
      branches: [quickBranch],
      sessions: [], edges: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (url.endsWith("/branches/branch-quick")) {
    return new Response(JSON.stringify({
      branch: quickBranch, work_item: quickWork, turns: [], messages: [], memory: [],
      external_jobs: [], goals: [], approvals: [], activity: [],
      conversation_state: { can_send: true, web_turn_status: "idle", local_turn_active: false },
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  const body = ["/repositories", "/memory", "/daily-reflections", "/skills", "/skill-proposals"].some((path) => url.endsWith(path)) ? [] : {
    continue_items: [{
      id: "work-1", human_id: "HARN-0001", program_id: "program-1",
      title: "Verifier task", objective: "Verify", kind: "task", lifecycle: "active",
      flow: "actionable", priority: 80, acceptance_criteria: [], labels: [], revision: 1,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
    }],
    needs_decision: [],
    waiting_external: [],
    suggested_next: [],
    active_sessions: 0,
    pending_merges: 0,
  };
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}));
vi.stubGlobal("ResizeObserver", TestResizeObserver);
vi.stubGlobal("WebSocket", TestWebSocket);

beforeEach(() => {
  cleanup();
  window.localStorage.clear();
  window.history.replaceState({}, "", "/today");
  document.title = "Orbitree";
});

test("renders the operational shell", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}><App /></QueryClientProvider>,
  );
  expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
  await waitFor(() => expect(document.title).toBe("今日 | Orbitree"));
  expect(screen.getAllByRole("button", { name: "New work" })).toHaveLength(2);
  const workRow = screen.getByRole("button", { name: "Open HARN-0001: Verifier task" });
  expect(workRow.querySelector("button")).toBeNull();
  const createScratch = screen.getByRole("button", { name: "Create scratch work" });
  expect(createScratch).toHaveAttribute("aria-describedby", "scratch-work-description");
  fireEvent.click(createScratch);
  await waitFor(() => expect(window.location.pathname).toBe("/programs/FREE/work/FREE-0001/results"));
  await waitFor(() => expect(document.title).toBe("FREE-0001 · 未命名工作 FREE-0001 | Orbitree"));
  expect(await screen.findByRole("status")).toHaveTextContent("Created FREE-0001");
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => expect(window.location.pathname).toBe("/today"));
  await waitFor(() => expect(screen.queryByText("Created FREE-0001")).not.toBeInTheDocument());
});

test("canonicalizes an invalid URL to Today", async () => {
  window.history.replaceState({}, "", "/this-route-does-not-exist");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  expect(await screen.findByRole("heading", { name: "Today" })).toBeInTheDocument();
  await waitFor(() => expect(window.location.pathname).toBe("/today"));
  expect(screen.getAllByRole("button", { name: "今日" })[0]).toHaveAttribute("aria-current", "page");
});

test("keeps Results primary and lets a Branch reveal or hide its navigator", async () => {
  window.history.replaceState({}, "", "/programs/FREE/branches/branch-quick/results");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  const showNavigator = await screen.findByRole("button", { name: "Show branch navigator" });
  expect(screen.getByRole("region", { name: "Test branch panel" })).toHaveAttribute("data-branch-id", "branch-quick");
  expect(showNavigator).toHaveAttribute("aria-pressed", "false");
  expect(screen.queryByRole("region", { name: "Program branch navigator" })).not.toBeInTheDocument();

  fireEvent.click(showNavigator);
  expect(await screen.findByRole("region", { name: "Program branch navigator" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Hide branch navigator" })).toHaveAttribute("aria-pressed", "true");
  expect(window.localStorage.getItem("research-ops:program-navigator:FREE")).toBe("open");

  fireEvent.click(screen.getByRole("button", { name: "Hide branch navigator" }));
  expect(screen.queryByRole("region", { name: "Program branch navigator" })).not.toBeInTheDocument();
  expect(window.localStorage.getItem("research-ops:program-navigator:FREE")).toBe("closed");
});

test("collapses and restores the desktop sidebar without losing navigation", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  const shell = document.querySelector<HTMLElement>(".app-shell");
  const sidebar = document.querySelector<HTMLElement>(".sidebar");
  if (!shell || !sidebar) throw new Error("App shell did not render");
  expect(shell).not.toHaveClass("sidebar-collapsed");
  const collapse = await screen.findByRole("button", { name: "Collapse sidebar" });
  expect(within(sidebar).getByRole("button", { name: "今日" })).toBeInTheDocument();

  fireEvent.click(collapse);
  expect(shell).toHaveClass("sidebar-collapsed");
  expect(window.localStorage.getItem("research-ops:sidebar")).toBe("collapsed");
  expect(screen.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-pressed", "true");
  expect(within(sidebar).getByRole("button", { name: "今日" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
  expect(shell).not.toHaveClass("sidebar-collapsed");
  expect(window.localStorage.getItem("research-ops:sidebar")).toBe("expanded");
});

test("switches the background ambience and persists the choice", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);

  const shell = document.querySelector<HTMLElement>(".app-shell");
  if (!shell) throw new Error("App shell did not render");
  expect(shell).toHaveAttribute("data-ambience", "observatory");

  fireEvent.click(screen.getAllByRole("button", { name: /Choose background theme/ })[0]);
  fireEvent.click(await screen.findByRole("menuitemradio", { name: "Person of Interest" }));

  expect(shell).toHaveAttribute("data-ambience", "person-of-interest");
  expect(window.localStorage.getItem("research-ops:ambience")).toBe("person-of-interest");

  cleanup();
  const reloadedClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={reloadedClient}><App /></QueryClientProvider>);
  expect(document.querySelector<HTMLElement>(".app-shell")).toHaveAttribute("data-ambience", "person-of-interest");
});

test("restores advanced views from deep links and browser history", async () => {
  window.history.replaceState({}, "", "/memory?view=ledger");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  expect(await screen.findByRole("heading", { name: "记忆账本" })).toBeInTheDocument();
  expect(window.location.search).toBe("?view=ledger");
  fireEvent.click(screen.getAllByRole("button", { name: "每日复盘" })[0]);
  expect(await screen.findByRole("heading", { name: "每日复盘" })).toBeInTheDocument();
  expect(window.location.search).toBe("");

  window.history.replaceState({}, "", "/skills?view=proposals");
  fireEvent.popState(window);
  expect(await screen.findByRole("heading", { name: "技能审核" })).toBeInTheDocument();
  await waitFor(() => expect(document.title).toBe("技能审核 | Orbitree"));
  expect(window.location.search).toBe("?view=proposals");
});
