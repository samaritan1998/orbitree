import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "@playwright/test";

const preview = process.env.ROPS_PREVIEW_URL || "http://127.0.0.1:5176";
const backend = process.env.ROPS_SMOKE_API || "http://127.0.0.1:8020";
const output = process.env.ROPS_SMOKE_OUTPUT || "../data/acceptance/goal-history";
const completeBranch = "00000000-0000-4000-8000-000000000001";
const rolloverBranch = "00000000-0000-4000-8000-000000000002";
const oldObjective = "/srv/research-ops/projects/Qwen3.8-27B-Infer/experiment.md\nRun the experiment checklist and preserve every result.\nSummarize independent experiments without modifying the objective.";
const writes = [];
const errors = [];
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.ROPS_SMOKE_CHROMIUM });
try {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    localStorage.setItem("research-ops:sidebar", "collapsed");
    localStorage.setItem("research-ops:program-navigator:ADAPT", "closed");
    localStorage.setItem("research-ops:ambience", "none");
  });
  await context.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() !== "GET") {
      writes.push(path);
      return route.fulfill({ status: 409, json: { detail: "Read-only acceptance run" } });
    }
    if (path.endsWith("/native-goal/history")) {
      const isCompleted = path.includes(completeBranch);
      return route.fulfill({ json: { total: 1, next_offset: null, items: [{
        goal_key: "acceptance-history", thread_id: "00000000-0000-4000-8000-000000000003",
        objective: isCompleted ? "Finished performance comparison" : oldObjective,
        status: "complete", token_budget: null, tokens_used: 30667559,
        time_used_seconds: 90834, goal_created_at: null, goal_updated_at: null,
        first_recorded_at: "2026-09-11T10:00:00Z", last_recorded_at: "2026-09-13T05:00:00Z",
        cleared_at: null, last_summary: "The recorded result remains available after session rollover.",
        last_error: null, turn_id: null, source: "notification",
      }] } });
    }
    if (path.endsWith("/native-goal")) {
      const isCompleted = path.includes(completeBranch);
      return route.fulfill({ json: { supported: true, thread_id: "current-session", runtime: null,
        goal: isCompleted ? {
          thread_id: "current-session", objective: "Finished performance comparison", status: "complete",
          token_budget: 1000, tokens_used: 400, time_used_seconds: 30, created_at: 1, updated_at: 2,
        } : null,
      } });
    }
    const response = await route.fetch({ url: `${backend}${new URL(req.url()).pathname}${new URL(req.url()).search}` });
    await route.fulfill({ response });
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.goto(`${preview}/programs/ADAPT/branches/${rolloverBranch}/goals`);
    const history = page.getByRole("region", { name: "Goal history" });
    await history.getByText("Goal history (1)").waitFor();
    await history.locator("summary").click();
    assert.equal(await history.locator("details").getAttribute("open"), "");
    await history.getByText("Codex Session ID", { exact: true }).scrollIntoViewIfNeeded();
    const overflow = await history.evaluate((element) => ({ scroll: element.scrollWidth, width: element.clientWidth }));
    assert.ok(overflow.scroll <= overflow.width + 1, `History overflow: ${JSON.stringify(overflow)}`);
    await page.screenshot({ path: `${output}/history-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.goto(`${preview}/programs/ADAPT/branches/${completeBranch}/goals`);
    await page.getByRole("button", { name: "New goal", exact: true }).click();
    const dialog = page.getByRole("dialog");
    assert.equal(await dialog.getByRole("textbox", { name: "Objective" }).inputValue(), "");
    assert.equal(await dialog.getByRole("spinbutton", { name: "Token budget" }).inputValue(), "");
    await dialog.getByRole("textbox", { name: "Objective" }).fill("A new objective");
    assert.equal(await dialog.getByRole("button", { name: "Start goal", exact: true }).isEnabled(), true);
    await page.screenshot({ path: `${output}/new-goal-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  }
  assert.deepEqual(writes, [], "Acceptance must not mutate production");
  assert.deepEqual(errors, [], "Browser errors");
  console.log(JSON.stringify({ desktop: "passed", mobile: "passed", productionWrites: writes.length, screenshots: output }));
} finally {
  await browser.close();
}
