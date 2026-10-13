import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-claude-workspace-20261003");
const output = "/tmp/research-ops-claude-approvals-ui";
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
const now = "2026-10-03T08:00:00Z";
const program = { id: "p", code: "COMMON", slug: "common", name: "Common", objective: "", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "COMMON-TEST", program_id: "p", title: "Infra agent", objective: "", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "claude-approval-ui", work_item_id: "w", name: "Claude continuation", slug: "claude", status: "active", mode: "write", cwd: "/tmp/infra-agent", codex_thread_id: "claude-thread", runtime: "claude", context_snapshot: { workspace_snapshot: { state: "ready", file_count: 633 } }, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: false, can_interrupt: true, web_turn_status: "running", local_turn_active: false };
const approval = { id: "permission-1", branch_id: branch.id, request_id: "claude:permission-1", method: "claude/tool/requestApproval", status: "pending", params: { tool: "Read", blocked_path: "/outside/experiment/checkpoint/README.md", input: { file_path: "/outside/experiment/checkpoint/README.md" } }, created_at: now };
const detail = { branch, work_item: work, runtime: "claude", session: null, turns: [], messages: [], memory: [], external_jobs: [], goals: [], approvals: [approval], activity: [], conversation_state: state };
const failures = [];
let browser;
let resolves = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    let current = structuredClone(detail);
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
      else if (path.endsWith("/claude-execution")) body = { effective: { label: "Personal Claude", model: "opus", reasoning_effort: "high" }, models: [], reasoning_efforts: [] };
      else if (path.endsWith("/claude-approval-ui")) body = current;
      else if (path === "/api/approvals/permission-1/resolve") {
        const decision = viewport.width === 1440 ? "accept" : "decline";
        if (route.request().postDataJSON().decision !== decision) throw new Error("Wrong approval decision");
        resolves += 1;
        current.approvals = [];
        body = { ...approval, status: "resolved" };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/COMMON/branches/claude-approval-ui/results`);
    const panel = page.getByRole("region", { name: "Claude permissions" });
    await panel.getByRole("button", { name: "Allow once", exact: true }).waitFor();
    await page.getByText("Workspace snapshot · 633 files", { exact: true }).waitFor();
    const snapshotBox = await page.locator(".workspace-snapshot").boundingBox();
    if (!snapshotBox || snapshotBox.height > 50) throw new Error("Collapsed snapshot is not compact");
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    const box = await panel.boundingBox();
    if (!box || box.x < 0 || box.x + box.width > viewport.width || box.height > viewport.height) throw new Error("Permission panel exceeds viewport");
    await page.screenshot({ path: `${output}/approval-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await panel.getByRole("button", { name: viewport.width === 1440 ? "Allow once" : "Deny", exact: true }).click();
    await panel.waitFor({ state: "detached" });
    await page.close();
  }
  if (failures.length || resolves !== 2) throw new Error(JSON.stringify({ failures, resolves }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], resolves, failures }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
