import { afterEach, expect, test, vi } from "vitest";
import { copyText } from "./clipboard";

afterEach(() => vi.restoreAllMocks());

test("falls back to document copy when the Clipboard API is unavailable", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  const execCommand = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });

  await copyText("codex -C /tmp/work");

  expect(execCommand).toHaveBeenCalledWith("copy");
  expect(document.querySelector("textarea")).toBeNull();
});

test("denied Clipboard API falls back to document copy and restores focus", async () => {
  const writeText = vi.fn().mockRejectedValue(new Error("permission denied"));
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const execCommand = vi.fn(() => true);
  Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
  const button = document.createElement("button");
  document.body.appendChild(button);
  button.focus();
  try {
    await copyText("selected terminal output");
    expect(writeText).toHaveBeenCalledWith("selected terminal output");
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(button);
    expect(document.querySelector("textarea")).toBeNull();
  } finally { button.remove(); }
});

test("failed fallback removes its temporary input and restores the original focus", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => { throw new Error("blocked"); }) });
  const input = document.createElement("input");
  document.body.appendChild(input);
  input.focus();
  try {
    await expect(copyText("text")).rejects.toThrow("blocked");
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.activeElement).toBe(input);
  } finally { input.remove(); }
});
