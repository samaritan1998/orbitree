import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import AppShell from "./AppShell";
import type { Page } from "../router";
import type { Program } from "../types";

const programs: Program[] = [
  { id: "p-harn", code: "HARN", slug: "harn", name: "Harness / Office", objective: "", color: "#39744e", sort_order: 1, active: true },
  { id: "p-free", code: "FREE", slug: "free", name: "Free Work", objective: "", color: "#987633", sort_order: 5, active: true },
];
beforeEach(() => window.localStorage.clear());
afterEach(cleanup);

function shell(page: Page = { type: "today" }, onNavigate = vi.fn()) {
  return <AppShell page={page} programs={programs} onNavigate={onNavigate} onNewWork={vi.fn()}><h1>Workspace</h1></AppShell>;
}

test("keeps low-frequency entries out of the primary navigation", () => {
  render(shell());
  const nav = within(screen.getByRole("navigation", { name: "Primary navigation" }));
  for (const name of ["今日", "会话", "工作方向", "任务与资源", "每日复盘", "终端"]) {
    expect(nav.getByRole("button", { name })).toBeInTheDocument();
  }
  for (const name of ["评测", "合并队列", "技能", "仓库", "记忆账本", "技能审核", "设置"]) {
    expect(nav.queryByRole("button", { name })).not.toBeInTheDocument();
  }
});

test("terminal has a distinct selected entry and is reachable on mobile", async () => {
  const navigate = vi.fn();
  render(shell({ type: "terminal" }, navigate));
  const nav = within(screen.getByRole("navigation", { name: "Primary navigation" }));
  expect(nav.getByRole("button", { name: "终端" })).toHaveAttribute("aria-current", "page");
  const mobile = within(screen.getByRole("navigation", { name: "Mobile navigation" }));
  fireEvent.click(mobile.getByRole("button", { name: "更多" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "终端" }));
  expect(navigate).toHaveBeenCalledWith({ type: "terminal" });
});

test("persists program collapse without removing programs", () => {
  const { unmount } = render(shell());
  const nav = within(screen.getByRole("navigation", { name: "Primary navigation" }));
  fireEvent.click(nav.getByRole("button", { name: "工作方向" }));
  expect(nav.getByRole("button", { name: "工作方向" })).toHaveAttribute("aria-expanded", "false");
  expect(nav.queryByRole("button", { name: "Free Work" })).not.toBeInTheDocument();
  expect(window.localStorage.getItem("research-ops:programs")).toBe("collapsed");
  unmount();
  render(shell());
  const reloaded = within(screen.getByRole("navigation", { name: "Primary navigation" }));
  expect(reloaded.getByRole("button", { name: "工作方向" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(reloaded.getByRole("button", { name: "工作方向" }));
  expect(reloaded.getByRole("button", { name: "Free Work" })).toBeInTheDocument();
});

test("exposes every low-frequency route in More", async () => {
  const navigate = vi.fn();
  render(shell({ type: "memory", view: "ledger" }, navigate));
  const more = screen.getAllByRole("button", { name: "更多" })[0];
  expect(more).toHaveAttribute("aria-current", "page");
  expect(screen.getAllByRole("button", { name: "每日复盘" })[0]).not.toHaveAttribute("aria-current");
  fireEvent.click(more);
  const menu = within(await screen.findByRole("menu", { name: "更多" }));
  for (const name of ["技能", "评测", "合并队列", "仓库", "记忆账本", "技能审核"]) {
    expect(menu.getByRole("menuitem", { name })).toBeInTheDocument();
  }
  fireEvent.click(menu.getByRole("menuitem", { name: "技能审核" }));
  expect(navigate).toHaveBeenCalledWith({ type: "skills", view: "proposals" });
});

test("makes all directions reachable from mobile and the collapsed rail", async () => {
  window.localStorage.setItem("research-ops:sidebar", "collapsed");
  const navigate = vi.fn();
  render(shell({ type: "program", code: "HARN" }, navigate));
  for (const label of ["Primary navigation", "Mobile navigation"]) {
    const nav = within(screen.getByRole("navigation", { name: label }));
    fireEvent.click(nav.getByRole("button", { name: "工作方向" }));
    const menu = within(await screen.findByRole("menu", { name: "工作方向" }));
    expect(menu.getByRole("menuitem", { name: /Harness/ })).toHaveAttribute("aria-current", "page");
    fireEvent.click(menu.getByRole("menuitem", { name: /Free Work/ }));
    expect(navigate).toHaveBeenLastCalledWith({ type: "program", code: "FREE" });
  }
  const mobile = within(screen.getByRole("navigation", { name: "Mobile navigation" }));
  expect(mobile.getAllByRole("button")).toHaveLength(6);
  fireEvent.click(mobile.getByRole("button", { name: "更多" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "设置" }));
  expect(navigate).toHaveBeenLastCalledWith({ type: "settings" });
});

test.each(["clusters", "ltp-monitor"] as const)("marks resources current at %s", (type) => {
  render(shell({ type }));
  expect(screen.getAllByRole("button", { name: "任务与资源" })[0]).toHaveAttribute("aria-current", "page");
});
