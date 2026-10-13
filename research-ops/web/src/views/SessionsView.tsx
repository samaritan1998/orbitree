import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Select,
  Skeleton,
  SkeletonItem,
  Tooltip,
} from "@fluentui/react-components";
import {
  Add20Regular,
  ArrowRight20Regular,
  ArrowSync20Regular,
  Checkmark20Regular,
  Copy20Regular,
  Edit20Regular,
  Eye20Regular,
  EyeOff20Regular,
  BranchFork20Regular,
  Link20Regular,
  List20Regular,
  Organization20Regular,
  Search20Regular,
  Sparkle20Regular,
  WindowMultiple20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime } from "../time";
import { copyText } from "../clipboard";
import type { CodexWindow, Program } from "../types";

type DialogState = { mode: "attach" | "create"; window: CodexWindow };
type Scope = "all" | "attention" | "live" | "managed" | "saved" | "ignored";

export default function SessionsView({ programs, onOpenBranch, onOpenWork }: {
  programs: Program[];
  onOpenBranch: (programCode: string, branchId: string) => void;
  onOpenWork: (programCode: string, workId: string) => void;
}) {
  const queryClient = useQueryClient();
  const windows = useQuery({
    queryKey: ["codex-windows"],
    queryFn: () => api.codexWindows(true),
    refetchInterval: 15_000,
  });
  const [scope, setScope] = useState<Scope>("live");
  const [viewMode, setViewMode] = useState<"lineage" | "list">("lineage");
  const [search, setSearch] = useState("");
  const [dialog, setDialog] = useState<DialogState>();
  const [targetBranch, setTargetBranch] = useState("");
  const [programCode, setProgramCode] = useState("FREE");
  const [copiedId, setCopiedId] = useState<string>();
  const [titleWindow, setTitleWindow] = useState<CodexWindow>();
  const [titleInput, setTitleInput] = useState("");
  const targets = useQuery({
    queryKey: ["codex-window-targets", dialog?.window.id],
    queryFn: () => api.codexWindowTargets(dialog!.window.id),
    enabled: dialog?.mode === "attach",
  });

  const refresh = useMutation({
    mutationFn: api.scanCodexWindows,
    onSuccess: (data) => queryClient.setQueryData(["codex-windows"], data),
  });
  const generateTitles = useMutation({
    mutationFn: api.generateCodexWindowTitles,
    onSuccess: (data) => queryClient.setQueryData(["codex-windows"], data),
  });
  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => (
      api.renameCodexWindow(id, title)
    ),
    onSuccess: () => {
      setTitleWindow(undefined);
      return queryClient.invalidateQueries({ queryKey: ["codex-windows"] });
    },
  });
  const visibility = useMutation({
    mutationFn: ({ id, ignored }: { id: string; ignored: boolean }) => (
      ignored ? api.ignoreCodexWindow(id) : api.restoreCodexWindow(id)
    ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["codex-windows"] }),
  });
  const attach = useMutation({
    mutationFn: ({ id, branchId }: { id: string; branchId: string }) => (
      api.attachCodexWindow(id, branchId)
    ),
    onSuccess: async (window) => {
      await queryClient.invalidateQueries({ queryKey: ["codex-windows"] });
      if (window.program_code) {
        await queryClient.invalidateQueries({ queryKey: ["tree", window.program_code] });
      }
      setDialog(undefined);
      if (window.program_code && window.branch_id) {
        onOpenBranch(window.program_code, window.branch_id);
      }
    },
  });
  const create = useMutation({
    mutationFn: ({ id, program }: { id: string; program: string }) => (
      api.createWorkFromCodexWindow(id, program)
    ),
    onSuccess: async (adoption) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["codex-windows"] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
        queryClient.invalidateQueries({ queryKey: ["tree", adoption.window.program_code] }),
      ]);
      setDialog(undefined);
      if (adoption.window.program_code) {
        onOpenWork(adoption.window.program_code, adoption.work_item.human_id);
      }
    },
  });
  const attachParent = useMutation({
    mutationFn: (id: string) => api.attachCodexWindowToParent(id),
    onSuccess: async (adoption) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["codex-windows"] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
        queryClient.invalidateQueries({ queryKey: ["tree", adoption.window.program_code] }),
      ]);
      if (adoption.window.program_code && adoption.window.branch_id) {
        onOpenBranch(adoption.window.program_code, adoption.window.branch_id);
      }
    },
  });

  const entries = windows.data ?? [];
  const metrics = {
    live: entries.filter((item) => item.live && !item.ignored).length,
    unassigned: entries.filter((item) => item.state === "unassigned").length,
    managed: entries.filter((item) => item.state === "managed").length,
    saved: entries.filter((item) => item.state === "saved").length,
  };
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return entries.filter((item) => {
      const scopeMatch = scope === "all"
        ? !item.ignored
        : scope === "attention"
          ? item.state === "unassigned"
          : scope === "live"
            ? item.live && !item.ignored
            : item.state === scope;
      const textMatch = !needle || [
        item.thread_id,
        item.display_name,
        item.cwd,
        item.work_id,
        item.work_title,
        item.program_code,
        item.tty,
      ].some((value) => value?.toLowerCase().includes(needle));
      return scopeMatch && textMatch;
    });
  }, [entries, scope, search]);
  const lineageEntries = useMemo(
    () => lineageContext(entries, filtered, scope === "ignored"),
    [entries, filtered, scope],
  );

  const openDialog = (mode: DialogState["mode"], window: CodexWindow) => {
    setTargetBranch("");
    setProgramCode("FREE");
    attach.reset();
    create.reset();
    setDialog({ mode, window });
  };
  const copyResume = async (window: CodexWindow) => {
    await copyText(window.resume_command);
    setCopiedId(window.id);
    globalThis.setTimeout(() => setCopiedId(undefined), 2000);
  };
  const openTitleDialog = (window: CodexWindow) => {
    setTitleWindow(window);
    setTitleInput(window.display_name);
    rename.reset();
  };
  const renderActions = (window: CodexWindow): ReactNode => (
    <div className="session-actions">
      <Tooltip content="Rename window" relationship="label">
        <Button
          appearance="subtle"
          icon={<Edit20Regular />}
          aria-label={`Rename ${window.display_name}`}
          onClick={() => openTitleDialog(window)}
        />
      </Tooltip>
      <Tooltip content="Copy resume command" relationship="label">
        <Button
          appearance="subtle"
          icon={copiedId === window.id ? <Checkmark20Regular /> : <Copy20Regular />}
          aria-label={copiedId === window.id ? "Resume command copied" : "Copy resume command"}
          onClick={() => copyResume(window)}
        />
      </Tooltip>
      {window.state === "managed" && window.program_code && window.branch_id && (
        <Button
          appearance="subtle"
          icon={<ArrowRight20Regular />}
          onClick={() => onOpenBranch(window.program_code!, window.branch_id!)}
        >
          Open
        </Button>
      )}
      {!window.ignored && window.state !== "managed" && <>
        {window.can_attach_to_parent && (
          <Button
            appearance="primary"
            icon={<BranchFork20Regular />}
            disabled={attachParent.isPending}
            onClick={() => attachParent.mutate(window.id)}
          >
            Attach under {window.parent_work_id}
          </Button>
        )}
        <Button appearance="subtle" icon={<Link20Regular />} onClick={() => openDialog("attach", window)}>Attach Branch</Button>
        <Button appearance={window.can_attach_to_parent ? "subtle" : "primary"} icon={<Add20Regular />} onClick={() => openDialog("create", window)}>Attach to Program</Button>
      </>}
      {window.ignored ? (
        <Tooltip content="Restore this session to the list" relationship="label">
          <Button appearance="subtle" icon={<Eye20Regular />} aria-label="Restore session" onClick={() => visibility.mutate({ id: window.id, ignored: false })} />
        </Tooltip>
      ) : (
        <Tooltip content="Hide from Sessions without stopping Codex or deleting files" relationship="label">
          <Button appearance="subtle" icon={<EyeOff20Regular />} aria-label="Ignore session" onClick={() => visibility.mutate({ id: window.id, ignored: true })} />
        </Tooltip>
      )}
    </div>
  );

  if (windows.isLoading) {
    return <div className="page-loading"><Skeleton><SkeletonItem /><SkeletonItem /><SkeletonItem /></Skeleton></div>;
  }
  if (windows.error) return <div className="page-error">{windows.error.message}</div>;

  return (
    <div className="page sessions-page">
      <header className="page-header sessions-header">
        <div>
          <h1>Sessions</h1>
          <p>Local Codex processes and resumable threads on this server.</p>
        </div>
        <div className="sessions-header-actions">
          <div className="header-metrics sessions-metrics">
            <span><strong>{metrics.live}</strong>live</span>
            <span><strong>{metrics.unassigned}</strong>unassigned</span>
            <span><strong>{metrics.managed}</strong>managed</span>
            <span><strong>{metrics.saved}</strong>saved</span>
          </div>
          <Button
            appearance="subtle"
            icon={<Sparkle20Regular />}
            disabled={generateTitles.isPending}
            onClick={() => generateTitles.mutate()}
          >
            Name windows
          </Button>
          <Button
            icon={<ArrowSync20Regular />}
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            Scan server
          </Button>
        </div>
      </header>

      <div className="sessions-toolbar">
        <Field label="Status">
          <Select value={scope} onChange={(event) => setScope(event.target.value as Scope)}>
            <option value="all">All visible</option>
            <option value="attention">Needs attention</option>
            <option value="live">Live</option>
            <option value="managed">Managed</option>
            <option value="saved">Saved</option>
            <option value="ignored">Ignored</option>
          </Select>
        </Field>
        <Field label="Search" className="sessions-search">
          <Input
            contentBefore={<Search20Regular />}
            value={search}
            onChange={(_event, data) => setSearch(data.value)}
            placeholder="Thread, directory, work"
          />
        </Field>
        <div className="view-toggle sessions-view-toggle" role="group" aria-label="Session view">
          <button aria-pressed={viewMode === "lineage"} className={viewMode === "lineage" ? "active" : ""} onClick={() => setViewMode("lineage")}><Organization20Regular />Lineage</button>
          <button aria-pressed={viewMode === "list"} className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")}><List20Regular />List</button>
        </div>
        <span>{filtered.length} matching · {viewMode === "lineage" ? lineageEntries.length : filtered.length} shown</span>
      </div>

      {(refresh.error || generateTitles.error || visibility.error || attachParent.error) && (
        <div className="inline-error">{refresh.error?.message ?? generateTitles.error?.message ?? visibility.error?.message ?? attachParent.error?.message}</div>
      )}

      {viewMode === "lineage" ? (
        <SessionLineage entries={lineageEntries} renderActions={renderActions} />
      ) : <div className="session-window-list">
        <div className="session-window-head" aria-hidden="true">
          <span>Status</span><span>Workspace</span><span>Codex thread</span><span>Work</span><span>Actions</span>
        </div>
        {!filtered.length && <div className="empty-state">
          <WindowMultiple20Regular />
          <strong>No matching sessions</strong>
          <span>Change the status filter or scan the server again.</span>
        </div>}
        {filtered.map((window) => (
          <article className="session-window-row" key={window.id}>
            <SessionState window={window} />
            <div className="session-workspace">
              <strong>{window.display_name}</strong>
              <code title={window.cwd}>{window.cwd}</code>
              {window.match_confidence === "ambiguous_cwd" && (
                <small>Multiple processes share this directory</small>
              )}
            </div>
            <div className="session-thread">
              <code title={window.thread_id}>{shortId(window.thread_id)}</code>
              <span>{window.pid ? `PID ${window.pid}` : "No live process"}{window.tty ? ` · ${window.tty}` : ""}</span>
              <time>{formatActivity(window.last_activity_at)}</time>
            </div>
            <div className="session-assignment">
              {window.work_id ? <>
                <strong>{window.work_id}</strong>
                <span>{window.work_title}</span>
                <small>{window.program_code} · {window.branch_name}</small>
              </> : <span className="unassigned-copy">Not attached</span>}
            </div>
            {renderActions(window)}
          </article>
        ))}
      </div>}

      <Dialog open={Boolean(dialog)} onOpenChange={(_event, data) => !data.open && setDialog(undefined)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{dialog?.mode === "attach" ? "Attach existing Branch" : "Attach Session to Program"}</DialogTitle>
            <DialogContent>
              {dialog && <div className="dialog-form session-dialog-copy">
                <div className="session-dialog-source">
                  <strong>{dialog.window.display_name}</strong>
                  <code>{shortId(dialog.window.thread_id)}</code>
                  <span>{dialog.window.cwd}</span>
                </div>
                {dialog.mode === "attach" ? <>
                  {targets.isLoading && <div className="inline-note" role="status">Finding Branches that use this directory...</div>}
                  {targets.isSuccess && targets.data.length > 0 && (
                    <Field label="Compatible Branch" hint="Only Branches using this exact directory are eligible.">
                      <Select value={targetBranch} onChange={(event) => setTargetBranch(event.target.value)}>
                        <option value="">Select a Branch</option>
                        {targets.data.map((target) => (
                          <option value={target.branch_id} key={target.branch_id}>
                            {target.program_code} · {target.work_id} · {target.branch_name}
                          </option>
                        ))}
                      </Select>
                    </Field>
                  )}
                  {targets.isSuccess && !targets.data.length && (
                    <div className="compatible-empty" role="status">
                      <strong>No compatible Branch</strong>
                      <span>No existing Branch uses this Session's directory. Attach it to a Program to create a lightweight tracking WorkItem without moving any files.</span>
                    </div>
                  )}
                </> : <>
                  <Field label="Program" hint="The new WorkItem will use the Session's current directory.">
                    <Select value={programCode} onChange={(event) => setProgramCode(event.target.value)}>
                      {programs.map((program) => <option value={program.code} key={program.code}>{program.code} · {program.name}</option>)}
                    </Select>
                  </Field>
                  <div className="inline-note">Research Ops creates a lightweight WorkItem under this Program so the Session has a durable owner. The directory and its files stay in place.</div>
                </>}
                {(targets.error || attach.error || create.error) && (
                  <div className="inline-error">{targets.error?.message ?? attach.error?.message ?? create.error?.message}</div>
                )}
              </div>}
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setDialog(undefined)}>Cancel</Button>
              {dialog?.mode === "attach" && targets.isSuccess && !targets.data.length ? (
                <Button
                  appearance="primary"
                  icon={<Add20Regular />}
                  onClick={() => {
                    setProgramCode("FREE");
                    attach.reset();
                    setDialog({ mode: "create", window: dialog.window });
                  }}
                >
                  Attach to Program
                </Button>
              ) : dialog?.mode === "attach" ? (
                <Button
                  appearance="primary"
                  icon={<Link20Regular />}
                  disabled={!targetBranch || attach.isPending}
                  onClick={() => attach.mutate({ id: dialog.window.id, branchId: targetBranch })}
                >
                  Attach Branch
                </Button>
              ) : dialog ? (
                <Button
                  appearance="primary"
                  icon={<Add20Regular />}
                  disabled={!programCode || create.isPending}
                  onClick={() => create.mutate({ id: dialog.window.id, program: programCode })}
                >
                  Attach to Program
                </Button>
              ) : null}
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>

      <Dialog open={Boolean(titleWindow)} onOpenChange={(_event, data) => !data.open && setTitleWindow(undefined)}>
        <DialogSurface>
          <DialogBody>
            <DialogTitle>Rename window</DialogTitle>
            <DialogContent>
              <div className="dialog-form">
                <Field label="Window name" validationMessage={rename.error?.message}>
                  <Input
                    value={titleInput}
                    maxLength={80}
                    autoFocus
                    onChange={(_event, data) => setTitleInput(data.value)}
                  />
                </Field>
                {titleWindow && <code>{shortId(titleWindow.thread_id)}</code>}
              </div>
            </DialogContent>
            <DialogActions>
              <Button appearance="secondary" onClick={() => setTitleWindow(undefined)}>Cancel</Button>
              <Button
                appearance="primary"
                disabled={!titleInput.trim() || rename.isPending}
                onClick={() => titleWindow && rename.mutate({ id: titleWindow.id, title: titleInput })}
              >
                Save
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  );
}

function SessionLineage({ entries, renderActions }: {
  entries: CodexWindow[];
  renderActions: (window: CodexWindow) => ReactNode;
}) {
  if (!entries.length) return <div className="empty-state">
    <WindowMultiple20Regular />
    <strong>No matching sessions</strong>
    <span>Change the status filter or scan the server again.</span>
  </div>;
  const visibleThreads = new Set(entries.map((item) => item.thread_id));
  const children = new Map<string, CodexWindow[]>();
  for (const item of entries) {
    if (item.parent_thread_id && visibleThreads.has(item.parent_thread_id)) {
      children.set(item.parent_thread_id, [
        ...(children.get(item.parent_thread_id) ?? []),
        item,
      ]);
    }
  }
  let roots = entries.filter(
    (item) => !item.parent_thread_id || !visibleThreads.has(item.parent_thread_id),
  );
  if (!roots.length) roots = entries.slice(0, 1);
  return <div className="session-lineage" aria-label="Codex session lineage">
    <ul className="session-lineage-roots">
      {roots.map((item) => (
        <LineageNode
          key={item.id}
          window={item}
          children={children}
          ancestors={new Set<string>()}
          renderActions={renderActions}
        />
      ))}
    </ul>
  </div>;
}

function LineageNode({ window, children, ancestors, renderActions }: {
  window: CodexWindow;
  children: Map<string, CodexWindow[]>;
  ancestors: Set<string>;
  renderActions: (window: CodexWindow) => ReactNode;
}) {
  const childWindows = children.get(window.thread_id) ?? [];
  const nextAncestors = new Set(ancestors);
  nextAncestors.add(window.thread_id);
  return <li>
    <article className={`session-lineage-node state-${window.state}`}>
      <SessionState window={window} />
      <div className="lineage-session-copy">
        <strong>{window.display_name}</strong>
        <code title={window.thread_id}>{shortId(window.thread_id)}</code>
        <span>
          {window.parent_thread_id ? `Fork of ${shortId(window.parent_thread_id)}` : "Root Thread"}
          {childWindows.length ? ` · ${childWindows.length} child${childWindows.length > 1 ? "ren" : ""}` : ""}
        </span>
        <span className="lineage-process">{window.pid ? `PID ${window.pid}` : "No live process"}{window.tty ? ` · ${window.tty}` : ""}</span>
        <small title={window.cwd}>{window.cwd}</small>
      </div>
      <div className="session-assignment lineage-assignment">
        {window.work_id ? <>
          <strong>{window.work_id}</strong>
          <span>{window.work_title}</span>
          <small>{window.program_code} · {window.branch_name}</small>
        </> : window.parent_work_id ? <>
          <strong>Parent: {window.parent_work_id}</strong>
          <span>Ready to attach under parent</span>
          <small>{window.parent_program_code}</small>
        </> : <span className="unassigned-copy">Not attached</span>}
      </div>
      {renderActions(window)}
    </article>
    {childWindows.length > 0 && <ul>
      {childWindows
        .filter((child) => !nextAncestors.has(child.thread_id))
        .map((child) => (
          <LineageNode
            key={child.id}
            window={child}
            children={children}
            ancestors={nextAncestors}
            renderActions={renderActions}
          />
        ))}
    </ul>}
  </li>;
}

function lineageContext(
  entries: CodexWindow[],
  matches: CodexWindow[],
  includeIgnored: boolean,
): CodexWindow[] {
  const eligible = entries.filter((item) => includeIgnored || !item.ignored);
  const byThread = new Map(eligible.map((item) => [item.thread_id, item]));
  const children = new Map<string, CodexWindow[]>();
  for (const item of eligible) {
    if (item.parent_thread_id) {
      children.set(item.parent_thread_id, [
        ...(children.get(item.parent_thread_id) ?? []),
        item,
      ]);
    }
  }
  const included = new Set(matches.map((item) => item.thread_id));
  for (const match of matches) {
    let parentId = match.parent_thread_id;
    const visited = new Set<string>();
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = byThread.get(parentId);
      if (!parent) break;
      included.add(parent.thread_id);
      parentId = parent.parent_thread_id;
    }
    const pending = [...(children.get(match.thread_id) ?? [])];
    while (pending.length) {
      const child = pending.pop()!;
      if (included.has(child.thread_id)) continue;
      included.add(child.thread_id);
      pending.push(...(children.get(child.thread_id) ?? []));
    }
  }
  return eligible.filter((item) => included.has(item.thread_id));
}

function SessionState({ window }: { window: CodexWindow }) {
  const label = window.state === "unassigned"
    ? "Unassigned"
    : window.state === "managed"
      ? "Managed"
      : window.state === "ignored"
        ? "Ignored"
        : "Saved";
  return <div className={`session-state state-${window.state}`}>
    <span className={window.live ? "live-dot active" : "live-dot"} />
    <strong>{label}</strong>
    <small>{window.live ? "Live process" : "Not running"}</small>
  </div>;
}

function shortId(value: string): string {
  return value.length > 22 ? `${value.slice(0, 10)}…${value.slice(-8)}` : value;
}

function formatActivity(value: string): string {
  return formatDateTime(value, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
