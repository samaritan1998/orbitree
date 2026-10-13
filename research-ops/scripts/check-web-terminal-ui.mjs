import { createServer } from "node:net";
import { mkdtemp, readFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { once } from "node:events";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const frontend = resolve(process.argv[2] || "web/dist-terminal-20261006");
const root = await mkdtemp(join(tmpdir(), "rops-terminal-ui-"));
const output = "/tmp/research-ops-web-terminal-ui";
await mkdir(output, { recursive: true });
const reserve = createServer();
await new Promise((done) => reserve.listen(0, "127.0.0.1", done));
const port = reserve.address().port;
await new Promise((done) => reserve.close(done));
const child = spawn(resolve(".venv/bin/python"), ["scripts/terminal-ui-fixture.py", "--port", String(port), "--root", root, "--frontend", frontend], { stdio: ["ignore", "pipe", "pipe"] });
let serverErrors = "";
child.stderr.on("data", (chunk) => { serverErrors += chunk.toString(); });
const origin = `http://127.0.0.1:${port}`;
const errors = [];
const frames = [];
let browser;
let activePage;
try {
  for (let i = 0; i < 200; i++) {
    if (child.exitCode !== null) throw new Error(serverErrors);
    try { if ((await fetch(`${origin}/api/terminal`)).ok) break; } catch {}
    await new Promise((done) => setTimeout(done, 100));
  }
  const key = await readFile(join(root, "runtime/web-terminal/unlock.key"), "utf8");
  browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] });
  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    const sentInput = [];
    activePage = page;
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("websocket", (ws) => {
      for (const direction of ["framesent", "framereceived"]) ws.on(direction, ({ payload }) => {
        try {
          const message = JSON.parse(String(payload));
          if (direction === "framesent" && message.type === "input") sentInput.push(message.data);
          frames.push({ direction, type: message.type || "auth", seq: message.seq, size: message.data?.length });
          if (frames.length > 60) frames.shift();
        } catch {}
      });
    });
    await page.goto(`${origin}/terminal`);
    await page.getByLabel("终端口令", { exact: true }).waitFor();
    await page.screenshot({ path: `${output}/terminal-locked-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByLabel("终端口令", { exact: true }).fill(key);
    await page.getByRole("button", { name: "解锁并连接", exact: true }).click();
    await page.getByText("已连接", { exact: true }).waitFor();
    const command = async (text) => {
      await page.locator(".xterm-helper-textarea").focus();
      await page.keyboard.type(text);
      await page.keyboard.press("Enter");
    };
    await command(`export ROPS_E2E_PERSIST=value${viewport.width}; printf '\\124\\105\\122\\115\\111\\116\\101\\114_READY\\n'`);
    await expect(page.locator(".xterm-rows")).toContainText("TERMINAL_READY");
    await command("sleep 30");
    await page.getByRole("button", { name: "中断前台命令（Ctrl+C）", exact: true }).click();
    await command("printf 'AFTER_%s\\n' 'INTERRUPT'");
    await expect(page.locator(".xterm-rows")).toContainText("AFTER_INTERRUPT");
    await page.reload();
    await page.getByText("已连接", { exact: true }).waitFor();
    await command('printf "RESTORED:%s\\n" "$ROPS_E2E_PERSIST"');
    await expect(page.locator(".xterm-rows")).toContainText(`RESTORED:value${viewport.width}`);
    await expect(page.getByRole("button", { name: "读屏模式", exact: true })).toHaveAttribute("aria-pressed", "false");
    await page.locator(".xterm-helper-textarea").focus();
    const insertedAt = sentInput.length;
    await page.keyboard.insertText("\u4e2d\u6587\u8f93\u5165");
    await expect.poll(() => sentInput.slice(insertedAt).join(""), { timeout: 3000 }).toBe("\u4e2d\u6587\u8f93\u5165");
    await page.keyboard.press("Control+u");
    const cdp = await page.context().newCDPSession(page);
    const composedAt = sentInput.length;
    await cdp.send("Input.imeSetComposition", { text: "ni", selectionStart: 2, selectionEnd: 2 });
    await cdp.send("Input.imeSetComposition", { text: "\u4f60", selectionStart: 1, selectionEnd: 1 });
    await cdp.send("Input.insertText", { text: "\u4f60" });
    await expect.poll(() => sentInput.slice(composedAt).join("")).toBe("\u4f60");
    await page.keyboard.press("Control+u");
    await cdp.detach();

    await page.keyboard.type("printf 'EDIT:%s\\n' 'ac'");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.type("bx");
    await page.keyboard.press("Backspace");
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await expect(page.locator(".xterm-rows")).toContainText("EDIT:abc");

    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.evaluate(() => navigator.clipboard.writeText("printf 'PASTE:%s\\n' '\u7c98\u8d34'"));
    await page.getByRole("button", { name: "粘贴", exact: true }).click();
    await expect(page.locator(".xterm-helper-textarea")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator(".xterm-rows")).toContainText("PASTE:\u7c98\u8d34");
    await page.getByRole("button", { name: "清除当前显示", exact: true }).click();
    await expect(page.locator(".xterm-helper-textarea")).toBeFocused();
    await page.keyboard.type("printf 'FOCUS:%s\\n' 'ok'");
    await page.keyboard.press("Enter");
    await expect(page.locator(".xterm-rows")).toContainText("FOCUS:ok");
    await command("printf '\\033[?1003h\\033[?1006h'");
    const outputRow = page.locator(".xterm-rows > div").filter({ hasText: /^FOCUS:ok\s*$/ }).last();
    const selectOutput = async () => {
      const row = await outputRow.boundingBox();
      if (!row) throw new Error("Output row is not visible");
      await page.keyboard.down("Shift");
      await page.mouse.click(row.x + 6, row.y + 6, { clickCount: 3 });
      await page.keyboard.up("Shift");
    };
    await selectOutput();
    const copiedAt = sentInput.length;
    await page.keyboard.press("Control+c");
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/^FOCUS:ok\s*$/);
    expect(sentInput.slice(copiedAt).join("")).not.toContain("\x03");
    await page.evaluate(async () => {
      await navigator.clipboard.writeText("before-fallback");
      Object.defineProperty(navigator.clipboard, "writeText", { configurable: true, value: async () => { throw new DOMException("Clipboard API denied", "NotAllowedError"); } });
    });
    await selectOutput();
    await page.getByRole("button", { name: /^(复制选中文本|已复制)$/ }).click();
    await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toMatch(/^FOCUS:ok\s*$/);
    await expect(page.locator(".xterm-helper-textarea")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    const interruptedAt = sentInput.length;
    await page.keyboard.press("Control+c");
    await expect.poll(() => sentInput.slice(interruptedAt).join("")).toContain("\x03");
    const reader = page.getByRole("button", { name: "读屏模式", exact: true });
    await reader.click();
    await expect(page.locator(".xterm-accessibility-tree")).toContainText("FOCUS:ok");
    await page.reload();
    await page.getByText("已连接", { exact: true }).waitFor();
    await expect(reader).toHaveAttribute("aria-pressed", "true");
    await reader.click();
    await expect(page.locator(".xterm-accessibility-tree")).toHaveCount(0);
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Horizontal overflow at ${viewport.width}`);
    const box = await page.locator(".terminal-stage").boundingBox();
    if (!box || box.height < viewport.height * 0.50 || box.x < 0 || box.x + box.width > viewport.width) throw new Error("Terminal is too small or outside the viewport");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    await page.screenshot({ path: `${output}/terminal-${viewport.width}.png`, fullPage: true, animations: "disabled" });
    await page.getByRole("button", { name: "结束终端", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "结束终端", exact: true }).click();
    await page.getByText("终端已结束", { exact: true }).waitFor();
    await page.close();
  }
  if (errors.length) throw new Error(JSON.stringify(errors));
  console.log(JSON.stringify({ screenshots: output, viewports: [1440, 390], realPty: true, input: true, unicodeInput: true, imeComposition: true, editing: true, paste: true, copyShortcut: true, copyFallback: true, toolbarFocus: true, readerPreference: true, interrupt: true, reload: true, errors }));
} catch (error) {
  if (activePage && !activePage.isClosed()) {
    await activePage.screenshot({ path: `${output}/failure.png`, fullPage: true });
    console.error(JSON.stringify({ frames, errors, page: (await activePage.locator("body").innerText()).slice(-2000) }));
  }
  throw error;
} finally {
  if (browser) await browser.close();
  if (child.exitCode === null) { child.kill("SIGTERM"); await once(child, "exit"); }
}
