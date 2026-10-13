import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import SkillsView from "./SkillsView";

const readyProposal = {
  id: "proposal-1",
  branch_id: "branch-1",
  session_id: "session-1",
  status: "ready",
  action: "merge",
  target_skill: "miaoda-ppt-autoresearch",
  title: "Add paired trajectory diagnosis",
  description: "Use when comparing paired Miaoda rollout trajectories.",
  rationale: "The work round established a repeatable evidence-backed comparison.",
  reusable_steps: ["Align paired examples.", "Compare reward and trajectory evidence."],
  risks: ["Keep model and dataset controls equal."],
  evidence_refs: ["turn-1"],
  source_context: {
    program_code: "HARN",
    work_human_id: "HARN-0001",
    work_title: "Validate PPT trajectories",
    branch_name: "Main",
  },
  proposed_content: "---\nname: miaoda-ppt-autoresearch\n---\n",
  diff_text: "@@ -1 +1 @@\n-old\n+new\n",
  validation: { valid: true },
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
};

const installedSkill = {
  name: "miaoda-ppt-autoresearch",
  directory_name: "miaoda-ppt-autoresearch",
  description: "Run the evidence-backed Miaoda PPT trajectory loop.",
  scope: "personal",
  install_path: "/root/.codex/skills/miaoda-ppt-autoresearch",
  source_path: "/workspace/skills/miaoda-ppt-autoresearch",
  source_available: true,
  supporting_files: 4,
  valid: true,
  updated_at: "2026-07-14T00:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("shows installed skills before the review inbox", async () => {
  let proposal = readyProposal;
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") proposal = { ...proposal, status: "accepted" };
    const body = url.endsWith("/skills")
      ? [installedSkill]
      : init?.method === "POST" ? proposal : [proposal];
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(<QueryClientProvider client={client}><SkillsView /></QueryClientProvider>);

  expect(await screen.findByText("Run the evidence-backed Miaoda PPT trajectory loop.")).toBeInTheDocument();
  expect(screen.getByText("Source linked")).toBeInTheDocument();
  expect(screen.getByText("/root/.codex/skills/miaoda-ppt-autoresearch")).toBeInTheDocument();
  expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/skill-proposals"))).toBe(false);
  rerender(<QueryClientProvider client={client}><SkillsView view="proposals" /></QueryClientProvider>);
  expect(await screen.findByText("Add paired trajectory diagnosis")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Review proposed diff"));
  expect(screen.getByText(/\+new/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Accept and install" }));

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText(/writes the reviewed SKILL.md/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Accept and install" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    "/api/skill-proposals/proposal-1/accept",
    expect.objectContaining({ method: "POST" }),
  ));
});
