import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect: playwrightExpect } = require("@playwright/test");
const expect = playwrightExpect.configure({ timeout: 30_000 });
const root = resolve(process.argv[2] || "/tmp/research-ops-native-goal-dist");
const output = "/tmp/research-ops-native-goal-ui";
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
const work = { id: "w", human_id: "ADAPT-TEST", program_id: "p", title: "256K paired evaluation", objective: "Complete the 256K paired evaluation", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "native-ui", work_item_id: "w", name: "Native Goal verification", slug: "native-ui", status: "active", mode: "thread", cwd: "/tmp/native-ui", codex_thread_id: "native-ui-thread", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const objective = "完成 256K 的成对测试，记录长上下文吞吐和稳定性。\n比较主模型与草稿模型的结果。";
let browser;
const failures = [];
let sends = 0;
const queries = [];
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    const native = {
      supported: true, thread_id: branch.codex_thread_id,
      goal: { thread_id: branch.codex_thread_id, objective, status: "active", token_budget: null, tokens_used: 145_200, time_used_seconds: 4_800, created_at: Date.now() / 1000 - 4_800, updated_at: Date.now() / 1000 },
      runtime: { status: "running", supervised: true, notifications_enabled: true, retry_attempts: 0, progress_interval_seconds: 900, last_checked_at: now, last_activity_at: now, notification_status: "sent", notification_sent_at: now, last_summary: "第一组配对测试已完成，正在核对长上下文测试结果。" },
    };
    const submitted = [];
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) {
        if (route.request().method() === "PUT") {
          const value = route.request().postDataJSON();
          submitted.push(value);
          native.goal.objective = value.objective;
        }
        body = native;
      } else if (path.endsWith("/native-goal/control")) {
        const action = route.request().postDataJSON().action;
        native.goal.status = action === "pause" ? "paused" : "active";
        native.runtime.status = action === "pause" ? "paused" : "running";
        body = native;
      } else if (path.endsWith("/native-ui")) body = detail;
      if (path.includes("/messages") && route.request().method() === "POST") {
        sends += 1;
        const value = route.request().postDataJSON();
        queries.push(value.content);
        body = {
          id: `query-${sends}`, branch_id: branch.id, role: "user", kind: "message",
          content: value.content, created_at: now,
          metadata_json: { origin: "web", send_status: "pending" },
        };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/ADAPT/branches/native-ui/results`);
    await expect(page.getByLabel("Goal execution", { exact: true })).toHaveText("Execution: Running");
    await page.screenshot({ path: `${output}/results-${viewport.width}.png`, fullPage: true });
    await page.getByRole("tab", { name: "goals", exact: true }).click();
    await expect(page.getByText("Every 15 min", { exact: true })).toBeVisible();
    await page.screenshot({ path: `${output}/running-${viewport.width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeVisible();
    await page.screenshot({ path: `${output}/paused-${viewport.width}.png`, fullPage: true });
    native.goal.status = "active";
    Object.assign(native.runtime, { status: "error", retry_attempts: 3, last_error: "Connection closed. Automatic recovery attempts exhausted; resume manually.", notification_status: "failed", notification_error: "Delivery timeout. The notification remains queued.", notification_sent_at: null });
    await page.reload();
    await expect(page.getByRole("button", { name: "Reconnect", exact: true })).toBeVisible();
    await expect(page.getByLabel("Goal execution", { exact: true })).toHaveText("Execution: Execution failed");
    const executionBox = await page.getByLabel("Goal execution", { exact: true }).boundingBox();
    if (!executionBox || executionBox.x < 0 || executionBox.x + executionBox.width > viewport.width) throw new Error(`Execution status clipped at ${viewport.width}`);
    await page.screenshot({ path: `${output}/error-${viewport.width}.png`, fullPage: true });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Page overflow at ${viewport.width}`);
    const overlaps = await page.locator(".goal-facts > div").evaluateAll((nodes) => nodes.filter((node) => node.scrollWidth > node.clientWidth + 1).length);
    if (overlaps) throw new Error(`Goal facts overflow at ${viewport.width}`);
    await page.locator(".goal-card").getByRole("button", { name: "Edit", exact: true }).click();
    const exact = "  完成我的正式目标\n\n不添加额外描述。  ";
    await page.getByRole("textbox", { name: /Objective/ }).fill(exact);
    await page.screenshot({ path: `${output}/edit-${viewport.width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Update goal", exact: true }).click();
    await expect.poll(() => submitted.length).toBe(1);
    if (submitted[0].objective !== exact || submitted[0].token_budget !== null) throw new Error("Goal input was changed");
    if (sends !== queries.length) throw new Error("Unexpected automatic Query");
    await page.getByRole("tab", { name: "results", exact: true }).click();
    const query = "  My exact query\n\nKeep my formatting.  ";
    await page.getByRole("textbox", { name: "Web turn message", exact: true }).fill(query);
    await page.getByRole("button", { name: "Send Web turn", exact: true }).click();
    await expect.poll(() => queries.at(-1)).toBe(query);
    await expect(page.getByRole("textbox", { name: "Web turn message", exact: true })).toHaveValue("");
    await page.screenshot({ path: `${output}/verbatim-query-${viewport.width}.png`, fullPage: true });
    await page.close();
  }
  if (failures.length || sends !== 3) throw new Error(JSON.stringify({ failures, sends }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 768, 390], failures, sends }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
