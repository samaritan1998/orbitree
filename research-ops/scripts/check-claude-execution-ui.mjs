import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-claude-model-controls-20261002");
const output = "/tmp/research-ops-claude-execution-ui";
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
const now = "2026-10-02T16:00:00Z";
const program = { id: "p", code: "COMMON", slug: "common", name: "Common", objective: "", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "COMMON-TEST", program_id: "p", title: "Infra agent", objective: "", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "claude-execution-ui", work_item_id: "w", name: "Claude continuation", slug: "claude", status: "active", mode: "write", cwd: "/tmp/infra-agent", codex_thread_id: "claude-thread", runtime: "claude", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const detail = { branch, work_item: work, runtime: "claude", session: null, turns: [], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const initial = { runtime: "claude", preferences: {}, effective: { identity: "claude", label: "Personal Claude", provider: "claude-cli", model: "sonnet", reasoning_effort: "high" }, models: [{ id: "sonnet", name: "Sonnet" }, { id: "opus", name: "Opus" }, { id: "haiku", name: "Haiku" }], reasoning_efforts: ["low", "medium", "high", "xhigh", "max"] };
const failures = [];
let browser;
let saves = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    let execution = structuredClone(initial);
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: false, goal: null };
      else if (path.endsWith("/claude-execution")) {
        if (route.request().method() === "PUT") {
          const input = route.request().postDataJSON();
          if (JSON.stringify(input) !== JSON.stringify({ model: "opus", reasoning_effort: "max" })) throw new Error("Unexpected settings payload");
          saves += 1;
          execution = { ...execution, preferences: input, effective: { ...execution.effective, ...input } };
        }
        body = execution;
      } else if (path.endsWith("/claude-execution-ui")) body = detail;
      if (route.request().method() === "POST") throw new Error("Settings unexpectedly sent a message");
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/COMMON/branches/claude-execution-ui/results`);
    await page.getByRole("button", { name: /Personal Claude.*sonnet.*high/ }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("combobox", { name: "Model", exact: true }).selectOption("opus");
    await dialog.getByRole("combobox", { name: "Reasoning effort" }).selectOption("max");
    await page.screenshot({ path: `${output}/dialog-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    const box = await dialog.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > viewport.width || box.height > viewport.height) throw new Error("Dialog exceeds viewport");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: /Personal Claude.*sonnet.*high/ }).click();
    if (await dialog.getByRole("combobox", { name: "Model", exact: true }).inputValue() !== "sonnet") throw new Error("Cancel retained draft");
    await dialog.getByRole("combobox", { name: "Model", exact: true }).selectOption("opus");
    await dialog.getByRole("combobox", { name: "Reasoning effort" }).selectOption("max");
    await dialog.getByRole("button", { name: "Apply", exact: true }).click();
    await page.getByText("Saved for the next turn", { exact: true }).waitFor();
    await page.reload();
    await page.getByRole("button", { name: /Personal Claude.*opus.*max/ }).waitFor();
    await page.screenshot({ path: `${output}/saved-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.close();
  }
  if (failures.length || saves !== 2) throw new Error(JSON.stringify({ failures, saves }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], saves, failures }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
