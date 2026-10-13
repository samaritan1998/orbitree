import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { randomBytes } from "node:crypto";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const receiptPath = process.argv[2] || "/srv/rops-team-preview/control/preview.json";
const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
const origin = new URL(receipt.url).origin;
const output = "/tmp/research-ops-team-ui";
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
const failures = [];
const tag = randomBytes(2).toString("hex").toUpperCase();
try {
  const context = await browser.newContext();
  const first = await context.newPage();
  first.on("pageerror", (error) => failures.push(error.message));
  if (!receipt.password) {
    await first.goto(receipt.url);
    await expect(first.getByRole("heading", { name: "激活账号" })).toBeVisible();
    assert.equal(new URL(first.url()).hash, "");
    receipt.password = randomBytes(24).toString("base64url");
    await first.getByLabel(/设置密码/).fill(receipt.password);
    await first.getByLabel(/确认密码/).fill(receipt.password);
    await first.getByRole("button", { name: "激活并登录" }).click();
    await expect(first.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
    await writeFile(receiptPath, JSON.stringify(receipt), { mode: 0o600 });
  } else {
    await first.goto(origin);
    await first.getByLabel(/^账号/).fill(receipt.username);
    await first.getByLabel(/^密码/).fill(receipt.password);
    await first.getByRole("button", { name: "登录", exact: true }).click();
    await expect(first.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
  }
  await first.close();
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
    const page = await context.newPage({ viewport });
    await page.setViewportSize(viewport);
    page.on("pageerror", (error) => failures.push(error.message));
    await page.goto(`${origin}/settings`);
    await expect(page.getByRole("heading", { name: "工作方向", exact: true })).toBeVisible();
    await expect(page.getByText(`@${receipt.username}`, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "新增方向", exact: true }).click();
    const code = "UI" + viewport.width + tag;
    await page.getByLabel(/^方向代码/).fill(code);
    await page.getByLabel(/^方向名称/).fill(`Research ${viewport.width} ${tag}`);
    await page.getByLabel("目标（选填）", { exact: true }).fill("Independent workspace acceptance");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Overflow ${viewport.width}`);
    await page.screenshot({ path: `${output}/settings-${viewport.width}.png`, fullPage: true });
    await page.getByRole("button", { name: "保存方向", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "工作方向已保存" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("button", { name: `编辑 Research ${viewport.width} ${tag}`, exact: true })).toBeVisible();
    await page.close();
  }
  const logout = await context.newPage();
  await logout.goto(`${origin}/settings`);
  await logout.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(logout.getByRole("heading", { name: "登录", exact: true })).toBeVisible();
  await logout.setViewportSize({ width: 390, height: 844 });
  await logout.screenshot({ path: `${output}/login-mobile.png`, fullPage: true });
  assert.equal((await context.request.get(`${origin}/api/programs`)).status(), 401);
  assert.deepEqual(failures, []);
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390, 320], real_worker: true, failures }));
} catch (error) {
  for (const context of browser.contexts()) for (const page of context.pages()) {
    await page.screenshot({ path: `${output}/failure.png`, fullPage: true }).catch(() => {});
  }
  throw error;
} finally { await browser.close(); }
