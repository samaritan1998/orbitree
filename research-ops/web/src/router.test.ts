import { expect, test } from "vitest";
import { pagePath, parsePath } from "./router";
import { conversationTurnPending, externalJobActive, sessionStatus, timestampMillis } from "./status";

test("round trips branch inspector deep links", () => {
  const page = parsePath("/programs/COST/branches/branch-123/activity");
  expect(page).toEqual({ type: "program", code: "COST", branchId: "branch-123", tab: "activity" });
  expect(pagePath(page)).toBe("/programs/COST/branches/branch-123/activity");
});

test("round trips work item deep links", () => {
  const page = parsePath("/programs/HARN/work/HARN-0042/brief");
  expect(page).toEqual({ type: "program", code: "HARN", workId: "HARN-0042", tab: "brief" });
  expect(pagePath(page)).toBe("/programs/HARN/work/HARN-0042/brief");
});

test("keeps list view in program URLs across refreshes", () => {
  const page = parsePath("/programs/COST/work/COST-0001/results", "?view=list");
  expect(page).toEqual({
    type: "program",
    code: "COST",
    workId: "COST-0001",
    tab: "results",
    view: "list",
  });
  expect(pagePath(page)).toBe("/programs/COST/work/COST-0001/results?view=list");
  if (page.type !== "program") throw new Error("Expected a Program page");
  expect(pagePath({ ...page, view: "graph" })).toBe("/programs/COST/work/COST-0001/results");
});

test("maps legacy conversation links to results", () => {
  const page = parsePath("/programs/HARN/branches/branch-123/conversation");
  expect(page).toEqual({ type: "program", code: "HARN", branchId: "branch-123", tab: "results" });
  expect(pagePath(page)).toBe("/programs/HARN/branches/branch-123/results");
});

test("uses stable routes for top-level pages", () => {
  expect(pagePath(parsePath("/merge"))).toBe("/merge");
  expect(pagePath(parsePath("/memory"))).toBe("/memory");
  expect(pagePath(parsePath("/repositories"))).toBe("/repositories");
  expect(pagePath(parsePath("/sessions"))).toBe("/sessions");
  expect(pagePath(parsePath("/clusters"))).toBe("/clusters");
  expect(pagePath(parsePath("/ltp-monitor"))).toBe("/ltp-monitor");
  expect(pagePath(parsePath("/skills"))).toBe("/skills");
});

test.each([
  ["/memory", "?view=ledger", { type: "memory", view: "ledger" }],
  ["/skills", "?view=proposals", { type: "skills", view: "proposals" }],
] as const)("preserves the advanced view at %s%s", (path, search, expected) => {
  expect(parsePath(path, search)).toEqual(expected);
  expect(pagePath(parsePath(path, search))).toBe(path + search);
});

test("falls back to the main view for an unknown section", () => {
  expect(pagePath(parsePath("/memory", "?view=unknown"))).toBe("/memory");
  expect(pagePath(parsePath("/skills", "?view=unknown"))).toBe("/skills");
});

test("treats timezone-less server timestamps as UTC", () => {
  expect(timestampMillis("2026-07-13T17:06:00")).toBe(Date.parse("2026-07-13T17:06:00Z"));
});

test("keeps handoff blocked until the latest user turn finishes", () => {
  expect(conversationTurnPending([
    { role: "user", metadata_json: { send_status: "pending" } },
    { role: "assistant", metadata_json: {} },
  ])).toBe(true);
  expect(conversationTurnPending([
    { role: "user", metadata_json: { send_status: "submitted" } },
  ])).toBe(false);
});

test("reports an expired session separately from work readiness", () => {
  expect(sessionStatus({
    id: "session-1",
    branch_id: "branch-1",
    status: "expired",
    lease_id: "lease-1",
    lease_owner: "terminal",
    lease_expires_at: "2026-07-14T00:00:00Z",
    heartbeat_at: "2026-07-14T00:00:00Z",
    brief: {},
    handoff: {},
  })).toBe("Expired");
});

test("recognizes active external job status variants", () => {
  expect(externalJobActive("created")).toBe(true);
  expect(externalJobActive("submitted_waiting_capacity")).toBe(true);
  expect(externalJobActive("failed_post_success_worker_ack_write")).toBe(false);
  expect(externalJobActive("succeeded_100_steps_finite_fp8_audits")).toBe(false);
});
