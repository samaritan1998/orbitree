import type { InspectorTab, Page } from "./router";
import type { Branch, Program, WorkItem } from "./types";

const PRODUCT_NAME = "Orbitree";

const PAGE_LABELS: Record<Exclude<Page["type"], "program">, string> = {
  today: "今日",
  merge: "Merge Queue",
  memory: "每日复盘",
  repositories: "Repositories",
  sessions: "会话",
  terminal: "终端",
  evals: "Eval Harness",
  clusters: "集群资源 · 任务与资源",
  "ltp-monitor": "任务监控 · 任务与资源",
  skills: "技能",
  settings: "设置",
};

const TAB_LABELS: Record<InspectorTab, string> = {
  results: "Results",
  brief: "Brief",
  goals: "Goals",
  jobs: "Jobs",
  memory: "Memory",
  activity: "Activity",
};

export function routeDocumentTitle(page: Page): string {
  if (page.type === "program") return title([page.code]);
  if (page.type === "memory" && page.view === "ledger") return title(["记忆账本"]);
  if (page.type === "skills" && page.view === "proposals") return title(["技能审核"]);
  return title([PAGE_LABELS[page.type]]);
}

export function programDocumentTitle({
  program,
  workItem,
  branch,
  tab,
  isDesk = false,
}: {
  program: Program;
  workItem?: WorkItem;
  branch?: Branch;
  tab?: InspectorTab;
  isDesk?: boolean;
}): string {
  if (isDesk) return title([program.code, "Desk", tabSuffix(tab)]);
  if (!workItem) return title([program.code, program.name]);

  const branchName = branch && branch.name.trim().toLowerCase() !== "main"
    ? branch.name
    : undefined;
  return title([
    workItem.human_id,
    branchName ?? workItem.title,
    tabSuffix(tab),
  ]);
}

function tabSuffix(tab?: InspectorTab): string | undefined {
  return tab && tab !== "results" ? TAB_LABELS[tab] : undefined;
}

function title(parts: Array<string | undefined>): string {
  const context = parts
    .map((part) => compact(part ?? ""))
    .filter(Boolean)
    .join(" · ");
  return context ? `${context} | ${PRODUCT_NAME}` : PRODUCT_NAME;
}

function compact(value: string, maxLength = 52): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 3)}...`;
}
