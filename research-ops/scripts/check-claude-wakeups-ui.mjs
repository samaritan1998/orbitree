import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-claude-wakeups-20261006");
const output = "/tmp/research-ops-claude-wakeups-ui";
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const path = resolve(root, "." + new URL(req.url, "http://localhost").pathname);
  const file = path.startsWith(root + "/") && extname(path) ? path : resolve(root, "index.html");
  try { res.setHeader("Content-Type", mime[extname(file)] || "application/octet-stream"); res.end(await readFile(file)); }
  catch { res.writeHead(404).end(); }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
await mkdir(output, { recursive: true });
const origin = `http://127.0.0.1:${server.address().port}`;
const now = new Date().toISOString();
const program = { id: "p", code: "ADAPT", slug: "adapt", name: "Model Adaptation", objective: "", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "ADAPT-TEST", program_id: "p", title: "Claude platform verification", objective: "", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "claude-wakeup-ui", work_item_id: "w", name: "Main", slug: "main", status: "active", mode: "write", cwd: "/tmp/fixture", codex_thread_id: "claude-thread", runtime: "claude", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "completed", local_turn_active: false };
const detail = { branch, work_item: work, runtime: "claude", session: null, turns: [], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const failures = [];
let browser;
let cancellations = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    const scheduled = { check: null, runs: [], jobs: [], default_prompt: "Check once.", notification: null, notifications_enabled: true,
      worker: { enabled: true, status: "healthy", last_tick_at: now, error: null },
      wakeups: [{ id: "wakeup-1", status: "scheduled", prompt: "Read the debug receipt and report whether it is ready. Do not change the experiment.", due_at: new Date(Date.now() + 600_000).toISOString(), reason: null, source_turn_id: "source-turn", message_id: null }] };
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/team/session")) body = { enabled: false };
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: false, goal: null };
      else if (path.endsWith("/claude-execution")) body = { effective: { label: "Personal Claude", model: "opus", reasoning_effort: "high" }, models: [], reasoning_efforts: [] };
      else if (path.endsWith("/claude-wakeup-ui")) body = detail;
      else if (path.endsWith("/scheduled-check")) body = scheduled;
      else if (path.endsWith("/wakeup-1/cancel")) {
        cancellations += 1;
        scheduled.wakeups[0].status = "cancelled";
        scheduled.wakeups[0].reason = "Cancelled by user";
        body = scheduled;
      } else if (path.endsWith("/scheduled-check/prompt")) throw new Error("Opening a native wakeup must not call a model");
      else if (route.request().method() !== "GET") throw new Error(`Unexpected mutation: ${path}`);
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/ADAPT/branches/claude-wakeup-ui/results`);
    await page.getByText("Claude wakeup · Scheduled", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Scheduled checks", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("region", { name: "Claude wakeups" }).waitFor();
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    const box = await dialog.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > viewport.width || box.height > viewport.height) throw new Error("Wakeup dialog exceeds viewport");
    await page.screenshot({ path: `${output}/wakeup-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("button", { name: "Cancel this wakeup", exact: true }).click();
    await dialog.locator("summary").filter({ hasText: "Cancelled" }).click();
    await page.getByText("Cancelled by user", { exact: true }).waitFor();
    await page.close();
  }
  if (failures.length || cancellations !== 2) throw new Error(JSON.stringify({ failures, cancellations }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], cancellations, failures }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
