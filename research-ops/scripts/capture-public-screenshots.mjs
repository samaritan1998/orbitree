// Render the actual frontend using synthetic, read-only API fixtures.
// No production backend, account, session transcript, or external URL is used.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist");
const output = resolve("docs/images");
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const path = resolve(root, "." + new URL(req.url, "http://localhost").pathname);
  if (!path.startsWith(root + "/")) { res.writeHead(403).end(); return; }
  const file = extname(path) ? path : resolve(root, "index.html");
  try { res.setHeader("Content-Type", mime[extname(file)] || "application/octet-stream"); res.end(await readFile(file)); }
  catch { res.writeHead(404).end(); }
});
await new Promise(done => server.listen(0, "127.0.0.1", done));
await mkdir(output, { recursive: true });
const origin = `http://127.0.0.1:${server.address().port}`;
const now = "2026-01-01T09:00:00Z";
const programs = [
  ["HARN", "Harness / Office", "#26745a"], ["COST", "Model Cost", "#ae670f"],
  ["ADAPT", "Model Adaptation", "#396ca8"], ["COMMON", "Shared Platform", "#954574"],
  ["FREE", "Free Work", "#617745"],
].map(([code, name, color], i) => ({ id: code, code, slug: code.toLowerCase(), name, color, objective: "Reusable research workflows", sort_order: i, active: true }));
const program = programs[0];
const work = { id: "example-work", human_id: "HARN-0001", program_id: program.id, title: "Presentation quality experiment", objective: "Compare two presentation workflows on a synthetic sample.", kind: "task", lifecycle: "ready", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "example-main", work_item_id: work.id, name: "Baseline", slug: "baseline", status: "active", mode: "thread", cwd: "/tmp/example-work", codex_thread_id: "example-session", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const child = { ...branch, id: "example-fork", parent_branch_id: branch.id, name: "Layout experiment", slug: "layout", codex_thread_id: "example-child-session" };
const conversation = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const turn = { id: "example-turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "example-turn", status: "completed", prompt: "Compare the two presentation layouts and summarize the next step.", final_answer: "The baseline and layout variant are ready for review.\n\n- Both preserve editable slide content.\n- The layout variant has clearer visual hierarchy.\n- Next: run the same evaluation criteria on additional synthetic samples.\n\nThis screenshot uses fictional data; no real experiment was run.", completed_at: now, metadata_json: { origin: "web" } };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [turn], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: conversation };
const errors = [];
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => { localStorage.setItem("research-ops:ambience", "none"); });
  await page.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    assert.equal(url.origin, origin, "Screenshot attempted an external request");
    if (!url.pathname.startsWith("/api/")) return route.continue();
    assert.equal(request.method(), "GET", "Screenshot must not mutate state");
    const path = url.pathname;
    let body = [];
    if (path === "/api/programs") body = programs;
    else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch, child], hidden_branches: [], edges: [], sessions: [] };
    else if (path.endsWith("/desk")) body = { work_item: work, branch };
    else if (path.endsWith("/conversation-state")) body = conversation;
    else if (path.endsWith("/native-goal")) body = { supported: true, goal: null, runtime: null };
    else if (path.endsWith("/workflow-links")) body = { links: [] };
    else if (path.endsWith("/runtime/state")) body = { runtime: "codex", status: "idle", queue: {} };
    else if (path.endsWith("/turns")) body = { items: [turn], total: 1, next_offset: null };
    else if (path.endsWith("/execution")) body = { runtime: "codex", preferences: {}, effective: { identity: "api", label: "API account", model: "Configured model", reasoning_effort: "high", provider: "configured", base_url: "" }, identities: [], api_isolated: true, login_command: "rops auth login" };
    else if (path.endsWith("/example-main")) body = detail;
    await route.fulfill({ json: body });
  });
  await page.goto(`${origin}/programs/HARN`);
  await expect(page.getByText(work.title).first()).toBeVisible();
  await expect(page.getByText(child.name).first()).toBeVisible();
  await page.screenshot({ path: `${output}/program-tree.png`, fullPage: true, animations: "disabled" });
  await page.goto(`${origin}/programs/HARN/branches/example-main/results`);
  await expect(page.getByText("The baseline and layout variant are ready for review.")).toBeVisible();
  await page.screenshot({ path: `${output}/live-activity.png`, fullPage: true, animations: "disabled" });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ syntheticScreenshots: 2, productionRequests: 0, errors }));
} finally {
  if (browser) await browser.close();
  await new Promise(done => server.close(done));
}
