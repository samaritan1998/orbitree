// Use only the dedicated acceptance gateway; creates one ordinary test account.
import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { randomBytes } from "node:crypto";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = process.argv[2] || "/srv/rops-team-preview";
const receipt = JSON.parse(await readFile(`${root}/control/preview.json`, "utf8"));
const origin = new URL(receipt.url).origin;
assert.equal(new URL(origin).hostname, "127.0.0.1");
assert.ok(receipt.username.startsWith("preview_"));
const username = "uitest_" + randomBytes(3).toString("hex");
const output = "/tmp/research-ops-team-create-ui";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
const failures = [];
let created = false;
const context = await browser.newContext();
try {
  const login = await context.request.post(`${origin}/api/team/login`, {
    headers: { Origin: origin }, data: { username: receipt.username, password: receipt.password },
  });
  assert.equal(login.status(), 200);
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
    const page = await context.newPage({ viewport });
    await page.setViewportSize(viewport);
    page.on("pageerror", (error) => failures.push(error.message));
    await page.goto(`${origin}/settings`);
    await page.getByRole("button", { name: "新增用户", exact: true }).click();
    const create = page.getByRole("button", { name: "创建并生成邀请", exact: true });
    await expect(create).toBeDisabled();
    await page.getByLabel(/^账号名/).fill(username);
    await page.getByLabel(/^显示名称/).fill("新增用户验收");
    await expect(create).toBeEnabled();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow ${viewport.width}`);
    await page.locator(".team-account-create").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${output}/create-${viewport.width}.png`, fullPage: true });
    if (viewport.width !== 320) {
      await page.getByRole("button", { name: "取消", exact: true }).click();
      await expect(page.getByRole("form", { name: "新增用户", exact: true })).toHaveCount(0);
      await page.close();
      continue;
    }
    const response = page.waitForResponse((res) => res.url() === `${origin}/api/team/accounts` && res.request().method() === "POST", { timeout: 70000 });
    await create.click();
    await expect(create).toBeDisabled();
    const result = await response;
    assert.equal(result.status(), 201, await result.text());
    created = true;
    const data = await result.json();
    assert.equal(data.account.role, "member");
    assert.equal(data.account.setup.status, "ready");
    await writeFile(`${root}/control/account-create-check.json`, JSON.stringify({ username }), { mode: 0o600 });
    await expect(page.getByText(`${username} 的邀请链接`, { exact: true })).toBeVisible();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    // Never store a real invitation token in a screenshot.
    await page.screenshot({ path: `${output}/created-320.png`, fullPage: true, mask: [page.locator(".team-invite code")] });
    const member = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const activate = await member.newPage();
    activate.on("pageerror", (error) => failures.push(error.message));
    await activate.goto(data.url);
    await expect(activate.getByRole("heading", { name: "激活账号", exact: true })).toBeVisible();
    assert.equal(new URL(activate.url()).hash, "");
    const password = randomBytes(24).toString("base64url");
    await activate.getByLabel(/设置密码/).fill(password);
    await activate.getByLabel(/确认密码/).fill(password);
    await activate.getByRole("button", { name: "激活并登录", exact: true }).click();
    await expect(activate.getByRole("heading", { name: "Today", exact: true })).toBeVisible({ timeout: 30000 });
    const programs = await member.request.get(`${origin}/api/programs`);
    assert.deepEqual((await programs.json()).map((program) => program.code), ["FREE"]);
    assert.equal((await member.request.get(`${origin}/api/team/accounts`)).status(), 403);
    await activate.goto(`${origin}/settings`);
    await expect(activate.getByRole("button", { name: "新增用户", exact: true })).toHaveCount(0);
    await member.close();
    await page.close();
  }
  assert.deepEqual(failures, []);
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390, 320], account: username, realWorker: true, activation: true, memberIsolation: true, failures }));
} finally {
  if (created) {
    await context.request.put(`${origin}/api/team/accounts/${username}`, {
      headers: { Origin: origin }, data: { enabled: false },
    }).catch(() => {});
  }
  await context.request.post(`${origin}/api/team/logout`, { headers: { Origin: origin }, data: {} }).catch(() => {});
  await browser.close();
}
