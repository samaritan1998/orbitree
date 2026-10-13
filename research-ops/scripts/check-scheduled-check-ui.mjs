import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-check-prompts-20260921");
const output = "/tmp/research-ops-scheduled-check-ui";
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const path = resolve(root, "." + new URL(req.url, "http://localhost").pathname);
  if (!path.startsWith(root + "/")) { res.writeHead(403).end(); return; }
  const file = extname(path) ? path : resolve(root, "index.html");
  try { res.setHeader("Content-Type", mime[extname(file)] || "application/octet-stream"); res.end(await readFile(file)); }
  catch { res.writeHead(404).end(); }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
await mkdir(output, { recursive: true });
const origin = `http://127.0.0.1:${server.address().port}`;
const now = new Date().toISOString();
const program = { id: "p", code: "ADAPT", slug: "adapt", name: "Model Adaptation", objective: "Model adaptation", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "ADAPT-TEST", program_id: "p", title: "Training progress", objective: "Inspect a scheduled check", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "check-ui", work_item_id: "w", name: "Training progress", slug: "check-ui", status: "active", mode: "thread", cwd: "/tmp/check-ui", codex_thread_id: "check-ui-thread", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const execution = { identity: "api", label: "Company API", model: "gpt-6-astra", reasoning_effort: "max" };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [{ id: "turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "test-turn", status: "completed", prompt: "Check training progress", final_answer: "Training is running. No intervention is needed.", completed_at: now, metadata_json: { execution } }], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const prompt = "检查 train-test 的最新状态和日志。\n只检查一次，不重提、不删除任务。\n最后一行写检查结果：无变化、有进展、失败、完成或需要决定。";
let browser;
const failures = [];
let sideEffects = 0;
let drafts = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }, { width: 360, height: 640 }]) {
    let check = null;
    let runs = [];
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      const method = route.request().method();
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/execution")) body = { runtime: "codex", preferences: {}, effective: execution, identities: [{ id: "api", label: "Company API", ready: true }], api_isolated: true, login_command: "rops auth login" };
      else if (path.endsWith("/scheduled-check/prompt")) {
        drafts += 1;
        body = { prompt, provider: "oneapi", model: "DeepSeek-V4.1-Flash", generated_at: now, context_updated_at: now, context_turns: 4, context_messages: 1, cached: false };
      } else if (path.includes("/scheduled-check")) {
        if (method === "PUT") {
          const draft = route.request().postDataJSON();
          check = { ...draft, id: "check", branch_id: branch.id, status: "active", phase: "scheduled", execution, next_run_at: new Date(Date.now() + draft.interval_minutes * 60_000).toISOString(), active_run_id: null, last_completed_at: null, last_outcome: null, last_summary: null, consecutive_errors: 0, reason: null };
        } else if (method === "POST") {
          const { action } = route.request().postDataJSON();
          if (action === "run_now") {
            check = { ...check, last_completed_at: now, last_outcome: "unchanged", last_summary: "Training is running.\n检查结果：无变化" };
            runs = [{ id: "run", status: "completed", outcome: "unchanged", summary: check.last_summary, prompt: check.prompt, created_at: now, completed_at: now, execution, message_id: "query" }];
          } else {
            const status = action === "pause" ? "paused" : action === "stop" ? "stopped" : "active";
            check = { ...check, status, phase: status === "active" ? "scheduled" : status, next_run_at: status === "active" ? new Date(Date.now() + 600_000).toISOString() : null };
          }
        }
        body = { check, runs, jobs: [{ id: "job-1", name: "train-test", status: "Running", provider: "ltp" }], default_prompt: "检查任务【填写明确的任务名或 Job ID】。", notification: null, notifications_enabled: true, worker: { enabled: true, last_tick_at: now, error: null } };
      } else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: true, thread_id: branch.codex_thread_id, goal: null };
      else if (path.endsWith("/check-ui")) body = detail;
      if (method !== "GET" && !path.includes("/scheduled-check")) sideEffects += 1;
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/ADAPT/branches/check-ui/results`);
    const clock = page.getByRole("button", { name: "Scheduled checks", exact: true });
    await expect(clock).toBeEnabled();
    await clock.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/branch-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await clock.click();
    await expect(page.getByRole("textbox", { name: "Check prompt" })).toHaveValue(prompt);
    await page.getByLabel("Interval", { exact: true }).selectOption("custom");
    await page.getByLabel("Minutes", { exact: true }).fill("17");
    await page.getByLabel("Stop condition", { exact: true }).selectOption("deadline");
    await page.getByLabel(/Stop time.*Shanghai/).fill("2030-01-01T09:30");
    const dialog = page.getByRole("dialog", { name: "Scheduled checks" });
    const box = await dialog.boundingBox();
    if (!box || box.x < 0 || box.y < 0 || box.x + box.width > viewport.width + 1 || box.y + box.height > viewport.height + 1) throw new Error(`Dialog outside viewport ${viewport.width}: ${JSON.stringify(box)}`);
    await page.screenshot({ path: `${output}/dialog-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("button", { name: "Start schedule", exact: true }).click();
    await expect(dialog).toBeHidden();
    if (check.prompt !== prompt || check.interval_minutes !== 17 || check.stop_at !== "2030-01-01T01:30:00.000Z") throw new Error("Prompt or schedule was changed");
    await page.reload();
    await clock.click();
    await expect(page.getByRole("button", { name: "Apply suggested prompt" })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Check prompt" })).toHaveValue(check.prompt);
    await page.locator(".scheduled-check-suggestion").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/suggestion-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("button", { name: "Apply suggested prompt" }).click();
    await expect(page.getByLabel("Minutes", { exact: true })).toHaveValue("17");
    await page.getByRole("button", { name: "Check now", exact: true }).click();
    await page.getByText("Recent checks · 1").click();
    await page.locator(".scheduled-check-history details summary").click();
    await expect(page.getByText("Training is running.\n检查结果：无变化", { exact: true })).toBeVisible();
    await page.screenshot({ path: `${output}/history-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("button", { name: "Pause future checks", exact: true }).click();
    await page.getByRole("button", { name: "Resume scheduled checks", exact: true }).click();
    await page.getByRole("button", { name: /Stop future checks; external jobs/ }).click();
    await expect(page.getByRole("button", { name: /Stop future checks; external jobs/ })).toBeDisabled();
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    await page.close();
  }
  if (failures.length || sideEffects || drafts !== 6) throw new Error(JSON.stringify({ failures, sideEffects, drafts }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390, 360], failures, sideEffects, drafts }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
