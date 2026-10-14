import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button, Field, Input, Spinner, Tooltip } from "@fluentui/react-components";
import { Add20Regular, Dismiss20Regular, ArrowExit20Regular, Person20Regular, Copy20Regular, ArrowSync20Regular } from "@fluentui/react-icons";
import { copyText } from "./clipboard";

declare global { interface Window { __ropsTeamMode?: boolean } }
type Account = { username: string; display_name: string; role: "admin" | "member"; enabled: boolean; activated: boolean; setup?: { status: string; error?: string | null } | null };
type TeamSession = { enabled: boolean; user?: Account | null; can_create_accounts?: boolean };
type Invitation = { url: string; expires_in: number };
const USERNAME = /^[a-z][a-z0-9_]{2,26}$/;
const Context = createContext<TeamSession>({ enabled: false });

export async function teamRequest<T>(path: string, method = "GET", body?: object): Promise<T> {
  const response = await fetch(`/api/team/${path}`, {
    method, credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "提交的信息不符合要求，请检查后重试。");
  return result as T;
}

export function TeamBoundary({ children }: { children: ReactNode }) {
  const cache = useQueryClient();
  const [session, setSession] = useState<TeamSession>();
  const account = useRef<string | null>(null);
  const [error, setError] = useState("");
  const [invite, setInvite] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get("invite") || "");
  const refresh = () => teamRequest<TeamSession>("session").then((value) => {
    const next = value.user?.username ?? null;
    if (account.current !== next) cache.clear();
    account.current = next;
    window.__ropsTeamMode = value.enabled;
    setError(""); setSession(value);
  }).catch((reason: Error) => setError(reason.message));
  useEffect(() => {
    void refresh();
    if (invite) window.history.replaceState({}, "", window.location.pathname + window.location.search);
    const expired = () => { cache.clear(); setSession({ enabled: true, user: null }); };
    window.addEventListener("rops-auth-expired", expired);
    const focused = () => { void refresh(); };
    window.addEventListener("focus", focused);
    return () => { window.removeEventListener("rops-auth-expired", expired); window.removeEventListener("focus", focused); };
  }, []); // Login bootstrap is intentionally independent of the worker's query cache.
  if (!session) return <main className="team-login"><h1>Orbitree</h1>{error
    ? <><p role="alert">{error}</p><Button onClick={() => void refresh()}>重试</Button></>
    : <Spinner label="正在连接账号服务" />}</main>;
  if (session.enabled && (!session.user || invite)) return <LoginForm invite={invite} onDone={() => {
    setInvite(""); cache.clear(); void refresh();
  }} onCancel={() => setInvite("")} />;
  return <Context.Provider key={session.user?.username ?? "standalone"} value={session}>{children}</Context.Provider>;
}

function LoginForm({ invite, onDone, onCancel }: { invite: string; onDone: () => void; onCancel: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <main className="team-login"><form onSubmit={async (event) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await teamRequest(invite ? "activate" : "login", "POST", invite ? { token: invite, password } : { username, password });
      sessionStorage.removeItem("rops_access_token");
      setPassword(""); onDone();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  }}>
    <h1>Orbitree</h1><h2>{invite ? "激活账号" : "登录"}</h2>
    {!invite && <Field label="账号" required><Input autoComplete="username" value={username} onChange={(_, data) => setUsername(data.value)} /></Field>}
    <Field label={invite ? "设置密码（至少 12 位）" : "密码"} required><Input type="password" autoComplete={invite ? "new-password" : "current-password"} value={password} onChange={(_, data) => setPassword(data.value)} /></Field>
    {invite && <Field label="确认密码" required><Input type="password" autoComplete="new-password" value={confirmation} onChange={(_, data) => setConfirmation(data.value)} /></Field>}
    {error && <p role="alert">{error}</p>}
    <Button appearance="primary" type="submit" disabled={busy || !password || (!invite && !username) || Boolean(invite && (password.length < 12 || password !== confirmation))}>{busy ? "正在处理" : invite ? "激活并登录" : "登录"}</Button>
    {invite && <Button appearance="subtle" onClick={onCancel}>返回登录</Button>}
  </form></main>;
}

export function TeamAccountSettings() {
  const session = useContext(Context);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [invite, setInvite] = useState<(Invitation & { username: string }) | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [pendingAccount, setPendingAccount] = useState<string | null>(null);
  const [current, setCurrent] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    try { setAccounts(await teamRequest<Account[]>("accounts")); setListError(""); }
    catch (error) { setListError((error as Error).message); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (session.user?.role === "admin") void refresh(); }, [session.user?.role]);
  const preparing = accounts.some((account) => account.setup && ["pending", "provisioning", "starting"].includes(account.setup.status));
  useEffect(() => {
    if (!preparing || session.user?.role !== "admin") return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [preparing, session.user?.role]);
  const inviteAccount = async (account: Account, retry = false) => {
    setPendingAccount(account.username); setError(""); setMessage(""); setInvite(null);
    try {
      const result = await teamRequest<Invitation>(`accounts/${account.username}/${retry ? "setup" : "invite"}`, "POST", {});
      setInvite({ ...result, username: account.username });
      setMessage(retry ? "用户实例已就绪，邀请链接已生成。" : "邀请链接已生成，24 小时内有效。");
      if (retry && username.trim() === account.username) { setCreating(false); setUsername(""); setDisplayName(""); }
    } catch (error) { setError((error as Error).message); }
    finally { setPendingAccount(null); await refresh(); }
  };
  if (!session.enabled || !session.user) return null;
  return <section className="settings-section team-account">
    <header><h2><Person20Regular /> {session.user.display_name} <small>@{session.user.username}</small></h2>
      <Button icon={<ArrowExit20Regular />} onClick={async () => {
        try { await teamRequest("logout", "POST", {}); window.location.reload(); }
        catch (error) { setMessage((error as Error).message); }
      }}>退出登录</Button></header>
    <form className="settings-fields" onSubmit={async (event) => {
      event.preventDefault(); setBusy(true);
      try { await teamRequest("password", "POST", { current_password: current, new_password: password }); window.location.reload(); }
      catch (error) { setMessage((error as Error).message); }
      finally { setBusy(false); }
    }}>
      <Field label="当前密码"><Input type="password" autoComplete="current-password" value={current} onChange={(_, data) => setCurrent(data.value)} /></Field>
      <Field label="新密码（至少 12 位）"><Input type="password" autoComplete="new-password" value={password} onChange={(_, data) => setPassword(data.value)} /></Field>
      <Button type="submit" disabled={busy || !current || password.length < 12}>修改密码并退出</Button>
    </form>
    {session.user.role === "admin" && <>
      <header><h3>团队账号</h3><div className="team-account-actions">
        <Button icon={<Add20Regular />} disabled={!session.can_create_accounts || Boolean(pendingAccount)} onClick={() => {
          setCreating(true); setUsername(""); setDisplayName(""); setError(""); setMessage("");
        }}>新增用户</Button>
        <Tooltip content="刷新团队账号" relationship="label"><Button icon={<ArrowSync20Regular />} onClick={() => void refresh()} aria-label="刷新团队账号" /></Tooltip>
      </div></header>
      {!session.can_create_accounts && <p role="status">尚未配置用户实例模板，请联系管理员配置账号网关的 --release。</p>}
      {creating && <form className="team-account-create" aria-label="新增用户" aria-busy={Boolean(pendingAccount)} onSubmit={async (event) => {
        event.preventDefault();
        if (pendingAccount || !USERNAME.test(username.trim()) || !displayName.trim()) return;
        const name = username.trim();
        setPendingAccount(name); setError(""); setMessage(""); setInvite(null);
        try {
          const result = await teamRequest<Invitation & { account: Account }>("accounts", "POST", { username: name, display_name: displayName.trim() });
          setInvite({ url: result.url, expires_in: result.expires_in, username: result.account.username });
          setCreating(false); setUsername(""); setDisplayName("");
          setMessage(`账号 ${name} 已创建，独立工作区和用户实例已就绪。`);
        } catch (error) { setError((error as Error).message); }
        finally { setPendingAccount(null); await refresh(); }
      }}>
        <div className="settings-fields">
          <Field label="账号名" required hint="3–27 位小写字母、数字或下划线，以字母开头。" validationState={username && !USERNAME.test(username.trim()) ? "error" : "none"}>
            <Input autoFocus autoComplete="off" value={username} maxLength={27} disabled={Boolean(pendingAccount)} onChange={(_, data) => setUsername(data.value)} />
          </Field>
          <Field label="显示名称" required><Input value={displayName} maxLength={160} disabled={Boolean(pendingAccount)} onChange={(_, data) => setDisplayName(data.value)} /></Field>
        </div>
        <p className="team-create-note">将创建独立的普通用户、工作区和实例，不复制现有账号的会话或模型登录信息。</p>
        <div className="team-account-actions">
          <Button appearance="primary" type="submit" icon={<Add20Regular />} disabled={Boolean(pendingAccount) || !USERNAME.test(username.trim()) || !displayName.trim()}>创建并生成邀请</Button>
          <Button icon={<Dismiss20Regular />} disabled={Boolean(pendingAccount)} onClick={() => { setCreating(false); setUsername(""); setDisplayName(""); setError(""); }}>取消</Button>
          {pendingAccount && <Spinner size="tiny" label="正在初始化独立工作区和用户实例" />}
        </div>
      </form>}
      {loading && <Spinner size="tiny" label="正在加载团队账号" />}
      {listError && <p role="alert">团队账号加载失败：{listError}</p>}
      {!loading && !listError && !accounts.length && <p>尚未创建团队账号。</p>}
      <div className="team-accounts">{accounts.map((account) => <div className="team-account-row" key={account.username}>
        <div><strong>{account.display_name}</strong><small>@{account.username} · {account.activated ? "已激活" : "待激活"} · {account.enabled ? "已启用" : "已停用"}
          {account.setup && ` · ${{ pending: "等待初始化", provisioning: "初始化中", starting: "实例启动中", ready: "实例已就绪", error: "初始化失败" }[account.setup.status] || account.setup.status}`}
        </small>{account.setup?.error && <span className="team-setup-error">{account.setup.error}</span>}</div>
        {!account.activated && account.enabled && (account.setup && account.setup.status !== "ready"
          ? <Button icon={<ArrowSync20Regular />} disabled={Boolean(pendingAccount) || !session.can_create_accounts} onClick={() => void inviteAccount(account, true)}>重试初始化</Button>
          : <Button disabled={Boolean(pendingAccount)} onClick={() => void inviteAccount(account)}>生成邀请</Button>)}
        {pendingAccount === account.username && !creating && <Spinner size="tiny" label="正在处理账号" />}
        {account.role !== "admin" && <Button disabled={Boolean(pendingAccount)} onClick={async () => {
          setPendingAccount(account.username); setError(""); setInvite(null);
          try { await teamRequest(`accounts/${account.username}`, "PUT", { enabled: !account.enabled }); await refresh(); }
          catch (error) { setError((error as Error).message); }
          finally { setPendingAccount(null); }
        }}>{account.enabled ? "停用访问" : "启用访问"}</Button>}
      </div>)}</div>
      {invite && <div className="team-invite"><div><strong>{invite.username} 的邀请链接</strong><code>{invite.url}</code><small>24 小时内有效，仅发送给对应同事。</small></div><Button icon={<Copy20Regular />} onClick={async () => {
        try { await copyText(invite.url); setMessage("邀请链接已复制，24 小时内有效"); }
        catch { setMessage("无法使用剪贴板，请手动选择链接"); }
      }}>复制邀请</Button></div>}
    </>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
