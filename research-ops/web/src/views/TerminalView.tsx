import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Spinner, ToggleButton, Tooltip } from "@fluentui/react-components";
import { Accessibility20Regular, ArrowSync20Regular, Checkmark20Regular, ClipboardPaste20Regular, Copy20Regular, Dismiss20Regular, Eraser20Regular, FullScreenMaximize20Regular, LockClosed20Regular, Play20Regular, Stop20Regular } from "@fluentui/react-icons";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api } from "../api";
import { copyText } from "../clipboard";
import type { WebTerminalStatus } from "../types";

const KEY = "research-ops:terminal-key";
const SCREEN_READER_KEY = "research-ops:terminal-screen-reader";
type Connection = "loading" | "locked" | "connecting" | "connected" | "disconnected" | "elsewhere" | "ended" | "disabled";
const LABELS: Record<Connection, string> = { loading: "正在加载", locked: "未解锁", connecting: "正在连接", connected: "已连接", disconnected: "连接已断开", elsewhere: "已在另一个页面连接", ended: "终端已结束", disabled: "终端未启用" };

function storedKey() {
  try { return sessionStorage.getItem(KEY) || ""; } catch { return ""; }
}

function storedScreenReaderMode() {
  try { return localStorage.getItem(SCREEN_READER_KEY) === "true"; } catch { return false; }
}

export default function TerminalView() {
  const host = useRef<HTMLDivElement>(null);
  const page = useRef<HTMLElement>(null);
  const term = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const writable = useRef(false);
  const generation = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const retries = useRef(0);
  const key = useRef(storedKey());
  const connectRef = useRef<() => void>(() => {});
  const [credential, setCredential] = useState("");
  const [connection, setConnection] = useState<Connection>("loading");
  const [info, setInfo] = useState<WebTerminalStatus>();
  const [error, setError] = useState("");
  const [ending, setEnding] = useState(false);
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [copied, setCopied] = useState(false);
  const [screenReaderMode, setScreenReaderMode] = useState(storedScreenReaderMode);

  const copySelection = useCallback(async () => {
    const text = term.current?.getSelection();
    if (!text) { setError("请先选中要复制的终端文本。"); return; }
    const activeElement = document.activeElement;
    try {
      setError("");
      await copyText(text);
      setCopied(true);
      clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("浏览器阻止了复制，请检查剪贴板权限后重试。");
    } finally {
      if (writable.current && document.activeElement === activeElement) term.current?.focus();
    }
  }, []);

  const send = useCallback((data: string) => {
    const ws = socket.current;
    if (writable.current && ws?.readyState === WebSocket.OPEN) {
      // Keep UTF-8 input frames bounded, including large clipboard pastes.
      let chunk = "";
      for (const character of data) {
        chunk += character;
        if (chunk.length >= 2048) { ws.send(JSON.stringify({ type: "input", data: chunk })); chunk = ""; }
      }
      if (chunk) ws.send(JSON.stringify({ type: "input", data: chunk }));
    }
  }, []);

  const resize = useCallback(() => {
    if (!term.current || !host.current?.clientWidth || !host.current.clientHeight) return;
    fit.current?.fit();
    if (writable.current && socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify({ type: "resize", cols: Math.max(20, Math.min(500, term.current.cols)), rows: Math.max(5, Math.min(200, term.current.rows)) }));
    }
  }, []);

  const connect = useCallback(async (candidate = key.current) => {
    const id = ++generation.current;
    writable.current = false;
    clearTimeout(reconnectTimer.current);
    socket.current?.close();
    socket.current = null;
    setError("");
    setConnection("connecting");
    try {
      const status = await api.terminalStatus(candidate);
      if (id !== generation.current) return;
      setInfo(status);
      if (!status.enabled) { setConnection("disabled"); return; }
      if (!status.available) { setConnection("disabled"); setError("服务器尚未安装 tmux。"); return; }
      if (status.requires_unlock) { setConnection("locked"); if (candidate) setError("口令无效，请重新输入。"); return; }
      key.current = candidate;
      try { if (candidate) sessionStorage.setItem(KEY, candidate); } catch { /* Keep this tab usable. */ }
      const { ticket } = await api.connectTerminal(candidate);
      if (id !== generation.current) return;
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/ws/terminal`);
      socket.current = ws;
      let manual = false;
      ws.onopen = () => {
        if (id !== generation.current) { ws.close(); return; }
        fit.current?.fit();
        ws.send(JSON.stringify({ ticket, cols: Math.max(20, Math.min(500, term.current?.cols || 100)), rows: Math.max(5, Math.min(200, term.current?.rows || 30)) }));
      };
      ws.onmessage = (event) => {
        if (id !== generation.current) return;
        const message = JSON.parse(event.data);
        if (message.type === "ready") {
          writable.current = false;
          term.current?.reset();
          setInfo(message.terminal);
          // Replayed terminal capability queries must not be answered into the
          // live shell. Only enable input after history has been parsed.
          term.current?.write(message.history || "", () => {
            if (id !== generation.current || ws.readyState !== WebSocket.OPEN) return;
            writable.current = true;
            setConnection("connected");
            retries.current = 0;
            requestAnimationFrame(() => { resize(); term.current?.focus(); });
          });
        } else if (message.type === "output") term.current?.write(message.data, () => {
          if (id === generation.current && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ack", seq: message.seq }));
        });
        else if (message.type === "replaced") { writable.current = false; manual = true; setConnection("elsewhere"); }
        else if (message.type === "exit") { writable.current = false; manual = true; setInfo((value) => value ? { ...value, active: false } : value); setConnection("ended"); }
        else if (message.type === "overflow") { writable.current = false; manual = true; setConnection("disconnected"); setError("输出过快，显示已暂停。可中断前台命令后重新连接。"); }
        else if (message.type === "error") { writable.current = false; manual = true; setConnection("disconnected"); setError(message.message); }
      };
      ws.onclose = () => {
        if (id !== generation.current || manual) return;
        writable.current = false;
        setConnection("disconnected");
        if (retries.current < 4) reconnectTimer.current = setTimeout(() => connectRef.current(), 1000 * 2 ** retries.current++);
      };
      ws.onerror = () => { if (id === generation.current) setError("终端连接中断，正在尝试重连。"); };
    } catch (problem) {
      if (id === generation.current) {
        setConnection("disconnected");
        setError(problem instanceof Error ? problem.message : "无法连接终端。");
      }
    }
  }, [resize]);
  connectRef.current = () => { void connect(); };

  useEffect(() => {
    if (!host.current) return;
    // Screen-reader mode bypasses input-only events from mobile and some IMEs.
    // Keep it opt-in without replacing xterm's native composition handling.
    const terminal = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: "'Cascadia Code', 'SFMono-Regular', Consolas, monospace", scrollback: 5000, screenReaderMode: storedScreenReaderMode(),
      theme: { background: "#17191c", foreground: "#e3e5e8", cursor: "#b0dfbc", selectionBackground: "#405548", black: "#202326", red: "#ed8585", green: "#a2d59e", yellow: "#e2ce84", blue: "#89b9de", magenta: "#c7a0cb", cyan: "#87c9c6", white: "#e3e5e8" } });
    const addon = new FitAddon();
    terminal.loadAddon(addon);
    terminal.open(host.current);
    term.current = terminal;
    fit.current = addon;
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown" || event.isComposing || event.altKey || event.key.toLowerCase() !== "c") return true;
      const copy = event.metaKey || (event.ctrlKey && (event.shiftKey || terminal.hasSelection()));
      if (!copy) return true;
      event.preventDefault();
      event.stopPropagation();
      void copySelection();
      return false;
    });
    const input = terminal.onData(send);
    const observer = new ResizeObserver(resize);
    observer.observe(host.current);
    const heartbeat = setInterval(() => {
      if (writable.current && socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify({ type: "ping" }));
    }, 20_000);
    void connect();
    return () => {
      generation.current += 1;
      writable.current = false;
      clearTimeout(reconnectTimer.current);
      clearTimeout(copiedTimer.current);
      clearInterval(heartbeat);
      socket.current?.close();
      socket.current = null;
      observer.disconnect();
      input.dispose();
      terminal.dispose();
      term.current = null;
      fit.current = null;
    };
  }, [connect, copySelection, resize, send]);

  useEffect(() => {
    if (term.current) term.current.options.screenReaderMode = screenReaderMode;
    try { localStorage.setItem(SCREEN_READER_KEY, String(screenReaderMode)); } catch { /* Session-only preference. */ }
  }, [screenReaderMode]);

  const action = async (callback: () => Promise<unknown>, restoreFocus = false) => {
    const activeElement = document.activeElement;
    try { setError(""); await callback(); } catch (problem) { setError(problem instanceof Error ? problem.message : "操作失败，请重试。"); }
    finally {
      if (restoreFocus && writable.current && document.activeElement === activeElement) term.current?.focus();
    }
  };
  const stop = async () => {
    setEnding(true);
    try {
      await api.stopTerminal(key.current);
      generation.current += 1;
      writable.current = false;
      clearTimeout(reconnectTimer.current);
      socket.current?.close();
      setConnection("ended");
      setInfo((value) => value ? { ...value, active: false } : value);
      setConfirmEnd(false);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "无法结束终端。"); }
    finally { setEnding(false); }
  };
  const unlocked = Boolean(info?.enabled && !info.requires_unlock);
  return <section className="terminal-page" ref={page}>
    <header className="terminal-header">
      <div className="terminal-heading"><h1>终端</h1><span className={`terminal-status terminal-status-${connection}`} role="status">{LABELS[connection]}</span></div>
      {info?.user && <code className="terminal-identity" title={`启动目录：${info.cwd}\n${info.shell}`}>{info.user}@{info.hostname}</code>}
      <div className="terminal-toolbar">
        <Tooltip content="重新连接" relationship="label"><Button size="small" appearance="subtle" icon={<ArrowSync20Regular />} disabled={!unlocked || connection === "connecting"} onClick={() => void connect()} /></Tooltip>
        <Tooltip content="中断前台命令（Ctrl+C）" relationship="label"><Button size="small" appearance="subtle" icon={<Stop20Regular />} disabled={!unlocked || !info?.active} onClick={() => void action(() => api.interruptTerminal(key.current), true)} /></Tooltip>
        <Tooltip content={copied ? "已复制" : "复制选中文本"} relationship="label"><Button size="small" appearance="subtle" icon={copied ? <Checkmark20Regular /> : <Copy20Regular />} onClick={() => void copySelection()} /></Tooltip>
        <Tooltip content="粘贴" relationship="label"><Button size="small" appearance="subtle" icon={<ClipboardPaste20Regular />} disabled={connection !== "connected"} onClick={() => void action(async () => term.current?.paste(await navigator.clipboard.readText()), true)} /></Tooltip>
        <Tooltip content="清除当前显示" relationship="label"><Button size="small" appearance="subtle" icon={<Eraser20Regular />} onClick={() => void action(async () => term.current?.clear(), true)} /></Tooltip>
        <Tooltip content="全屏" relationship="label"><Button size="small" appearance="subtle" icon={<FullScreenMaximize20Regular />} onClick={() => void action(async () => document.fullscreenElement ? document.exitFullscreen() : page.current?.requestFullscreen(), true)} /></Tooltip>
        <Tooltip content="读屏模式" relationship="label"><ToggleButton size="small" appearance="subtle" icon={<Accessibility20Regular />} checked={screenReaderMode} onClick={() => setScreenReaderMode((value) => !value)} /></Tooltip>
        <Tooltip content="结束终端" relationship="label"><Button size="small" appearance="subtle" icon={<Dismiss20Regular />} disabled={!unlocked || !info?.active} onClick={() => setConfirmEnd(true)} /></Tooltip>
      </div>
    </header>
    {error && <div className="terminal-error" role="alert">{error}</div>}
    <div className="terminal-stage">
      <div className="terminal-emulator" ref={host} aria-label="服务器终端" />
      {(connection === "locked" || connection === "disabled" || connection === "loading") && <div className="terminal-overlay">
        {connection === "locked" ? <form className="terminal-unlock" onSubmit={(event) => { event.preventDefault(); void connect(credential.trim()); }}>
          <LockClosed20Regular /><h2>解锁终端</h2>
          <Field label="终端口令"><Input type="password" value={credential} onChange={(_, value) => setCredential(value.value)} autoComplete="off" /></Field>
          <div className="terminal-key-command"><code>rops terminal key</code><Tooltip content="复制获取口令的命令" relationship="label"><Button appearance="subtle" icon={<Copy20Regular />} onClick={() => void action(() => copyText(info?.unlock_command || "rops terminal key"))} /></Tooltip></div>
          <Button type="submit" appearance="primary" icon={<Play20Regular />} disabled={!credential.trim()}>解锁并连接</Button>
        </form> : connection === "loading" ? <Spinner label="正在加载终端" /> : <p>终端未启用</p>}
      </div>}
      {["ended", "elsewhere", "disconnected"].includes(connection) && <div className="terminal-reconnect"><Button icon={<Play20Regular />} appearance="primary" disabled={!unlocked} onClick={() => void connect()}>{connection === "ended" ? "新建终端" : connection === "elsewhere" ? "在此页面接管" : "重新连接"}</Button></div>}
    </div>
    <footer className="terminal-footer"><div><Button size="small" disabled={connection !== "connected"} onClick={() => { send("\x1b"); term.current?.focus(); }}>Esc</Button><Button size="small" disabled={connection !== "connected"} onClick={() => { send("\t"); term.current?.focus(); }}>Tab</Button></div><span>{info?.user === "root" ? "root 权限" : info?.user || ""}</span><span>tmux</span></footer>
    <Dialog open={confirmEnd} onOpenChange={(_, data) => { if (!ending) setConfirmEnd(data.open); }}><DialogSurface><DialogBody><DialogTitle>结束这个终端？</DialogTitle><DialogContent>此终端的 Shell 和其中的前台程序将结束。其他 Research Ops 对话不受影响。</DialogContent><DialogActions><Button disabled={ending} onClick={() => setConfirmEnd(false)}>取消</Button><Button appearance="primary" disabled={ending} onClick={() => void stop()}>结束终端</Button></DialogActions></DialogBody></DialogSurface></Dialog>
  </section>;
}
