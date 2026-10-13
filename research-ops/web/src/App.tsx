import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@fluentui/react-components";
import { Dismiss20Regular } from "@fluentui/react-icons";
import { api } from "./api";
import AppShell from "./components/AppShell";
import { pagePath, parsePath, type Page } from "./router";
import NewWorkDialog from "./components/NewWorkDialog";
import MemoryView from "./views/MemoryView";
import MergeView from "./views/MergeView";
import ProgramView from "./views/ProgramView";
import RepositoriesView from "./views/RepositoriesView";
import SessionsView from "./views/SessionsView";
import EvalHarnessView from "./views/EvalHarnessView";
import ResourcesView from "./views/ResourcesView";
import SkillsView from "./views/SkillsView";
import TodayView from "./views/TodayView";
import SettingsView from "./views/SettingsView";
import type { WorkItem } from "./types";
import { routeDocumentTitle } from "./documentTitle";

const TerminalView = lazy(() => import("./views/TerminalView"));

export default function App() {
  const queryClient = useQueryClient();
  const programs = useQuery({ queryKey: ["programs"], queryFn: api.programs });
  const [page, setPage] = useState<Page>(() => parsePath(window.location.pathname, window.location.search));
  const [newWorkOpen, setNewWorkOpen] = useState(false);
  const [scratchWork, setScratchWork] = useState<WorkItem>();

  const selectedProgram = useMemo(() => {
    if (page.type !== "program") return undefined;
    return programs.data?.find((program) => program.code === page.code);
  }, [page, programs.data]);

  useEffect(() => {
    const restore = () => {
      const next = parsePath(window.location.pathname, window.location.search);
      const canonical = pagePath(next);
      if (canonical !== `${window.location.pathname}${window.location.search}`) {
        window.history.replaceState({}, "", canonical);
      }
      setPage(next);
    };
    restore();
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);

  useEffect(() => {
    if (programs.isSuccess && page.type === "program" && !selectedProgram) {
      window.history.replaceState({}, "", pagePath({ type: "today" }));
      setPage({ type: "today" });
    }
  }, [page, programs.isSuccess, selectedProgram]);

  useEffect(() => {
    if (page.type !== "program" || !selectedProgram) {
      document.title = routeDocumentTitle(page);
    }
  }, [page, selectedProgram]);

  const navigate = (next: Page, replace = false) => {
    const path = pagePath(next);
    if (path !== `${window.location.pathname}${window.location.search}`) {
      window.history[replace ? "replaceState" : "pushState"]({}, "", path);
    }
    setPage(next);
  };

  const undoScratch = useMutation({
    mutationFn: () => {
      if (!scratchWork) throw new Error("No scratch work to undo");
      return api.undoWork(scratchWork.id);
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["today"] }),
        queryClient.invalidateQueries({ queryKey: ["tree"] }),
        queryClient.invalidateQueries({ queryKey: ["program-desk"] }),
      ]);
      if (page.type === "program" && page.workId === scratchWork?.human_id) {
        navigate({ type: "today" }, true);
      }
      setScratchWork(undefined);
    },
  });

  return (
    <AppShell
      page={page}
      programs={programs.data ?? []}
      onNavigate={navigate}
      onNewWork={() => setNewWorkOpen(true)}
    >
      {page.type === "today" && <TodayView
        scratchProgram={programs.data?.find((program) => program.code === "FREE")?.code ?? programs.data?.[0]?.code ?? ""}
        onOpenWork={(work) => navigate({ type: "program", code: work.human_id.split("-")[0], workId: work.human_id })}
        onQuickCreated={(work) => {
          setScratchWork(work);
          navigate({ type: "program", code: work.human_id.split("-")[0], workId: work.human_id });
        }}
      />}
      {page.type === "program" && selectedProgram && <ProgramView program={selectedProgram} page={page} onNavigate={navigate} />}
      {page.type === "merge" && <MergeView />}
      {page.type === "memory" && <MemoryView programs={programs.data ?? []} section={page.view === "ledger" ? "ledger" : "reflections"} />}
      {page.type === "repositories" && <RepositoriesView programs={programs.data ?? []} />}
      {page.type === "sessions" && <SessionsView
        programs={programs.data ?? []}
        onOpenBranch={(programCode, branchId) => navigate({
          type: "program",
          code: programCode,
          branchId,
          tab: "results",
        })}
        onOpenWork={(programCode, workId) => navigate({
          type: "program",
          code: programCode,
          workId,
          tab: "results",
        })}
      />}
      {page.type === "evals" && <EvalHarnessView />}
      {(page.type === "clusters" || page.type === "ltp-monitor") && <ResourcesView section={page.type} onNavigate={navigate} />}
      {page.type === "skills" && <SkillsView view={page.view === "proposals" ? "proposals" : "installed"} />}
      {page.type === "settings" && <SettingsView />}
      {page.type === "terminal" && <Suspense fallback={<p role="status">正在加载终端…</p>}><TerminalView /></Suspense>}
      <NewWorkDialog
        open={newWorkOpen}
        programs={programs.data ?? []}
        initialProgram={page.type === "program" ? page.code : programs.data?.[0]?.code ?? ""}
        onClose={() => setNewWorkOpen(false)}
        onCreated={(code, workId) => {
          setNewWorkOpen(false);
          navigate({ type: "program", code, workId });
        }}
      />
      {scratchWork && <aside className="scratch-created-notice" role="status" aria-live="polite">
        <div>
          <strong>Created {scratchWork.human_id}</strong>
          <span>Undefined scratch work in Free Work</span>
          {undoScratch.error && <small>{undoScratch.error.message}</small>}
        </div>
        <Button disabled={undoScratch.isPending} onClick={() => undoScratch.mutate()}>Undo</Button>
        <Button
          appearance="subtle"
          icon={<Dismiss20Regular />}
          aria-label="Dismiss scratch work confirmation"
          onClick={() => setScratchWork(undefined)}
        />
      </aside>}
    </AppShell>
  );
}
