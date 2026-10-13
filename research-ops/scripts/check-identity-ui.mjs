import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, resolve } from "node:path";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = resolve(process.argv[2] || "/tmp/research-ops-identities-dist");
const output = "/tmp/research-ops-identity-ui";
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
const origin = process.env.RESEARCH_OPS_UI_ORIGIN || `http://127.0.0.1:${server.address().port}`;
const now = "2026-09-10T06:00:00Z";
const program = { id: "p", code: "ADAPT", slug: "adapt", name: "Model Adaptation", objective: "Model adaptation", color: "#28704f", sort_order: 1, active: true };
const work = { id: "w", human_id: "ADAPT-TEST", program_id: "p", title: "Identity verification", objective: "Verify manual execution selection", kind: "task", lifecycle: "active", flow: "actionable", priority: 50, acceptance_criteria: [], labels: [], revision: 1, created_at: now, updated_at: now };
const branch = { id: "identity-ui", work_item_id: "w", name: "Identity verification", slug: "identity-ui", status: "active", mode: "thread", cwd: "/tmp/identity-ui", codex_thread_id: "identity-ui-thread", runtime: "codex", context_snapshot: {}, memory_cursor: 0, created_at: now, updated_at: now };
const state = { can_send: true, can_interrupt: false, web_turn_status: "idle", local_turn_active: false };
const original = { identity: "api", label: "Company API", model: "gpt-6-astra", reasoning_effort: "max", provider: "gateway", base_url: "https://company.invalid" };
let execution = { runtime: "codex", preferences: {}, effective: original, identities: [{ id: "api", label: "Company API", ready: true }, { id: "chatgpt", label: "Personal Codex", ready: true }, { id: "oneapi", label: "Company OneAPI", ready: true, default_model: "DeepSeek-V4.1-Flash", default_reasoning_effort: "high" }], api_isolated: true, login_command: "rops auth login" };
const detail = { branch, work_item: work, runtime: "codex", session: null, turns: [{ id: "turn", branch_id: branch.id, thread_id: branch.codex_thread_id, external_turn_id: "test-turn", status: "completed", prompt: "Verify identity isolation", final_answer: "The API and personal credentials are isolated. Running work keeps its recorded identity.", completed_at: now, metadata_json: { execution: original } }], messages: [], memory: [], external_jobs: [], goals: [], approvals: [], activity: [], conversation_state: state };
let browser;
const failures = [];
let sends = 0;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    execution = { ...execution, effective: original };
    const page = await browser.newPage({ viewport });
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let body = [];
      if (path === "/api/programs") body = [program];
      else if (path.endsWith("/tree")) body = { program, work_items: [work], branches: [branch], hidden_branches: [], edges: [], sessions: [] };
      else if (path.endsWith("/desk")) body = { work_item: work, branch };
      else if (path.startsWith("/api/settings/identities/") && path.endsWith("/models")) {
        const identity = path.split("/")[4];
        body = { identity, allow_custom: identity !== "chatgpt", models: identity === "oneapi" ? [
          { id: "DeepSeek-V4.1-Flash", name: "DeepSeek-V4.1-Flash", reasoning_efforts: [], default_reasoning_effort: null, is_default: true },
          { id: "gpt-6-astra", name: "gpt-6-astra", reasoning_efforts: [], default_reasoning_effort: null, is_default: false },
        ] : [
          { id: identity === "api" ? "gpt-6-astra" : "account-model", name: identity === "api" ? "GPT-6-Astra" : "Account model", reasoning_efforts: ["high", "max", "ultra"], default_reasoning_effort: "high", is_default: true },
          { id: "limited-model", name: "Limited model", reasoning_efforts: ["low", "high"], default_reasoning_effort: "low", is_default: false },
        ] };
      }
      else if (path.endsWith("/execution")) {
        if (route.request().method() === "PUT") {
          const choice = route.request().postDataJSON();
          execution = { ...execution, preferences: choice, effective: { ...original, ...choice, label: { api: "Company API", chatgpt: "Personal Codex", oneapi: "Company OneAPI" }[choice.identity] } };
        }
        body = execution;
      } else if (path.endsWith("/conversation-state")) body = state;
      else if (path.endsWith("/native-goal")) body = { supported: true, thread_id: branch.codex_thread_id, goal: null };
      else if (path.endsWith("/identity-ui")) body = detail;
      if (path.includes("/messages") && route.request().method() === "POST") sends += 1;
      await route.fulfill({ json: body });
    });
    await page.goto(`${origin}/programs/ADAPT/branches/identity-ui/results`);
    const control = page.getByRole("button", { name: /Company API.*gpt-6-astra/ });
    await control.waitFor({ timeout: 30_000 });
    await control.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/branch-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await control.click();
    await page.getByLabel("Identity", { exact: true }).selectOption("chatgpt");
    await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("account-model");
    await page.getByRole("combobox", { name: "Reasoning effort" }).selectOption("ultra");
    if (await page.getByRole("button", { name: "Apply", exact: true }).isEnabled()) throw new Error("Personal switch lacked consent");
    await page.getByRole("checkbox", { name: /personal account/ }).check();
    await page.screenshot({ path: `${output}/dialog-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    if (overflow) throw new Error(`Horizontal overflow at ${viewport.width}`);
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await page.getByText("Saved for the next turn", { exact: true }).waitFor();
    await page.getByRole("button", { name: /Personal Codex.*account-model/ }).click();
    await page.getByLabel("Identity", { exact: true }).selectOption("oneapi");
    await page.getByRole("option", { name: "DeepSeek-V4.1-Flash", exact: true }).waitFor({ state: "attached" });
    if (await page.getByRole("combobox", { name: "Model", exact: true }).inputValue() !== "DeepSeek-V4.1-Flash") throw new Error("OneAPI model default missing");
    if (await page.getByRole("combobox", { name: "Reasoning effort" }).inputValue() !== "high") throw new Error("OneAPI effort default missing");
    if (execution.effective.identity !== "chatgpt") throw new Error("Draft identity was applied automatically");
    const effort = page.getByRole("combobox", { name: "Reasoning effort" });
    for (const value of ["none", "minimal", "low", "medium", "high", "xhigh", "ultra", "max"]) {
      await effort.selectOption(value);
      if (await effort.inputValue() !== value) throw new Error(`OneAPI effort ${value} is not selectable`);
    }
    await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("gpt-6-astra");
    if (await effort.inputValue() !== "max") throw new Error("Changing OneAPI model reset the draft effort");
    await page.screenshot({ path: `${output}/oneapi-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`OneAPI overflow at ${viewport.width}`);
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await page.getByText("Saved for the next turn", { exact: true }).waitFor();
    if (execution.effective.identity !== "oneapi") throw new Error("OneAPI preference was not saved");
    if (execution.effective.reasoning_effort !== "max") throw new Error("OneAPI effort was not saved");
    if (detail.turns[0].metadata_json.execution.identity !== "api") throw new Error("Historical execution was changed");
    await page.reload();
    await page.getByRole("button", { name: /Company OneAPI.*gpt-6-astra.*max/ }).click();
    if (await page.getByRole("combobox", { name: "Reasoning effort" }).inputValue() !== "max") throw new Error("Reload lost the OneAPI effort");
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.close();
  }
  if (failures.length || sends) throw new Error(JSON.stringify({ failures, sends }));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], failures, sends }));
} finally {
  if (browser) await browser.close();
  await new Promise((done) => server.close(done));
}
