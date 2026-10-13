import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../api";
import { copyText } from "../clipboard";
import TerminalView from "./TerminalView";

const terminal = vi.hoisted(() => ({ options: { screenReaderMode: false }, write: vi.fn(), reset: vi.fn(), clear: vi.fn(), focus: vi.fn(), dispose: vi.fn(), getSelection: vi.fn(() => "selected"), hasSelection: vi.fn(() => true), attachCustomKeyEventHandler: vi.fn((_callback: (event: KeyboardEvent) => boolean) => {}), paste: vi.fn(), cols: 100, rows: 30, onData: vi.fn((_callback: (data: string) => void) => ({ dispose: vi.fn() })), loadAddon: vi.fn(), open: vi.fn() }));
vi.mock("@xterm/xterm", () => ({ Terminal: class { constructor() { return terminal; } } }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("../clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  onopen?: () => void;
  onclose?: () => void;
  onerror?: () => void;
  onmessage?: (message: { data: string }) => void;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(); });
  constructor(public url: string) { FakeSocket.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  message(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
const locked = { enabled: true, available: true, requires_unlock: true, unlock_command: "/server/.venv/bin/rops terminal key" };
const ready = { ...locked, requires_unlock: false, active: true, user: "alice", hostname: "worker", cwd: "/work", shell: "/bin/bash" };

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  terminal.getSelection.mockReturnValue("selected");
  terminal.hasSelection.mockReturnValue(true);
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(api, "terminalStatus").mockResolvedValue(locked);
  vi.spyOn(api, "connectTerminal").mockResolvedValue({ ticket: "one-use-ticket" });
  vi.spyOn(api, "stopTerminal").mockResolvedValue({ ...ready, active: false });
  vi.spyOn(api, "interruptTerminal").mockResolvedValue({ accepted: true });
  terminal.write.mockImplementation((_: string, callback?: () => void) => callback?.());
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function connected() {
  vi.mocked(api.terminalStatus).mockResolvedValue(ready);
  render(<TerminalView />);
  await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
  const ws = FakeSocket.instances[0];
  act(() => { ws.open(); ws.message({ type: "ready", history: "shell history", terminal: ready }); });
  await screen.findByText("已连接");
  return ws;
}

test("locked terminal starts no shell or model and requires an explicit unlock", async () => {
  render(<TerminalView />);
  await screen.findByLabelText("终端口令");
  expect(api.connectTerminal).not.toHaveBeenCalled();
  expect(FakeSocket.instances).toHaveLength(0);
  expect(screen.getByRole("button", { name: "解锁并连接" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("终端口令"), { target: { value: "private-key" } });
  vi.mocked(api.terminalStatus).mockResolvedValue(ready);
  fireEvent.click(screen.getByRole("button", { name: "解锁并连接" }));
  await waitFor(() => expect(api.connectTerminal).toHaveBeenCalledWith("private-key"));
  expect(sessionStorage.getItem("research-ops:terminal-key")).toBe("private-key");
  const ws = FakeSocket.instances[0];
  expect(ws.url).not.toContain("ticket");
  expect(ws.url).not.toContain("private-key");
  act(() => ws.open());
  expect(JSON.parse(ws.send.mock.calls[0][0])).toEqual({ ticket: "one-use-ticket", cols: 100, rows: 30 });
});

test("displays streamed output and offers foreground interruption", async () => {
  const ws = await connected();
  terminal.write.mockImplementationOnce(() => {});
  act(() => ws.message({ type: "output", data: "actual process output", seq: 7 }));
  expect(terminal.write).toHaveBeenCalledWith("actual process output", expect.any(Function));
  const write = [...terminal.write.mock.calls].reverse().find((call) => call[0] === "actual process output");
  expect(ws.send).not.toHaveBeenCalledWith(JSON.stringify({ type: "ack", seq: 7 }));
  act(() => write?.[1]());
  expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: "ack", seq: 7 }));
  fireEvent.click(screen.getByRole("button", { name: "中断前台命令（Ctrl+C）" }));
  await waitFor(() => expect(api.interruptTerminal).toHaveBeenCalled());
  expect(api.stopTerminal).not.toHaveBeenCalled();
});

test("history replay cannot send terminal responses into the live shell", async () => {
  vi.mocked(api.terminalStatus).mockResolvedValue(ready);
  render(<TerminalView />);
  await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
  const ws = FakeSocket.instances[0];
  const onData = terminal.onData.mock.calls.at(-1)?.[0] as unknown as (value: string) => void;
  let parsed: (() => void) | undefined;
  terminal.write.mockImplementationOnce((_: string, callback?: () => void) => {
    onData("capability-response");
    parsed = callback;
  });
  act(() => { ws.open(); ws.message({ type: "ready", history: "replay", terminal: ready }); });
  expect(ws.send).not.toHaveBeenCalledWith(JSON.stringify({ type: "input", data: "capability-response" }));
  act(() => parsed?.());
  await screen.findByText("已连接");
  act(() => onData("user input"));
  expect(ws.send).toHaveBeenCalledWith(JSON.stringify({ type: "input", data: "user input" }));
});

test("screen-reader mode is opt-in, persisted and does not reconnect the terminal", async () => {
  await connected();
  const toggle = screen.getByRole("button", { name: "读屏模式" });
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  expect(terminal.options.screenReaderMode).toBe(false);
  fireEvent.click(toggle);
  expect(terminal.options.screenReaderMode).toBe(true);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  expect(localStorage.getItem("research-ops:terminal-screen-reader")).toBe("true");
  expect(FakeSocket.instances).toHaveLength(1);
  cleanup();
  render(<TerminalView />);
  expect(screen.getByRole("button", { name: "读屏模式" })).toHaveAttribute("aria-pressed", "true");
  expect(terminal.options.screenReaderMode).toBe(true);
});

test("unicode and editing keys are sent once and input stops after disconnection", async () => {
  const ws = await connected();
  const onData = terminal.onData.mock.calls.at(-1)![0];
  ws.send.mockClear();
  const text = "\u4e2d\u6587\u8f93\u5165\ud83d\ude80\x7f\x1b[D\r";
  act(() => onData(text));
  expect(ws.send).toHaveBeenCalledTimes(1);
  expect(JSON.parse(ws.send.mock.calls[0][0])).toEqual({ type: "input", data: text });
  act(() => { ws.close(); onData("not sent"); });
  expect(ws.send).toHaveBeenCalledTimes(1);
});

test("toolbar actions restore terminal focus without stealing a new focus target", async () => {
  await connected();
  await waitFor(() => expect(terminal.focus).toHaveBeenCalled());
  terminal.focus.mockClear();
  const clear = screen.getByRole("button", { name: "清除当前显示" });
  clear.focus();
  fireEvent.click(clear);
  await waitFor(() => expect(terminal.focus).toHaveBeenCalledTimes(1));
  expect(terminal.clear).toHaveBeenCalled();

  terminal.focus.mockClear();
  let finish: ((value: { accepted: boolean }) => void) | undefined;
  vi.mocked(api.interruptTerminal).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const interrupt = screen.getByRole("button", { name: "中断前台命令（Ctrl+C）" });
  interrupt.focus();
  fireEvent.click(interrupt);
  clear.focus();
  await act(async () => finish?.({ accepted: true }));
  expect(terminal.focus).not.toHaveBeenCalled();
});

test("copy uses the shared clipboard fallback and reports success or an empty selection", async () => {
  await connected();
  fireEvent.click(screen.getByRole("button", { name: "复制选中文本" }));
  await waitFor(() => expect(copyText).toHaveBeenCalledWith("selected"));
  const copied = await screen.findByRole("button", { name: "已复制" });
  terminal.getSelection.mockReturnValue("");
  fireEvent.click(copied);
  expect(await screen.findByRole("alert")).toHaveTextContent("请先选中要复制的终端文本。");
  expect(copyText).toHaveBeenCalledTimes(1);
});

test("copy shortcuts never interrupt a selected terminal, but plain Ctrl+C still can", async () => {
  await connected();
  const handle = terminal.attachCustomKeyEventHandler.mock.calls.at(-1)![0];
  const selectedCopy = new KeyboardEvent("keydown", { key: "c", ctrlKey: true, cancelable: true });
  await act(async () => { expect(handle(selectedCopy)).toBe(false); });
  expect(selectedCopy.defaultPrevented).toBe(true);
  expect(copyText).toHaveBeenCalledTimes(1);
  expect(handle(new KeyboardEvent("keyup", { key: "c", ctrlKey: true }))).toBe(true);
  expect(handle(new KeyboardEvent("keydown", { key: "c", ctrlKey: true, isComposing: true }))).toBe(true);
  terminal.hasSelection.mockReturnValue(false);
  expect(handle(new KeyboardEvent("keydown", { key: "c", ctrlKey: true }))).toBe(true);
  await act(async () => { expect(handle(new KeyboardEvent("keydown", { key: "C", ctrlKey: true, shiftKey: true }))).toBe(false); });
  await act(async () => { expect(handle(new KeyboardEvent("keydown", { key: "c", metaKey: true }))).toBe(false); });
  expect(copyText).toHaveBeenCalledTimes(3);
});

test("clipboard failure is visible and does not end the terminal", async () => {
  await connected();
  vi.mocked(copyText).mockRejectedValueOnce(new Error("denied"));
  fireEvent.click(screen.getByRole("button", { name: "复制选中文本" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("浏览器阻止了复制");
  expect(api.stopTerminal).not.toHaveBeenCalled();
  expect(api.interruptTerminal).not.toHaveBeenCalled();
});

test("taking control elsewhere does not trigger a reconnect fight", async () => {
  const ws = await connected();
  act(() => { ws.message({ type: "replaced" }); ws.close(); });
  expect(await screen.findByText("已在另一个页面连接")).toBeInTheDocument();
  expect(api.connectTerminal).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "在此页面接管" }));
  await waitFor(() => expect(api.connectTerminal).toHaveBeenCalledTimes(2));
});

test("ending the shell requires confirmation and closing the page only detaches", async () => {
  const ws = await connected();
  fireEvent.click(screen.getByRole("button", { name: "结束终端" }));
  const dialog = within(await screen.findByRole("dialog"));
  expect(api.stopTerminal).not.toHaveBeenCalled();
  fireEvent.click(dialog.getByRole("button", { name: "结束终端" }));
  await waitFor(() => expect(api.stopTerminal).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("终端已结束")).toBeInTheDocument();
  cleanup();
  expect(ws.close).toHaveBeenCalled();
  expect(api.stopTerminal).toHaveBeenCalledTimes(1);
});
