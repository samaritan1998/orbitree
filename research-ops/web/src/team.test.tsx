import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { type ReactNode } from "react";
import { TeamAccountSettings, TeamBoundary } from "./team";
import { copyText } from "./clipboard";

vi.mock("./clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });
function renderBoundary(children: ReactNode = <div>Private workspace</div>) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><TeamBoundary>{children}</TeamBoundary></QueryClientProvider>);
  return cache;
}
test("single-user deployment remains available", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: false }))));
  renderBoundary();
  expect(await screen.findByText("Private workspace")).toBeInTheDocument();
});
test("unauthenticated users cannot mount workspace queries", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: true, user: null }))));
  renderBoundary();
  expect(await screen.findByRole("heading", { name: "登录" })).toBeInTheDocument();
  expect(screen.queryByText("Private workspace")).not.toBeInTheDocument();
});
test("account bootstrap errors fail closed", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Network unavailable")));
  renderBoundary();
  expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
  expect(screen.queryByText("Private workspace")).not.toBeInTheDocument();
});
test("invitation is removed from URL and requires matching passwords", async () => {
  window.history.replaceState({}, "", "/#invite=private-invitation");
  const fetcher = vi.fn().mockImplementation(async (path: string) => new Response(JSON.stringify(path.endsWith("session") ? { enabled: true, user: null } : { ok: true })));
  vi.stubGlobal("fetch", fetcher);
  renderBoundary();
  expect(await screen.findByRole("heading", { name: "激活账号" })).toBeInTheDocument();
  expect(window.location.hash).toBe("");
  fireEvent.change(screen.getByLabelText(/设置密码/), { target: { value: "long password 1234" } });
  expect(screen.getByRole("button", { name: "激活并登录" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/确认密码/), { target: { value: "long password 1234" } });
  fireEvent.click(screen.getByRole("button", { name: "激活并登录" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/team/activate", expect.objectContaining({
    method: "POST", body: JSON.stringify({ token: "private-invitation", password: "long password 1234" }),
  })));
});
test("expired session clears the previous account query cache", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: true, user: { username: "alice" } }))));
  const cache = renderBoundary();
  await screen.findByText("Private workspace");
  cache.setQueryData(["private-results"], "alice data");
  window.dispatchEvent(new Event("rops-auth-expired"));
  await screen.findByRole("heading", { name: "登录" });
  expect(cache.getQueryData(["private-results"])).toBeUndefined();
});

const OWNER = { username: "owner", display_name: "Owner", role: "admin", enabled: true, activated: true };
const CAROL = { username: "carol", display_name: "Carol", role: "member", enabled: true, activated: false, setup: { status: "ready", error: null } };
const INVITE = { url: "https://research.example.test/#invite=private-carol", expires_in: 86400 };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

test("administrator creates user and copies its invitation", async () => {
  let accounts = [OWNER];
  const fetcher = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith("/session")) return json({ enabled: true, user: OWNER, can_create_accounts: true });
    if (path.endsWith("/accounts") && options?.method === "POST") {
      accounts = [OWNER, CAROL];
      return json({ account: CAROL, ...INVITE }, 201);
    }
    return json(accounts);
  });
  vi.stubGlobal("fetch", fetcher);
  renderBoundary(<TeamAccountSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "新增用户" }));
  const submit = screen.getByRole("button", { name: "创建并生成邀请" });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/账号名/), { target: { value: "../root" } });
  fireEvent.change(screen.getByLabelText(/显示名称/), { target: { value: "Carol" } });
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/账号名/), { target: { value: "carol" } });
  fireEvent.click(submit);
  expect(await screen.findByText("carol 的邀请链接")).toBeInTheDocument();
  expect(screen.queryByRole("form", { name: "新增用户" })).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledWith("/api/team/accounts", expect.objectContaining({ method: "POST", body: JSON.stringify({ username: "carol", display_name: "Carol" }) }));
  fireEvent.click(screen.getByRole("button", { name: "复制邀请" }));
  await waitFor(() => expect(copyText).toHaveBeenCalledWith(INVITE.url));
  expect(await screen.findByText("邀请链接已复制，24 小时内有效")).toBeInTheDocument();
});

test("cancel discards account draft without creating anything", async () => {
  const fetcher = vi.fn(async (path: string, _options?: RequestInit) => json(path.endsWith("/session") ? { enabled: true, user: OWNER, can_create_accounts: true } : [OWNER]));
  vi.stubGlobal("fetch", fetcher);
  renderBoundary(<TeamAccountSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "新增用户" }));
  fireEvent.change(screen.getByLabelText(/账号名/), { target: { value: "carol" } });
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  fireEvent.click(screen.getByRole("button", { name: "新增用户" }));
  expect(screen.getByLabelText(/账号名/)).toHaveValue("");
  expect(fetcher.mock.calls.every(([, options]) => !options || options.method !== "POST")).toBe(true);
});

test("failed setup remains visible and can be retried", async () => {
  let failed = false;
  let fixed = false;
  vi.stubGlobal("fetch", vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith("/session")) return json({ enabled: true, user: OWNER, can_create_accounts: true });
    if (path.endsWith("/accounts") && options?.method === "POST") {
      failed = true;
      return json({ detail: "用户实例启动失败，已有文件已保留。" }, 503);
    }
    if (path.endsWith("/setup")) { fixed = true; return json({ account: CAROL, ...INVITE }); }
    return json(failed ? [OWNER, { ...CAROL, setup: fixed ? CAROL.setup : { status: "error", error: "需要重试初始化" } }] : [OWNER]);
  }));
  renderBoundary(<TeamAccountSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "新增用户" }));
  fireEvent.change(screen.getByLabelText(/账号名/), { target: { value: "carol" } });
  fireEvent.change(screen.getByLabelText(/显示名称/), { target: { value: "Carol" } });
  fireEvent.click(screen.getByRole("button", { name: "创建并生成邀请" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("已有文件已保留");
  expect(screen.queryByRole("button", { name: "生成邀请" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "重试初始化" }));
  expect(await screen.findByText("carol 的邀请链接")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "生成邀请" })).toBeInTheDocument();
});

test("creation disables duplicate submissions while pending", async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const fetcher = vi.fn((path: string, options?: RequestInit) => {
    if (path.endsWith("/session")) return Promise.resolve(json({ enabled: true, user: OWNER, can_create_accounts: true }));
    if (options?.method === "POST") return pending;
    return Promise.resolve(json([OWNER]));
  });
  vi.stubGlobal("fetch", fetcher);
  renderBoundary(<TeamAccountSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "新增用户" }));
  fireEvent.change(screen.getByLabelText(/账号名/), { target: { value: "carol" } });
  fireEvent.change(screen.getByLabelText(/显示名称/), { target: { value: "Carol" } });
  fireEvent.click(screen.getByRole("button", { name: "创建并生成邀请" }));
  expect(screen.getByRole("button", { name: "创建并生成邀请" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
  finish(json({ account: CAROL, ...INVITE }, 201));
  await screen.findByText("carol 的邀请链接");
  expect(fetcher.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
});

test("missing template explains disabled create button", async () => {
  vi.stubGlobal("fetch", vi.fn(async (path: string) => json(path.endsWith("/session") ? { enabled: true, user: OWNER, can_create_accounts: false } : [OWNER])));
  renderBoundary(<TeamAccountSettings />);
  expect(await screen.findByRole("button", { name: "新增用户" })).toBeDisabled();
  expect(screen.getByText(/尚未配置用户实例模板/)).toBeInTheDocument();
});

test("ordinary members have no account creation controls", async () => {
  const fetcher = vi.fn(async () => json({ enabled: true, user: { ...OWNER, role: "member" } }));
  vi.stubGlobal("fetch", fetcher);
  renderBoundary(<TeamAccountSettings />);
  await screen.findByRole("button", { name: "退出登录" });
  expect(screen.queryByRole("button", { name: "新增用户" })).not.toBeInTheDocument();
  expect(fetcher).not.toHaveBeenCalledWith("/api/team/accounts", expect.anything());
});
