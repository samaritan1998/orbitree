import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";
import { workflowLinksFixture } from "../web/src/test/workflowFixture.ts";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-workflows");
const output = "/tmp/research-ops-workflow-links-ui";
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
const now = "2026-09-15T08:00:00Z";
const program = { id: "p", code: "ADAPT", slug: "adapt", name: "Model Adaptation", objective: "Model adaptation", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "ADAPT-TEST", program_id: "p", title: "Long-context paired evaluation", objective: "Complete the paired evaluation", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "workflow-test", work_item_id: "w", name: "Workflow integration", slug: "workflow-test", status: "active", mode: "thread", cwd: "/tmp/workflow-test", codex_thread_id: "workflow-test-thread", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const turn = { id: "recorded-turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "recorded", status: "completed", prompt: "Check the latest experiment", final_answer: "Training is complete. The paired evaluation is running. The session workflow contains the associated task evidence.", completed_at: now, started_at: now, metadata_json: {} };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [turn], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
let browser;
const errors = [];
const writes = [];
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 1024, height: 768 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => errors.push(error.message));
    const fixture = structuredClone(workflowLinksFixture);
    let failLinks = false;
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (route.request().method() !== "GET") writes.push(path);
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: true, goal: null, runtime: {} };
      else if (path.endsWith("/workflow-links")) {
        if (failLinks) { await route.fulfill({ status: 503, json: { detail: "Unavailable" } }); return; }
        body = fixture;
      }
      else if (path.endsWith("/workflow-test")) body = detail;
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/ADAPT/branches/workflow-test/results`);
    const region = page.getByRole("region", { name: "Session workflows" });
    const toggle = region.getByRole("button", { name: "Show all workflows" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("textbox", { name: "Web turn message", exact: true })).toBeEnabled();
    await expect(region.getByRole("link")).toHaveCount(2);
    const link = region.getByRole("link").first();
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer");
    await expect(link).toHaveAttribute("href", fixture.links[0].url);
    await page.screenshot({ path: `${output}/collapsed-${viewport.width}.png`, fullPage: true });
    await toggle.click();
    await expect(region.getByRole("link")).toHaveCount(3);
    await page.screenshot({ path: `${output}/expanded-${viewport.width}.png`, fullPage: true });
    failLinks = true;
    await region.getByRole("button", { name: "Refresh workflow links" }).click();
    await expect(region.getByRole("alert")).toContainText("Unavailable");
    await expect(region.getByRole("link")).toHaveCount(3);
    await expect(page.getByRole("textbox", { name: "Web turn message", exact: true })).toBeEnabled();
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) throw new Error(`Page overflow at ${viewport.width}`);
    const overflow = await region.locator(".workflow-links-heading, .workflow-links a").evaluateAll((nodes) => nodes.some((node) => node.scrollWidth > node.clientWidth + 2));
    if (overflow) throw new Error(`Workflow text overflow at ${viewport.width}`);
    await page.screenshot({ path: `${output}/error-${viewport.width}.png`, fullPage: true });
    failLinks = false;
    fixture.links = [];
    await region.getByRole("button", { name: "Refresh workflow links" }).click();
    await expect(region).toHaveCount(0);
    await expect(page.getByRole("textbox", { name: "Web turn message", exact: true })).toBeEnabled();
    await page.close();
  }
  if (errors.length || writes.length) throw new Error(JSON.stringify({ errors, writes }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 1024, 390], errors, writes }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
