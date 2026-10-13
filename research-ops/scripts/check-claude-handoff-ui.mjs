import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = resolve(process.argv[2] || "/tmp/research-ops-claude-personal-dist");
const output = "/tmp/research-ops-claude-handoff-ui";
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
const branch = { id: "claude-handoff-ui", work_item_id: "w", name: "Main", slug: "main", status: "active", mode: "write", cwd: "/tmp/infra-agent", codex_thread_id: "source-thread", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [{ id: "turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "test-turn", status: "completed", prompt: "Build the infra agent", final_answer: "Configuration and tests are ready. The next task is deployment verification.", completed_at: now, metadata_json: {} }], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
const handoff = { source: { runtime: "codex", thread_id: "source-thread", work_id: "COMMON-TEST" }, completed_turns: Array.from({ length: 6 }, (_, index) => ({ prompt: `Review configuration ${index}`, final_answer: "Configuration verified. Existing work and files remain available." })) };
const failures = [];
let browser;
let creates = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: true, goal: null };
      else if (path.endsWith("/execution")) body = { runtime: "codex", preferences: {}, effective: { identity: "api", label: "Company API", model: "gpt-6-astra", reasoning_effort: "max" }, identities: [] };
      else if (path.endsWith("/claude-handoff")) body = { handoff_hash: "frozen-preview", turn_count: 6, prompt: "Conversation handoff (reference history, not a new task)\n\n" + JSON.stringify(handoff, null, 2), account: "Personal Claude", model: "sonnet", effort: "high" };
      else if (path.endsWith("/continue-with-claude")) {
        const input = route.request().postDataJSON();
        if (input.expected_handoff_hash !== "frozen-preview" || !input.include_context || input.continuation !== "parallel") throw new Error("Invalid confirmed handoff");
        creates += 1;
        body = { ...branch, id: "claude-child", parent_branch_id: branch.id, runtime: "claude", name: "Claude · Infra agent" };
      } else if (path.endsWith("/claude-handoff-ui")) body = detail;
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/COMMON/branches/claude-handoff-ui/results`);
    await page.getByRole("button", { name: "Fork with Claude", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByText("Personal Claude · sonnet · high", { exact: true }).waitFor();
    if (!(await dialog.getByRole("textbox", { name: "First-turn context" }).inputValue()).includes("source-thread")) throw new Error("Preview lacks source context");
    await page.screenshot({ path: `${output}/dialog-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    const box = await dialog.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > viewport.width || box.height > viewport.height) throw new Error("Dialog exceeds viewport");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.getByRole("button", { name: "Fork with Claude", exact: true }).click();
    await dialog.getByRole("button", { name: "Create fork", exact: true }).click();
    await page.getByRole("button", { name: "Open Claude fork", exact: true }).waitFor();
    await page.close();
  }
  if (failures.length || creates !== 2 || branch.runtime !== "codex") throw new Error(JSON.stringify({ failures, creates }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], creates, failures }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
