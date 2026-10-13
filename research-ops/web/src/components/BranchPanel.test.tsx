import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import BranchPanel from "./BranchPanel";
import type { BranchDetail, NativeGoalState, Program } from "../types";

class TestWebSocket {
  static latest: TestWebSocket | undefined;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor() { TestWebSocket.latest = this; }
  close() {}
}

const program: Program = {
  id: "program-1",
  code: "HARN",
  slug: "harness",
  name: "Harness",
  objective: "Build harness tasks",
  color: "#28704f",
  sort_order: 1,
  active: true,
};

const detail: BranchDetail = {
  branch: {
    id: "branch-1",
    work_item_id: "work-1",
    name: "Main",
    slug: "main",
    status: "active",
    mode: "thread",
    cwd: "/tmp/work",
    context_snapshot: {},
    memory_cursor: 0,
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
  },
  work_item: {
    id: "work-1",
    human_id: "HARN-0001",
    program_id: "program-1",
    title: "PPT trajectory evaluation",
    objective: "Evaluate PPT trajectories",
    kind: "task",
    lifecycle: "active",
    flow: "actionable",
    priority: 80,
    acceptance_criteria: [],
    labels: [],
    revision: 1,
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
  },
  session: {
    id: "session-1",
    branch_id: "branch-1",
    status: "active",
    lease_id: "lease-1",
    lease_owner: "web",
    lease_expires_at: "2999-01-01T00:00:00Z",
    heartbeat_at: "2026-07-14T00:00:00Z",
    brief: {},
    handoff: {},
  },
  turns: [],
  messages: [],
  memory: [],
  external_jobs: [],
  goals: [],
  approvals: [],
  activity: [],
  conversation_state: {
    can_send: true,
    web_turn_status: "idle",
    local_turn_active: false,
  },
};

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

test("shows a single-turn Web composer in the results view", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(detail), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("No completed Codex turns yet")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Send one Web turn")).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "results" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "brief" })).toHaveAttribute("aria-selected", "false");
});

test("shows a pending Codex question on the Branch results page", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const waiting: BranchDetail = {
    ...detail,
    approvals: [{
      id: "question-1", branch_id: "branch-1", thread_id: "thread-1", rpc_request_id: "1",
      method: "item/tool/requestUserInput", status: "pending", response: {},
      created_at: "2026-10-11T00:00:00Z",
      params: { questions: [{ id: "choice", header: "Choice", question: "Which test?", options: [
        { label: "Fast", description: "Run a smoke test" },
        { label: "Full", description: "Run the whole suite" },
      ] }] },
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(waiting), {
    status: 200, headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><BranchPanel branchId="branch-1" program={program}
    onTabChange={() => {}} onClose={() => {}} /></QueryClientProvider>);
  expect(await screen.findByText(/Choice · Which test\?/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send answer to Codex" })).toBeDisabled();
});

test("shows Pi steer, follow-up, and abort controls only while Pi is running", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const running: BranchDetail = {
    ...detail,
    runtime: "pi",
    conversation_state: {
      can_send: false,
      can_interrupt: true,
      web_turn_status: "running",
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.endsWith("/runtime-state")
      ? {
          runtime: "pi",
          state: "running",
          capabilities: ["prompt", "steer", "follow_up", "abort"],
          session_id: "pi-session-1",
          owner: "web",
          queue: { steering: [], follow_up: [] },
        }
      : url.endsWith("/conversation-state")
        ? running.conversation_state
        : running;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByLabelText("Pi runtime controls")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Message to Pi")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Steer now" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Queue next" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Abort" })).toBeEnabled();
  expect(screen.queryByPlaceholderText("Send one Web turn")).not.toBeInTheDocument();
});

test("keeps an idle Pi branch on Web and hides the Codex local launcher", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const idlePi: BranchDetail = {
    ...detail,
    runtime: "pi",
    session: { ...detail.session!, status: "completed" },
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.endsWith("/runtime-state")
      ? {
          runtime: "pi",
          state: "settled",
          capabilities: ["prompt", "steer", "follow_up", "abort"],
          session_id: "pi-session-1",
          owner: "web",
          queue: { steering: [], follow_up: [] },
        }
      : url.endsWith("/conversation-state")
        ? idlePi.conversation_state
        : idlePi;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("No completed Pi turns yet")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Send one Web turn")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Start locally|Continue locally/ })).not.toBeInTheDocument();
});

test("creates a parallel Pi fork from an idle completed Codex branch", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const codexParent: BranchDetail = {
    ...detail,
    branch: { ...detail.branch, codex_thread_id: "codex-parent-thread" },
    session: {
      ...detail.session!,
      codex_thread_id: "codex-parent-thread",
      status: "completed",
      lease_expires_at: "2026-07-14T00:00:00Z",
    },
    turns: [turnForBranch()],
  };
  const piChild = {
    ...codexParent.branch,
    id: "pi-child-1",
    parent_branch_id: "branch-1",
    name: "Pi · Verifier experiment",
    runtime: "pi" as const,
    codex_thread_id: "pi-child-session",
  };
  const requests: Array<{ url: string; body?: string }> = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({
      url,
      body: typeof init?.body === "string" ? init.body : undefined,
    });
    const payload = url.endsWith("/continue-with-pi")
      ? piChild
      : url.endsWith("/conversation-state")
        ? codexParent.conversation_state
        : codexParent;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));
  const onOpenBranch = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
        onOpenBranch={onOpenBranch}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Fork with Pi" }));

  await waitFor(() => expect(requests.some((request) => (
    request.url.endsWith("/continue-with-pi")
  ))).toBe(true));
  const piRequest = requests.find((request) => request.url.endsWith("/continue-with-pi"));
  expect(JSON.parse(piRequest?.body ?? "{}")).toMatchObject({
    manual_approval: true,
    continuation: "parallel",
  });
  expect(screen.getByText("Created a parallel Pi fork with a frozen Codex handoff. The Codex Branch remains available.")).toBeInTheDocument();
  expect(onOpenBranch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Open Pi fork" }));
  expect(onOpenBranch).toHaveBeenCalledWith("pi-child-1");
});

test("creates a parallel Codex fork from an idle completed Pi branch", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const piParent: BranchDetail = {
    ...detail,
    runtime: "pi",
    branch: { ...detail.branch, codex_thread_id: "pi-parent-session" },
    session: {
      ...detail.session!,
      runtime: "pi",
      codex_thread_id: "pi-parent-session",
      status: "completed",
      lease_expires_at: "2026-07-14T00:00:00Z",
    },
    turns: [turnForBranch()],
  };
  const codexChild = {
    ...piParent.branch,
    id: "codex-child-1",
    parent_branch_id: "branch-1",
    name: "Codex · Verifier experiment",
    runtime: "codex" as const,
    codex_thread_id: "codex-child-session",
  };
  const requests: Array<{ url: string; body?: string }> = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({
      url,
      body: typeof init?.body === "string" ? init.body : undefined,
    });
    const payload = url.endsWith("/continue-with-codex")
      ? codexChild
      : url.endsWith("/runtime-state")
        ? {
            runtime: "pi",
            state: "settled",
            capabilities: ["prompt", "steer", "follow_up", "abort"],
            session_id: "pi-parent-session",
            owner: "web",
            queue: { steering: [], follow_up: [] },
          }
        : url.endsWith("/conversation-state")
          ? piParent.conversation_state
          : piParent;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));
  const onOpenBranch = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
        onOpenBranch={onOpenBranch}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Continue with Codex" }));

  await waitFor(() => expect(requests.some((request) => (
    request.url.endsWith("/continue-with-codex")
  ))).toBe(true));
  const codexRequest = requests.find((request) => request.url.endsWith("/continue-with-codex"));
  expect(JSON.parse(codexRequest?.body ?? "{}")).toMatchObject({
    manual_approval: true,
    continuation: "parallel",
  });
  expect(screen.getByText("Created a parallel Codex fork with a frozen Pi handoff. The Pi Branch remains available.")).toBeInTheDocument();
  expect(onOpenBranch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Open Codex fork" }));
  expect(onOpenBranch).toHaveBeenCalledWith("codex-child-1");
});

test("separates current session availability from the previous failed attempt", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const externalRunning: BranchDetail = {
    ...detail,
    session: {
      ...detail.session!,
      status: "error",
      lease_expires_at: "2026-07-14T00:00:00Z",
      error: "Codex terminal exited with status 1",
    },
    external_jobs: [{
      id: "job-1",
      work_item_id: "work-1",
      provider: "ltp/cluster-c",
      remote_job_id: "fp8-retry-1",
      status: "created",
      metadata_json: {},
      created_at: "2026-07-17T03:06:07Z",
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(externalRunning), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const statuses = await screen.findByLabelText("Work, session, and Goal status");
  expect(within(statuses).getByText("Running")).toBeInTheDocument();
  expect(within(statuses).getByText("Error")).toBeInTheDocument();
  expect(within(statuses).getByText("Ready to continue")).toBeInTheDocument();
});

test("routes a continued parent to its child instead of accepting another turn", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const continued: BranchDetail = {
    ...detail,
    branch: {
      ...detail.branch,
      status: "continued",
      continued_by_branch_id: "child-branch-1",
    },
    session: {
      ...detail.session!,
      status: "completed",
      lease_expires_at: "2026-07-14T00:00:00Z",
    },
    conversation_state: {
      can_send: false,
      web_turn_status: "completed",
      local_turn_active: false,
      reason: "This Branch continues in its child Branch",
    },
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.endsWith("/conversation-state")
      ? continued.conversation_state
      : continued;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));
  const onOpenBranch = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
        onOpenBranch={onOpenBranch}
      />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("This conversation continued in a child Branch")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open child" }));
  expect(onOpenBranch).toHaveBeenCalledWith("child-branch-1");
  expect(screen.getByRole("button", { name: "Send Web turn" })).toBeDisabled();
});

test("shows the fork objective and traceable parent handoff in Brief", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const child: BranchDetail = {
    ...detail,
    branch: {
      ...detail.branch,
      id: "child-branch-1",
      parent_branch_id: "branch-1",
      name: "OpenReward system",
      context_snapshot: {
        fork_objective: "Build the four-domain OpenReward system.",
        parent_snapshot: {
          source_turn_id: "turn-origin-1",
          conclusion: "The protocol layer is ready; connect the real backend next.",
        },
      },
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(child), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="child-branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("tab", { name: "brief" }));
  expect(screen.getByText("Build the four-domain OpenReward system.")).toBeInTheDocument();
  expect(screen.getByText("source turn turn-origin-1")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Parent handoff"));
  expect(screen.getByText("The protocol layer is ready; connect the real backend next.")).toBeInTheDocument();
  expect(screen.queryByText("Evaluate PPT trajectories")).not.toBeInTheDocument();
});

test("keeps an interrupted Web query in result history without inventing an answer", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const interrupted: BranchDetail = {
    ...detail,
    session: {
      ...detail.session!,
      status: "completed",
      error: "Interrupted from the Web workspace",
    },
    messages: [{
      id: "interrupted-query-1",
      branch_id: "branch-1",
      session_id: "session-1",
      turn_id: "interrupted-turn-1",
      role: "user",
      kind: "message",
      content: "Compare the paired FP8 experiment",
      metadata_json: {
        origin: "web",
        send_status: "interrupted",
        error: "Interrupted from the Web workspace",
      },
      created_at: "2026-07-15T06:40:02Z",
    }, {
      id: "interrupted-update-1",
      branch_id: "branch-1",
      session_id: "session-1",
      turn_id: "interrupted-turn-1",
      external_item_id: "interrupted-item-1",
      role: "assistant",
      kind: "message",
      content: "The paired FP8 run reached checkpoint load before the worker disappeared.",
      metadata_json: { status: "completed" },
      created_at: "2026-07-15T06:40:12Z",
    }],
    conversation_state: {
      can_send: true,
      web_turn_status: "interrupted",
      local_turn_active: false,
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(interrupted), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Interrupted query")).toBeInTheDocument();
  expect(screen.getAllByText("Compare the paired FP8 experiment")).toHaveLength(2);
  expect(screen.getByText("No final Codex answer was recorded")).toBeInTheDocument();
  expect(screen.getByText("1 update recorded before interruption")).toBeInTheDocument();
  expect(screen.getByText("The paired FP8 run reached checkpoint load before the worker disappeared.")).toBeInTheDocument();
  expect(screen.getByText("Interrupted from the Web workspace")).toBeInTheDocument();
  expect(screen.getByText("0 completed · 1 interrupted")).toBeInTheDocument();
  expect(screen.queryByText("No completed Codex turns yet")).not.toBeInTheDocument();
});

test("keeps the newest interrupted query visible beside newer completed results", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const interruptedBehindResults: BranchDetail = {
    ...detail,
    session: { ...detail.session!, status: "completed" },
    turns: Array.from({ length: 8 }, (_value, index) => ({
      ...turnForBranch(),
      id: `newer-turn-${index}`,
      external_turn_id: `newer-external-turn-${index}`,
      final_answer: `Newer result ${index}`,
      completed_at: new Date(Date.parse("2026-07-15T06:40:10Z") + index * 1000).toISOString(),
    })),
    messages: [{
      id: "older-interrupted-query",
      branch_id: "branch-1",
      session_id: "session-1",
      turn_id: "older-interrupted-turn",
      role: "user",
      kind: "message",
      content: "Keep this interrupted query visible",
      metadata_json: {
        origin: "web",
        send_status: "interrupted",
        error: "Interrupted from the Web workspace",
      },
      created_at: "2026-07-15T06:40:00Z",
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(interruptedBehindResults), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Interrupted query")).toBeInTheDocument();
  expect(screen.getAllByText("Keep this interrupted query visible")).toHaveLength(2);
  expect(document.querySelectorAll(".turn-result")).toHaveLength(6);
  expect(screen.getByText("3 not shown")).toBeInTheDocument();
});

test("unlocks a failed Web query and restores it as an explicit draft", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const failed: BranchDetail = {
    ...detail,
    session: {
      ...detail.session!,
      status: "error",
      error: "Codex runtime request failed",
    },
    messages: [{
      id: "failed-query-1",
      branch_id: "branch-1",
      session_id: "session-1",
      turn_id: "failed-turn-1",
      role: "user",
      kind: "message",
      content: "Recheck the current deployment before resubmitting",
      metadata_json: {
        origin: "web",
        send_status: "failed",
        error: "Request blocked after the stream disconnected before response.completed",
      },
      created_at: "2026-07-15T06:40:02Z",
    }],
    conversation_state: {
      can_send: true,
      web_turn_status: "failed",
      local_turn_active: false,
    },
  };
  const fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify(failed), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const input = await screen.findByPlaceholderText("Send one Web turn");
  expect(input).toBeEnabled();
  expect(screen.getByText("The previous Web turn failed. This Branch is ready for a new message.")).toBeInTheDocument();
  expect(screen.getByText("The model gateway did not settle this turn. The Branch is unlocked; the query was not replayed automatically because its remote execution state is unknown.")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Use failed query as draft" }));
  await waitFor(() => expect(input).toHaveValue("Recheck the current deployment before resubmitting"));
  expect(screen.getByRole("button", { name: "Send Web turn" })).toBeEnabled();
  expect(fetchMock).not.toHaveBeenCalledWith(
    "/api/branches/branch-1/messages",
    expect.objectContaining({ method: "POST" }),
  );
});

test("disables Web input while a local CLI turn is running", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const local = {
    ...detail,
    conversation_state: {
      can_send: false as const,
      web_turn_status: "idle" as const,
      local_turn_active: true,
      reason: "Wait for the current local CLI turn to finish",
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(local), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Local CLI turn running. You can draft here; Send unlocks when it finishes.")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Send one Web turn")).toBeEnabled();
  expect(screen.getByRole("button", { name: "Send Web turn" })).toBeDisabled();
});

test("mirrors a local CLI snapshot and updates tool status in place", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const local = {
    ...detail,
    conversation_state: {
      can_send: false as const,
      web_turn_status: "idle" as const,
      local_turn_active: true,
      reason: "Wait for the current local CLI turn to finish",
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(local), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("tab", { name: "Live activity" }));
  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "local.live.snapshot",
      events: [
        {
          id: "local:thread:message:10",
          turn_id: "turn-local",
          role: "assistant",
          kind: "message",
          content: "Checkpoint 12 is healthy.",
          created_at: "2026-07-14T00:00:01Z",
          status: "completed",
        },
        {
          id: "local:thread:tool:call-1",
          turn_id: "turn-local",
          role: "tool",
          kind: "command",
          content: "$ uv run pytest -q",
          created_at: "2026-07-14T00:00:02Z",
          status: "running",
        },
      ],
    }),
  })));

  expect(await screen.findByText("Checkpoint 12 is healthy.")).toBeInTheDocument();
  expect(screen.getByText("Current local CLI turn")).toBeInTheDocument();
  expect(screen.queryByText("Command running")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch", { name: "Tool activity" }));
  expect(await screen.findByText("Command running")).toBeInTheDocument();
  expect(screen.getAllByText("$ uv run pytest -q")).toHaveLength(2);

  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "local.live.batch",
      events: [{
        id: "local:thread:tool:call-1",
        turn_id: "turn-local",
        role: "tool",
        kind: "command",
        content: "$ uv run pytest -q\nstatus=completed",
        created_at: "2026-07-14T00:00:03Z",
        status: "completed",
      }],
    }),
  })));

  expect(await screen.findByText("Command · completed")).toBeInTheDocument();
  expect(screen.getAllByText("$ uv run pytest -q")).toHaveLength(1);
});

test("renders only a small page of long result history", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const longHistory = {
    ...detail,
    turns: Array.from({ length: 12 }, (_value, index) => ({
      ...turnForBranch(),
      id: `turn-${index}`,
      external_turn_id: `external-turn-${index}`,
      final_answer: `Result ${index}`,
      completed_at: new Date(Date.parse("2026-07-14T00:00:00Z") + index * 1000).toISOString(),
    })),
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(longHistory), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("7 not shown")).toBeInTheDocument();
  expect(document.querySelectorAll(".turn-result")).toHaveLength(5);
  expect(screen.getByText("Result 11")).toBeInTheDocument();
  expect(screen.queryByText("Result 10")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show earlier turns" }));
  expect(document.querySelectorAll(".turn-result")).toHaveLength(12);
});

test("labels automatic Goal results and previous sessions without inventing a Query", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const history = {
    ...detail,
    branch: { ...detail.branch, codex_thread_id: "current-thread" },
    turns: [{
      ...turnForBranch(),
      thread_id: "previous-thread",
      prompt: "Continue objective: Complete four experiment rounds",
      metadata_json: { prompt_kind: "goal_continuation" },
      final_answer: "The first round passed.",
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(history), {
    status: 200, headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
  </QueryClientProvider>);
  expect(await screen.findByText("Goal continuation")).toBeInTheDocument();
  expect(screen.getByText("Previous session · previous")).toHaveAttribute("title", "previous-thread");
  const result = document.querySelector(".turn-result") as HTMLElement;
  expect(within(result).getByText("Objective")).toBeInTheDocument();
  expect(within(result).queryByText("Query")).not.toBeInTheDocument();
  expect(within(result).queryByText(/Continue objective:/)).not.toBeInTheDocument();
});

test("keeps literal Continue objective user text as a normal Query", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const history = {
    ...detail,
    turns: [{ ...turnForBranch(), prompt: "Continue objective: literal human request",
      metadata_json: { prompt_kind: "user" } }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(history), {
    status: 200, headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
  </QueryClientProvider>);
  expect(await screen.findByText("Latest Codex result")).toBeInTheDocument();
  expect(screen.getByText("Query")).toBeInTheDocument();
  expect(screen.queryByText("Goal continuation")).not.toBeInTheDocument();
});

test("submits one Web turn and clears the input after completion", async () => {
  const query = "  Check the latest metrics\n\nKeep my formatting.  ";
  vi.stubGlobal("WebSocket", TestWebSocket);
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST" && String(input).endsWith("/messages")) {
      return Promise.resolve(new Response(JSON.stringify({
        id: "message-web-1",
        branch_id: "branch-1",
        session_id: "session-1",
        turn_id: "turn-web-1",
        role: "user",
        kind: "message",
        content: query,
        metadata_json: { origin: "web", send_status: "submitted" },
        created_at: "2026-07-14T00:00:02Z",
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify(detail), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const input = await screen.findByPlaceholderText("Send one Web turn");
  fireEvent.change(input, { target: { value: query } });
  fireEvent.click(screen.getByRole("button", { name: "Send Web turn" }));

  expect(screen.getByRole("tab", { name: "Live activity" })).toHaveAttribute("aria-selected", "true");

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/branches/branch-1/messages",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ content: query }),
    }),
  ));
  await waitFor(() => expect(screen.getByPlaceholderText("Send one Web turn")).toHaveValue(""));
});

test("restores the submitted prompt after refreshing a running Web turn", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const running = {
    ...detail,
    session: {
      ...detail.session!,
      status: "expired",
      lease_expires_at: "2026-07-15T06:39:00Z",
    },
    conversation_state: {
      can_send: false as const,
      can_interrupt: true,
      web_turn_status: "running" as const,
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
    messages: [{
      id: "pending-web-message",
      branch_id: "branch-1",
      session_id: "session-1",
      role: "user" as const,
      kind: "message",
      content: "Continue the monitored training workflow",
      metadata_json: { origin: "web", send_status: "pending" },
      created_at: "2026-07-15T06:40:02Z",
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(running), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Submitted prompt")).toBeInTheDocument();
  expect(screen.getByText("Continue the monitored training workflow")).toBeInTheDocument();
  expect(screen.getByText("Web turn running. Stop it here or continue from the local CLI.")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Send one Web turn")).toBeEnabled();
  const statuses = screen.getByLabelText("Work, session, and Goal status");
  expect(within(statuses).getAllByText("Running")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
});

test("can supersede a running Web turn from the composer", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const running: BranchDetail = {
    ...detail,
    conversation_state: {
      can_send: false,
      can_interrupt: true,
      web_turn_status: "running",
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
    messages: [{
      id: "pending-web-message",
      branch_id: "branch-1",
      session_id: "session-1",
      role: "user",
      kind: "message",
      content: "Old direction",
      metadata_json: { origin: "web", send_status: "pending" },
      created_at: "2026-07-15T06:40:02Z",
    }],
  };
  let sendAttempts = 0;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST" && String(input).endsWith("/messages")) {
      sendAttempts += 1;
      if (sendAttempts === 1) {
        return Promise.resolve(new Response(JSON.stringify({
          detail: "Wait for the current local CLI turn to finish",
        }), { status: 409, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        id: "message-web-2",
        branch_id: "branch-1",
        session_id: "session-1",
        role: "user",
        kind: "message",
        content: "Use the new direction",
        metadata_json: { origin: "web", send_status: "submitted" },
        created_at: "2026-07-15T06:41:02Z",
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify(running), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const input = await screen.findByPlaceholderText("Send one Web turn");
  fireEvent.change(input, { target: { value: "Use the new direction" } });
  const send = screen.getByRole("button", {
    name: "Interrupt current Web turn and send message",
  });
  expect(send).toBeEnabled();
  fireEvent.click(send);
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/branches/branch-1/messages",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ content: "Use the new direction" }),
    }),
  ), { timeout: 3_000 });
  await waitFor(() => expect(sendAttempts).toBe(2), { timeout: 3_000 });
  // Live activity remains selected after sending, but the composer stays
  // available so another prompt can explicitly interrupt and supersede it.
  expect(screen.getByPlaceholderText("Send one Web turn")).toBeInTheDocument();
});

test("restores a Web draft when sending fails", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST" && String(input).endsWith("/messages")) {
      return Promise.resolve(new Response(JSON.stringify({
        detail: "The branch is temporarily unavailable",
      }), { status: 409, headers: { "Content-Type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify(detail), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const input = await screen.findByPlaceholderText("Send one Web turn");
  fireEvent.change(input, { target: { value: "Keep this draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Send Web turn" }));

  expect(await screen.findByText("The branch is temporarily unavailable")).toBeInTheDocument();
  expect(input).toHaveValue("Keep this draft");
});

test("interrupts a running Web turn from the results toolbar", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const pendingMessage = {
    id: "pending-web-message",
    branch_id: "branch-1",
    session_id: "session-1",
    role: "user" as const,
    kind: "message",
    content: "Monitor the active run",
    metadata_json: { origin: "web", send_status: "pending" },
    created_at: "2026-07-15T06:40:02Z",
  };
  const running: BranchDetail = {
    ...detail,
    conversation_state: {
      can_send: false,
      can_interrupt: true,
      web_turn_status: "running",
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
    messages: [pendingMessage],
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST" && String(input).endsWith("/pending-web-message/interrupt")) {
      return Promise.resolve(new Response(JSON.stringify({
        ...pendingMessage,
        metadata_json: { origin: "web", send_status: "interrupted" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify(running), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/branches/branch-1/messages/pending-web-message/interrupt",
    expect.objectContaining({ method: "POST" }),
  ));
});

test("optionally shows and follows the current turn activity", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const running: BranchDetail = {
    ...detail,
    conversation_state: {
      can_send: false,
      web_turn_status: "running",
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
    messages: [
      {
        id: "pending-web-message",
        branch_id: "branch-1",
        session_id: "session-1",
        role: "user",
        kind: "message",
        content: "Monitor the active training run",
        metadata_json: { origin: "web", send_status: "pending" },
        created_at: "2026-07-15T06:40:02Z",
      },
      {
        id: "assistant-update-1",
        branch_id: "branch-1",
        session_id: "session-1",
        turn_id: "turn-web-1",
        external_item_id: "item-1",
        role: "assistant",
        kind: "message",
        content: "Checkpoint 7 is complete and training remains healthy.",
        metadata_json: {},
        created_at: "2026-07-15T06:41:02Z",
      },
      {
        id: "command-update-1",
        branch_id: "branch-1",
        session_id: "session-1",
        turn_id: "turn-web-1",
        external_item_id: "item-2",
        role: "tool",
        kind: "command",
        content: "$ tail -n 100 train.log\nstatus=completed exit_code=0",
        metadata_json: { status: "completed", exit_code: 0 },
        created_at: "2026-07-15T06:41:05Z",
      },
    ],
  };
  const fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify(running), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Current turn progress")).toBeInTheDocument();
  expect(screen.getByText("Checkpoint 7 is complete and training remains healthy.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "Live activity" }));

  expect(await screen.findByText("Checkpoint 7 is complete and training remains healthy.")).toBeInTheDocument();
  expect(screen.getByText("$ tail -n 100 train.log")).toBeInTheDocument();
  expect(screen.getByText("Current Web turn")).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Follow live" })).toBeChecked();
  expect(window.localStorage.getItem("research-ops.results-view")).toBe("live");

  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "assistant.delta",
      turn_id: "turn-web-1",
      item_id: "item-3",
      delta: "Step 48 reached a new low loss.",
    }),
  })));

  expect(await screen.findByText("Step 48 reached a new low loss.")).toBeInTheDocument();
  expect((fetchMock.mock.calls as unknown[][]).filter(([url]) => String(url) === "/api/branches/branch-1")).toHaveLength(1);
});

test("does not mix stale Web drafts into the current turn", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const running: BranchDetail = {
    ...detail,
    conversation_state: {
      can_send: false,
      web_turn_status: "running",
      local_turn_active: false,
      reason: "Wait for the current Web turn to finish",
    },
    messages: [
      {
        id: "pending-current-message",
        branch_id: "branch-1",
        session_id: "session-1",
        turn_id: "turn-current",
        role: "user",
        kind: "message",
        content: "Repair the image pull failure",
        metadata_json: { origin: "web", send_status: "pending" },
        created_at: "2026-07-15T07:00:00Z",
      },
      {
        id: "old-persisted-update",
        branch_id: "branch-1",
        session_id: "session-1",
        turn_id: "turn-previous",
        role: "assistant",
        kind: "message",
        content: "Old queue monitoring update.",
        metadata_json: {},
        created_at: "2026-07-15T06:50:00Z",
      },
    ],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(running), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("tab", { name: "Live activity" }));
  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "assistant.delta",
      turn_id: "turn-previous",
      item_id: "old-draft",
      delta: "Stale live queue monitoring update.",
    }),
  })));
  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "assistant.delta",
      turn_id: "turn-current",
      item_id: "current-draft",
      delta: "Building the compact replacement image.",
    }),
  })));
  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({
      type: "local.live.snapshot",
      events: [{
        id: "local:thread:message:1",
        turn_id: "durable-jsonl-turn",
        role: "assistant",
        kind: "message",
        content: "The durable JSONL mirror confirms the replacement image build.",
        created_at: "2026-07-15T07:01:00Z",
        status: "completed",
        source: "local_cli",
      }],
    }),
  })));

  expect(await screen.findByText("Building the compact replacement image.")).toBeInTheDocument();
  expect(screen.getByText("The durable JSONL mirror confirms the replacement image build.")).toBeInTheDocument();
  expect(screen.queryByText("Old queue monitoring update.")).not.toBeInTheDocument();
  expect(screen.queryByText("Stale live queue monitoring update.")).not.toBeInTheDocument();
});

test("shows the managed Q3 entry without hiding the original Branch location", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const legacy = {
    ...detail,
    branch: {
      ...detail.branch,
      workspace_path: "/workspace/Q3/HARN/tasks/HARN-0001/primary",
      cwd: "/workspace/legacy/HARN-0001/main",
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(legacy), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("tab", { name: "brief" }));
  expect(screen.getByText("/workspace/Q3/HARN/tasks/HARN-0001/primary")).toBeInTheDocument();
  expect(screen.getByText("Original Branch location")).toBeInTheDocument();
  expect(screen.getByText("/workspace/legacy/HARN-0001/main")).toBeInTheDocument();
});

test("separates executable work status from the latest session status", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const expired = {
    ...detail,
    session: {
      ...detail.session!,
      status: "expired",
      lease_expires_at: "2026-07-14T00:01:00Z",
    },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(expired), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  const statuses = await screen.findByLabelText("Work, session, and Goal status");
  expect(within(statuses).getByText("Ready")).toBeInTheDocument();
  expect(within(statuses).getByText("Expired")).toBeInTheDocument();
  expect(within(statuses).getByText("Ready to continue")).toBeInTheDocument();
});

test.each(["active", "paused"] as const)("shows the Codex Native Goal status, usage, and controls (%s)", async (goalStatus) => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const now = Math.floor(Date.now() / 1_000);
  const goalState: NativeGoalState = {
    supported: true,
    thread_id: detail.branch.codex_thread_id,
    goal: {
      thread_id: detail.branch.codex_thread_id!,
      objective: "Keep checking until the rollout is complete.",
      status: goalStatus,
      token_budget: 250_000,
      tokens_used: 125_000,
      time_used_seconds: 3_600,
      created_at: now - 3_600,
      updated_at: now,
    },
    runtime: {
      status: "retrying", retry_attempts: 1, supervised: true,
      notifications_enabled: true, progress_interval_seconds: 900,
      last_checked_at: new Date().toISOString(),
      last_activity_at: new Date().toISOString(),
      next_retry_at: new Date(Date.now() + 30_000).toISOString(),
      last_error: "503 service unavailable",
      last_summary: "The first paired test is complete.",
      notification_status: "failed", notification_error: "Delivery timeout",
    },
  };
  const withGoal: BranchDetail = { ...detail, runtime: "codex", goals: [] };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.endsWith("/native-goal")
      ? goalState
      : url.endsWith("/conversation-state")
        ? withGoal.conversation_state
        : withGoal;
    return Promise.resolve(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        initialTab="goals"
        onTabChange={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("Keep checking until the rollout is complete.")).toBeInTheDocument();
  const statuses = screen.getByLabelText("Work, session, and Goal status");
  expect(within(statuses).getByText(goalStatus === "active" ? "Active" : "Paused")).toBeInTheDocument();
  expect(within(statuses).getByText("125K · 1h")).toBeInTheDocument();
  expect(screen.getByText("Tokens used")).toBeInTheDocument();
  expect(screen.getByText("250K")).toBeInTheDocument();
  expect(screen.getByLabelText("50% of Goal token budget used")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  expect(within(statuses).getByLabelText("Goal execution")).toHaveTextContent("Retry scheduled");
  expect(screen.getByText("Every 15 min")).toBeInTheDocument();
  expect(screen.getByText("Next retry")).toBeInTheDocument();
  expect(screen.getByText("The first paired test is complete.")).toBeInTheDocument();
  expect(screen.getByText("503 service unavailable")).toBeInTheDocument();
  expect(screen.getByText("Notification failed: Delivery timeout")).toBeInTheDocument();
});

test("submits the exact objective without descriptions and reports a detached runtime", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const objective = "  实测 256K\n\n不要附加任何描述。  ";
  let submitted: Record<string, unknown> | undefined;
  let goalState: NativeGoalState = { supported: true, thread_id: "thread-1", goal: null };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/native-goal") && init?.method === "PUT") {
      submitted = JSON.parse(String(init.body));
      goalState = {
        supported: true, thread_id: "thread-1",
        goal: {
          thread_id: "thread-1", objective, status: "active", token_budget: null,
          tokens_used: 0, time_used_seconds: 0, created_at: 1, updated_at: 1,
        },
        runtime: { status: "detached", supervised: true, retry_attempts: 0, progress_interval_seconds: 900 },
      };
    }
    const payload = url.endsWith("/native-goal") ? goalState
      : url.endsWith("/conversation-state") ? detail.conversation_state : detail;
    return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
  </QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Set goal" }));
  fireEvent.change(screen.getByRole("textbox", { name: /Objective/ }), { target: { value: objective } });
  fireEvent.click(screen.getByRole("button", { name: "Start goal" }));
  await screen.findByText("Goal saved. Execution: Runtime disconnected.");
  expect(submitted).toEqual({ objective, token_budget: null });
  fireEvent.click(screen.getByRole("tab", { name: "goals" }));
  expect(await screen.findByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  expect(screen.getByLabelText("Goal execution")).toHaveTextContent("Runtime disconnected");
});

test("starts a fresh goal after completion without copying the old objective or budget", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const state: NativeGoalState = {
    supported: true, thread_id: "thread-1", goal: {
      thread_id: "thread-1", objective: "Finished old objective", status: "complete",
      token_budget: 1000, tokens_used: 400, time_used_seconds: 12, created_at: 1, updated_at: 2,
    },
  };
  let submitted: Record<string, unknown> | undefined;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/native-goal") && init?.method === "PUT") {
      submitted = JSON.parse(String(init.body));
      return Promise.resolve(new Response(JSON.stringify({ detail: "Goal service unavailable" }), { status: 503 }));
    }
    const payload = url.includes("/native-goal/history") ? { items: [], total: 0, next_offset: null }
      : url.endsWith("/native-goal") ? state
        : url.endsWith("/conversation-state") ? detail.conversation_state : { ...detail, runtime: "codex" };
    return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <BranchPanel branchId="branch-1" program={program} initialTab="goals" onTabChange={() => {}} onClose={() => {}} />
  </QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "New goal" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText("Start Codex Native Goal")).toBeInTheDocument();
  const objective = within(dialog).getByRole("textbox", { name: /Objective/ });
  expect(objective).toHaveValue("");
  expect(within(dialog).getByRole("spinbutton", { name: /Token budget/ })).toHaveValue(null);
  expect(within(dialog).getByRole("button", { name: "Start goal" })).toBeDisabled();
  fireEvent.change(objective, { target: { value: "  A new objective\nExactly as entered.  " } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Start goal" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Goal service unavailable");
  expect(submitted).toEqual({ objective: "  A new objective\nExactly as entered.  ", token_budget: null });
  expect(objective).toHaveValue("  A new objective\nExactly as entered.  ");
});

test("shows history even when the current session has no native goal", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const payload = url.includes("/native-goal/history") ? {
      total: 1, next_offset: null, items: [{
        goal_key: "old", thread_id: "old-session", objective: "A historical goal", status: "complete",
        source: "monitor", first_recorded_at: "2026-09-12T01:00:00Z", last_recorded_at: "2026-09-12T02:00:00Z",
        tokens_used: 10, time_used_seconds: 1, token_budget: null, goal_created_at: null,
        goal_updated_at: null, cleared_at: null, last_summary: null, last_error: null, turn_id: null,
      }],
    } : url.endsWith("/native-goal") ? { supported: true, thread_id: "new-session", goal: null }
      : url.endsWith("/conversation-state") ? detail.conversation_state : { ...detail, runtime: "codex" };
    return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <BranchPanel branchId="branch-1" program={program} initialTab="goals" onTabChange={() => {}} onClose={() => {}} />
  </QueryClientProvider>);
  expect(await screen.findByText("No Codex Native Goal")).toBeInTheDocument();
  expect(await screen.findByText("Goal history (1)")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Set native goal" })).toBeEnabled();
});

test("confirms a submitted branch result", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const expired = {
    ...detail,
    session: {
      ...detail.session!,
      status: "expired",
      lease_expires_at: "2026-07-14T00:01:00Z",
    },
  };
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.method === "POST" && url.endsWith("/result") ? {} : expired;
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  }));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "Submit result" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByRole("textbox", { name: /Conclusion/ }), {
    target: { value: "The primary path has a durable result." },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Submit result" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Result submitted. Path parked.");
});

test("optionally queues skill extraction after a successful handoff", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const completed = {
    ...detail,
    session: {
      ...detail.session!,
      status: "completed",
      lease_expires_at: "2026-07-14T00:01:00Z",
    },
    turns: [turnForBranch()],
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST" && url.endsWith("/handoff")) {
      return Promise.resolve(new Response(JSON.stringify(completed.session), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    }
    if (init?.method === "POST" && url.endsWith("/skill-proposals")) {
      return Promise.resolve(new Response(JSON.stringify({
        id: "proposal-1",
        branch_id: "branch-1",
        session_id: "session-1",
        status: "pending",
        title: "",
        description: "",
        rationale: "",
        reusable_steps: [],
        risks: [],
        evidence_refs: [],
        source_context: {},
        proposed_content: "",
        diff_text: "",
        validation: {},
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
      }), { status: 202, headers: { "Content-Type": "application/json" } }));
    }
    return Promise.resolve(new Response(JSON.stringify(completed), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "End this round" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "Extract reusable skill for review" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Save handoff" }));

  expect(await screen.findByRole("status")).toHaveTextContent("Skill proposal is analyzing in the Inbox");
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/branches/branch-1/sessions/session-1/skill-proposals",
    expect.objectContaining({ method: "POST" }),
  );
});

test("renders a synchronized Codex turn without refetching the full branch", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const fetchMock = vi.fn(() => Promise.resolve(new Response(JSON.stringify(detail), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })));
  vi.stubGlobal("fetch", fetchMock);

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
  await screen.findByText("No completed Codex turns yet");

  await act(async () => TestWebSocket.latest?.onmessage?.(new MessageEvent("message", {
    data: JSON.stringify({ type: "turn.synced", turn: {
      ...turnForBranch(),
      final_answer: "Streamed immediately",
    } }),
  })));

  expect(await screen.findByText("Streamed immediately")).toBeInTheDocument();
  expect((fetchMock.mock.calls as unknown[][]).filter(([url]) => String(url) === "/api/branches/branch-1")).toHaveLength(1);
});

test("renders local result paths as copyable references and opens web links externally", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const withLinks = {
    ...detail,
    turns: [{
      ...turnForBranch(),
      final_answer: "![Loss curve](/mnt/project/loss.png)\n\n[源文件 index.html](/mnt/project/index.html)\n\n[arXiv](https://arxiv.org/abs/1234.5678)",
    }],
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(withLinks), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel branchId="branch-1" program={program} onTabChange={() => {}} onClose={() => {}} />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("/mnt/project/index.html")).toBeInTheDocument();
  const image = screen.getByRole("img", { name: "Loss curve" });
  expect(image).toHaveAttribute(
    "src",
    "/api/branches/branch-1/result-file?path=%2Fmnt%2Fproject%2Floss.png",
  );
  expect(image.closest("a")).toHaveAttribute("target", "_blank");
  expect(screen.queryByRole("link", { name: /源文件 index.html/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Copy path /mnt/project/index.html" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("/mnt/project/index.html"));
  const external = screen.getByRole("link", { name: /arXiv/ });
  expect(external).toHaveAttribute("target", "_blank");
  expect(external).toHaveAttribute("rel", "noreferrer");
});

test("shows and copies the resumable Codex session id", async () => {
  vi.stubGlobal("WebSocket", TestWebSocket);
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const withThread = {
    ...detail,
    branch: { ...detail.branch, codex_thread_id: "019f5ea5-aa35-7502-901d-73ff4a5242a6" },
    session: { ...detail.session!, codex_thread_id: "019f5ea5-aa35-7502-901d-73ff4a5242a6" },
  };
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify(withThread), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }))));

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BranchPanel
        branchId="branch-1"
        program={program}
        onTabChange={() => {}}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("019f5ea5-aa35-7502-901d-73ff4a5242a6")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Copy session ID" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("019f5ea5-aa35-7502-901d-73ff4a5242a6"));
});

function turnForBranch() {
  return {
    id: "turn-1",
    branch_id: "branch-1",
    session_id: "session-1",
    thread_id: "thread-1",
    external_turn_id: "external-turn-1",
    status: "completed",
    prompt: "Run the evaluation",
    final_answer: "Response",
    completed_at: "2026-07-14T00:00:01Z",
    metadata_json: {},
  };
}
