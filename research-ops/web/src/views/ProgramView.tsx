import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Skeleton, SkeletonItem, Tooltip } from "@fluentui/react-components";
import {
  ArrowSync20Regular,
  BrainCircuit20Regular,
  BranchFork20Regular,
  Eye20Regular,
  EyeOff20Regular,
  List20Regular,
  Organization20Regular,
  PanelRightContract20Regular,
  PanelRightExpand20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import BranchPanel from "../components/BranchPanel";
import ForkDialog from "../components/ForkDialog";
import TreeCanvas from "../components/TreeCanvas";
import ProgramList from "../components/ProgramList";
import type { Branch, Program } from "../types";
import type { Page } from "../router";
import { programDocumentTitle } from "../documentTitle";

type ProgramPage = Extract<Page, { type: "program" }>;

export default function ProgramView({ program, page, onNavigate }: {
  program: Program;
  page: ProgramPage;
  onNavigate: (page: Page) => void;
}) {
  const queryClient = useQueryClient();
  const tree = useQuery({
    queryKey: ["tree", program.code],
    queryFn: () => api.tree(program.code),
    refetchInterval: 12_000,
  });
  const desk = useQuery({
    queryKey: ["program-desk", program.code],
    queryFn: () => api.programDesk(program.code),
    refetchInterval: 12_000,
  });
  const [selectedBranchId, setSelectedBranchId] = useState<string>();
  const [forkOpen, setForkOpen] = useState(false);
  const [navigatorOpen, setNavigatorOpen] = useState(() => readNavigatorPreference(program.code));
  const [showHidden, setShowHidden] = useState(false);
  const viewMode = page.view ?? "graph";

  useEffect(() => {
    if (page.branchId) {
      setSelectedBranchId(page.branchId);
      return;
    }
    if (page.workId && tree.data) {
      const work = tree.data.work_items.find((item) => item.human_id === page.workId);
      const main = work && tree.data.branches.find((item) => item.work_item_id === work.id && !item.parent_branch_id);
      setSelectedBranchId(main?.id);
      return;
    }
    setSelectedBranchId(undefined);
  }, [page.branchId, page.workId, program.code, tree.data]);

  useEffect(() => {
    setNavigatorOpen(readNavigatorPreference(program.code));
  }, [program.code]);

  const deskBranch = desk.data?.branch;
  const selectedBranch = useMemo(
    () => tree.data?.branches.find((branch) => branch.id === selectedBranchId)
      ?? tree.data?.hidden_branches?.find((branch) => branch.id === selectedBranchId)
      ?? (deskBranch?.id === selectedBranchId ? deskBranch : undefined),
    [deskBranch, tree.data, selectedBranchId],
  );
  const isDeskSelected = deskBranch?.id === selectedBranchId;
  const selectedWork = useMemo(() => {
    if (isDeskSelected) return desk.data?.work_item;
    if (selectedBranch) {
      return tree.data?.work_items.find((work) => work.id === selectedBranch.work_item_id);
    }
    if (page.workId) {
      return tree.data?.work_items.find((work) => work.human_id === page.workId);
    }
    return undefined;
  }, [desk.data?.work_item, isDeskSelected, page.workId, selectedBranch, tree.data?.work_items]);

  useEffect(() => {
    document.title = programDocumentTitle({
      program,
      workItem: selectedWork,
      branch: selectedBranch,
      tab: page.tab,
      isDesk: isDeskSelected,
    });
  }, [isDeskSelected, page.tab, program, selectedBranch, selectedWork]);

  const setNavigatorVisibility = useCallback((open: boolean) => {
    setNavigatorOpen(open);
    writeNavigatorPreference(program.code, open);
  }, [program.code]);

  const selectBranch = useCallback((branchId: string) => {
    setNavigatorVisibility(false);
    onNavigate({ type: "program", code: program.code, branchId, tab: "results", view: viewMode });
  }, [onNavigate, program.code, setNavigatorVisibility, viewMode]);

  useEffect(() => {
    if (
      !selectedBranch
      || selectedBranch.status !== "continued"
      || !selectedBranch.continued_by_branch_id
      || !tree.data
    ) return;
    const latestSession = tree.data.sessions
      .filter((session) => session.branch_id === selectedBranch.id)
      .sort((left, right) => Date.parse(right.heartbeat_at) - Date.parse(left.heartbeat_at))[0];
    const sessionActive = Boolean(
      latestSession
      && ["starting", "active"].includes(latestSession.status)
      && Date.parse(latestSession.lease_expires_at) > Date.now(),
    );
    if (sessionActive) return;
    const targetId = visibleContinuationTarget(
      selectedBranch.continued_by_branch_id,
      tree.data.branches,
      tree.data.hidden_branches ?? [],
    );
    if (!targetId) return;
    const key = continuationSeenKey(selectedBranch.id, targetId);
    try {
      if (window.sessionStorage.getItem(key)) return;
      window.sessionStorage.setItem(key, "1");
    } catch {
      // Navigation still works when browser storage is unavailable.
    }
    selectBranch(targetId);
  }, [selectBranch, selectedBranch, tree.data]);

  if (tree.isLoading) {
    return <div className="page-loading"><Skeleton><SkeletonItem /><SkeletonItem /><SkeletonItem /></Skeleton></div>;
  }
  if (tree.error || !tree.data) {
    return <div className="page-error">{tree.error?.message ?? "Program tree is unavailable"}</div>;
  }
  const hiddenBranches = tree.data.hidden_branches ?? [];
  const navigationTree = showHidden
    ? { ...tree.data, branches: [...tree.data.branches, ...hiddenBranches] }
    : tree.data;

  return (
    <div className={`program-workspace${selectedBranchId ? " branch-selected" : ""}${navigatorOpen ? " navigator-expanded" : ""}`}>
      <header className="program-header">
        <div className="program-context">
          <div className="program-title-row">
            <span className="program-swatch large" style={{ backgroundColor: program.color }} />
            <h1>{program.name}</h1>
            <span>{program.code}</span>
          </div>
          <p>{program.objective}</p>
        </div>
        <div className="program-actions">
          {hiddenBranches.length > 0 && <Button
            className="hidden-toggle"
            appearance={showHidden ? "primary" : "subtle"}
            icon={showHidden ? <Eye20Regular /> : <EyeOff20Regular />}
            aria-label={showHidden ? "Hide hidden branches" : `Show ${hiddenBranches.length} hidden branches`}
            aria-pressed={showHidden}
            onClick={() => setShowHidden((value) => !value)}
          >
            <span className="program-action-label">{showHidden ? "Hide hidden" : `Hidden ${hiddenBranches.length}`}</span>
          </Button>}
          {selectedBranch && <Tooltip
            content={navigatorOpen ? "Hide branch navigator" : "Show branch navigator"}
            relationship="description"
          >
            <Button
              className="navigator-toggle"
              appearance="subtle"
              aria-label={navigatorOpen ? "Hide branch navigator" : "Show branch navigator"}
              aria-pressed={navigatorOpen}
              icon={navigatorOpen ? <PanelRightContract20Regular /> : <PanelRightExpand20Regular />}
              onClick={() => setNavigatorVisibility(!navigatorOpen)}
            >
              <span className="navigator-toggle-label">{navigatorOpen ? "Hide graph" : "Graph"}</span>
            </Button>
          </Tooltip>}
          {(!selectedBranch || navigatorOpen) && <div className="view-toggle" role="group" aria-label="Program view">
            <button aria-pressed={viewMode === "graph"} className={viewMode === "graph" ? "active" : ""} onClick={() => onNavigate({ ...page, view: "graph" })}><Organization20Regular />Graph</button>
            <button aria-pressed={viewMode === "list"} className={viewMode === "list" ? "active" : ""} onClick={() => onNavigate({ ...page, view: "list" })}><List20Regular />List</button>
          </div>}
          <Tooltip content="Open the persistent coordinator for this Program" relationship="description">
            <Button
              className="desk-action"
              appearance={isDeskSelected ? "primary" : "secondary"}
              icon={<BrainCircuit20Regular />}
              aria-label="Open Program desk"
              disabled={!desk.data}
              onClick={() => desk.data && selectBranch(desk.data.branch.id)}
            >
              <span className="program-action-label">Desk</span>
            </Button>
          </Tooltip>
          <Tooltip content="Refresh Program" relationship="description">
            <Button className="refresh-action" icon={<ArrowSync20Regular />} aria-label="Refresh Program" onClick={() => tree.refetch()}>
              <span className="program-action-label">Refresh</span>
            </Button>
          </Tooltip>
          {!isDeskSelected && <Tooltip content="Fork this Branch" relationship="description">
            <Button
              className="fork-action"
              icon={<BranchFork20Regular />}
              aria-label="Fork this Branch"
              disabled={!selectedBranch}
              onClick={() => setForkOpen(true)}
            >
              <span className="program-action-label">Fork</span>
            </Button>
          </Tooltip>}
        </div>
      </header>

      <div className={`tree-workspace-grid ${selectedBranchId ? (navigatorOpen ? "split" : "conversation-only") : "navigator-only"}`}>
        {selectedBranchId && <aside className="branch-inspector open" aria-label="Branch workspace">
          <BranchPanel
            branchId={selectedBranchId}
            program={program}
            initialTab={page.tab}
            onTabChange={(tab) => onNavigate({ ...page, tab })}
            onClose={() => onNavigate({ type: "program", code: program.code, view: viewMode })}
            onOpenBranch={selectBranch}
          />
        </aside>}
        {(!selectedBranchId || navigatorOpen) && <section className="program-navigator" aria-label="Program branch navigator">
          {viewMode === "graph"
            ? <TreeCanvas compact={Boolean(selectedBranchId)} tree={navigationTree} selectedBranchId={selectedBranchId} onSelectBranch={selectBranch} />
            : <ProgramList tree={navigationTree} selectedBranchId={selectedBranchId} onSelectBranch={selectBranch} />}
        </section>}
      </div>

      {selectedBranchId && !isDeskSelected && <ForkDialog
        branchId={selectedBranchId}
        open={forkOpen}
        onClose={() => setForkOpen(false)}
        onCreated={async (branchId, continuation) => {
          setForkOpen(false);
          await queryClient.invalidateQueries({ queryKey: ["tree", program.code] });
          if (continuation === "continue") {
            try {
              window.sessionStorage.setItem(
                continuationSeenKey(selectedBranchId, branchId),
                "1",
              );
            } catch {
              // Direct navigation remains available without browser storage.
            }
            onNavigate({
              type: "program",
              code: program.code,
              branchId,
              tab: "results",
              view: viewMode,
            });
          }
        }}
      />}
    </div>
  );
}

function continuationSeenKey(parentBranchId: string, childBranchId: string): string {
  return `research-ops:continuation:${parentBranchId}:${childBranchId}`;
}

function navigatorPreferenceKey(programCode: string): string {
  return `research-ops:program-navigator:${programCode}`;
}

function readNavigatorPreference(programCode: string): boolean {
  try {
    return window.localStorage.getItem(navigatorPreferenceKey(programCode)) === "open";
  } catch {
    return false;
  }
}

function writeNavigatorPreference(programCode: string, open: boolean): void {
  try {
    window.localStorage.setItem(navigatorPreferenceKey(programCode), open ? "open" : "closed");
  } catch {
    // Results-first navigation remains available when browser storage is disabled.
  }
}

function visibleContinuationTarget(
  branchId: string,
  visibleBranches: Branch[],
  hiddenBranches: Branch[],
): string | undefined {
  const visibleIds = new Set(visibleBranches.map((branch) => branch.id));
  const branches = new Map(
    [...visibleBranches, ...hiddenBranches].map((branch) => [branch.id, branch]),
  );
  const visited = new Set<string>();
  let currentId: string | undefined = branchId;
  while (currentId && !visited.has(currentId)) {
    visited.add(currentId);
    if (visibleIds.has(currentId)) return currentId;
    currentId = branches.get(currentId)?.continued_by_branch_id;
  }
  return undefined;
}
