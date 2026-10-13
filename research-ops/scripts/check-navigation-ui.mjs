import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = resolve(process.argv[2] || "web/dist-navigation-20261003");
const output = "/tmp/research-ops-navigation-ui";
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
const programs = [
  ["HARN", "Harness / Office", "#26745a"], ["COST", "Model Cost", "#ae670f"],
  ["ADAPT", "Model Adaptation", "#396ca8"], ["COMMON", "Shared Platform", "#954574"],
  ["FREE", "Free Work", "#617745"],
].map(([code, name, color], index) => ({ id: code, code, slug: code.toLowerCase(), name, color, objective: "", sort_order: index, active: true }));
const works = programs.slice(0, 3).map((program, index) => ({
  id: `work-${index}`, human_id: `${program.code}-0001`, program_id: program.id,
  title: ["PPT trajectory evaluation", "GLM FP8 paired short run", "Model adaptation"][index],
  objective: "", kind: "task", lifecycle: "ready", flow: "actionable", priority: 50,
  acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now,
}));
const monitor = {
  enabled: true, notifications_enabled: true, users: ["test-user"], group_id: "test-group",
  state: { last_success_at: now }, jobs: [{ id: "j1", job_name: "training-canary", user: "test-user", state: "running", cluster: "cluster-a", provider_status: "Running", last_observed_at: now, last_probe_at: now }],
  events: [], notifications: [],
};
const clusters = {
  schema_version: 1, mode: "shadow", observed_at: now, resources_observed_at: now, jobs_observed_at: now,
  source: { kind: "live", fetched_at: now, cache_hit: false, degraded: false, stale: false, age_seconds: 1 },
  coverage: { source: "ltp_user_directory", complete: true, requested_users: 1, successful_users: 1, failed_users: 0, failed_user_names: [] },
  summary: { clusters: 0, resource_pools: 0, resource_domains: 0, available_cards: 0, free_nodes: 0, running_jobs: 0, queued_jobs: 0, running_nodes: 0, queued_nodes: 0, profiled_jobs: 0, estimated_jobs: 0, checkpoint_interval_jobs: 0 },
  categories: {}, clusters: [], warnings: [],
};
const reflection = {
  id: "reflection", reflection_date: "2026-10-02", timezone: "Asia/Shanghai", status: "completed",
  source_counts: { completed_turns: 3, work_item_titles: [{ id: "ADAPT-0001", title: "Model adaptation", program: "ADAPT" }] },
  evidence_refs: [], report_markdown: "## 今日总结\n\n模型适配和对照实验已完成。", notification_status: "sent", agent_model: "configured",
};
const failures = [];
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 1280, height: 720 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
    const mobile = viewport.width < 820;
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    const requests = [];
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      requests.push(path);
      assert.equal(route.request().method(), "GET", "Navigation must not mutate data");
      let body = [];
      if (path === "/api/programs") body = programs;
      else if (path === "/api/today") body = { continue_items: works, needs_decision: [], waiting_external: [], suggested_next: [], active_sessions: 0, pending_merges: 0 };
      else if (path === "/api/ltp-monitor") body = monitor;
      else if (path === "/api/clusters/overview") body = clusters;
      else if (path === "/api/daily-reflections") body = [reflection];
      else if (path.endsWith("/tree")) body = { program: programs[4], work_items: [], branches: [], hidden_branches: [], edges: [], sessions: [] };
      else if (path === "/api/skills") body = [{ name: "example-skill", directory_name: "example-skill", description: "Example installed procedure", scope: "personal", install_path: "/tmp/skills/example", source_path: "/tmp/skills/example", source_available: true, supporting_files: 1, valid: true, updated_at: now }];
      await route.fulfill({ json: body });
    });
    if (mobile) await page.addInitScript(() => localStorage.setItem("research-ops:sidebar", "collapsed"));
    const checkLayout = async () => {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow at ${viewport.width}`);
      const navButtons = await page.locator(mobile ? ".mobile-nav button" : ".sidebar button").evaluateAll((buttons) => buttons.filter((button) => button.getBoundingClientRect().height > 0).map((button) => {
        const { x, y, width, height } = button.getBoundingClientRect();
        return { x, y, width, height, name: button.getAttribute("aria-label") };
      }));
      assert.ok(navButtons.every((box) => box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1), JSON.stringify(navButtons));
      assert.equal(await page.locator("button button").count(), 0, "Nested buttons");
      if (!mobile && !await page.locator(".sidebar-collapsed").count()) {
        const titleBox = await page.locator(".brand-copy").boundingBox();
        assert.ok(titleBox && titleBox.y >= 0 && titleBox.y + titleBox.height <= 72, "Brand text is clipped");
      }
    };
    const checkMenu = async () => {
      await expect.poll(async () => {
        const box = await page.locator(".navigation-popover").boundingBox();
        return Boolean(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1);
      }, { message: `Menu exceeds viewport ${viewport.width}` }).toBe(true);
    };
    await page.goto(`${origin}/today`);
    await expect(page.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "今日", exact: true }).filter({ visible: true })).toHaveAttribute("aria-current", "page");
    await checkLayout();
    await page.screenshot({ path: `${output}/today-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    if (!mobile) {
      await page.getByRole("button", { name: "工作方向", exact: true }).click();
      await page.reload();
      await expect(page.getByRole("button", { name: "工作方向", exact: true })).toHaveAttribute("aria-expanded", "false");
      await expect(page.getByRole("button", { name: "Free Work", exact: true })).toBeHidden();
      await page.getByRole("button", { name: "Collapse sidebar" }).click();
    }
    await page.getByRole("button", { name: "工作方向", exact: true }).press("Enter");
    const directions = page.getByRole("menu");
    await expect(directions.getByRole("menuitem")).toHaveCount(5);
    await checkMenu();
    await page.keyboard.press("Escape");
    if (!mobile) await page.getByRole("button", { name: "Expand sidebar" }).click();

    await page.getByRole("button", { name: "每日复盘", exact: true }).click();
    await expect(page.getByRole("heading", { name: "每日复盘", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "今日总结", exact: true })).toBeVisible();
    assert.equal(requests.includes("/api/memory"), false);
    await expect(page.getByRole("tab")).toHaveCount(0);
    await page.getByRole("button", { name: "更多", exact: true }).click();
    await checkMenu();
    await page.screenshot({ path: `${output}/more-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    const more = page.getByRole("menu");
    await expect(more.getByRole("menuitem")).toHaveCount(mobile ? 7 : 6);
    await more.getByRole("menuitem", { name: "记忆账本", exact: true }).click();
    await expect(page).toHaveURL(`${origin}/memory?view=ledger`);
    await page.reload();
    await expect(page.getByRole("heading", { name: "记忆账本" })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { name: "每日复盘", exact: true })).toBeVisible();

    await page.getByRole("button", { name: "任务与资源", exact: true }).click();
    await expect(page.getByRole("heading", { name: "任务监控" })).toBeVisible();
    assert.equal(requests.includes("/api/clusters/overview"), false);
    await checkLayout();
    await page.screenshot({ path: `${output}/resources-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("tab", { name: "集群资源" }).click();
    await expect(page).toHaveURL(`${origin}/clusters`);
    await page.reload();
    await expect(page.getByRole("tab", { name: "集群资源" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "集群资源" })).toBeVisible();
    await checkLayout();
    await page.goBack();
    await expect(page.getByRole("tab", { name: "任务监控" })).toHaveAttribute("aria-selected", "true");

    await page.getByRole("button", { name: "更多", exact: true }).click();
    await page.getByRole("menuitem", { name: "技能", exact: true }).click();
    await expect(page.getByText("Example installed procedure")).toBeVisible();
    assert.equal(requests.includes("/api/skill-proposals"), false);
    await expect(page.getByRole("tab")).toHaveCount(0);
    await page.getByRole("button", { name: "更多", exact: true }).click();
    await page.getByRole("menuitem", { name: "技能审核", exact: true }).click();
    await page.reload();
    await expect(page).toHaveURL(`${origin}/skills?view=proposals`);
    await expect(page.getByRole("heading", { name: "技能审核" })).toBeVisible();
    await checkLayout();
    await page.close();
  }
  assert.deepEqual(failures, []);
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 1280, 390, 320], failures, mutations: 0 }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
