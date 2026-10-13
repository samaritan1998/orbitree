import { expect, test } from "vitest";
import { programDocumentTitle, routeDocumentTitle } from "./documentTitle";
import type { Branch, Program, WorkItem } from "./types";

const program: Program = {
  id: "program-harn",
  code: "HARN",
  slug: "harn",
  name: "Harness / Office",
  objective: "Build harness tasks",
  color: "#2563eb",
  sort_order: 10,
  active: true,
};

const workItem: WorkItem = {
  id: "work-harn-1",
  human_id: "HARN-0001",
  program_id: program.id,
  title: "PPT trajectory experiment",
  objective: "Evaluate trajectories",
  kind: "task",
  lifecycle: "active",
  flow: "running",
  priority: 80,
  acceptance_criteria: [],
  labels: [],
  revision: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const mainBranch: Branch = {
  id: "branch-main",
  work_item_id: workItem.id,
  name: "Main",
  slug: "main",
  status: "active",
  mode: "write",
  cwd: "/tmp/main",
  context_snapshot: {},
  memory_cursor: 0,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

test("uses compact route titles", () => {
  expect(routeDocumentTitle({ type: "today" })).toBe("今日 | Research Ops");
  expect(routeDocumentTitle({ type: "sessions" })).toBe("会话 | Research Ops");
  expect(routeDocumentTitle({ type: "ltp-monitor" })).toBe("任务监控 · 任务与资源 | Research Ops");
  expect(routeDocumentTitle({ type: "memory" })).toBe("每日复盘 | Research Ops");
  expect(routeDocumentTitle({ type: "memory", view: "ledger" })).toBe("记忆账本 | Research Ops");
  expect(routeDocumentTitle({ type: "skills", view: "proposals" })).toBe("技能审核 | Research Ops");
});

test("uses the work title for a main branch and branch name for a child", () => {
  expect(programDocumentTitle({ program, workItem, branch: mainBranch })).toBe(
    "HARN-0001 · PPT trajectory experiment | Research Ops",
  );
  expect(programDocumentTitle({
    program,
    workItem,
    branch: { ...mainBranch, id: "branch-child", name: "Online MOPD", slug: "online-mopd", parent_branch_id: mainBranch.id },
    tab: "jobs",
  })).toBe("HARN-0001 · Online MOPD · Jobs | Research Ops");
});
