export type InspectorTab = "results" | "brief" | "goals" | "jobs" | "memory" | "activity";

export type Page =
  | { type: "today" }
  | { type: "program"; code: string; workId?: string; branchId?: string; tab?: InspectorTab; view?: "graph" | "list" }
  | { type: "merge" }
  | { type: "memory"; view?: "ledger" }
  | { type: "repositories" }
  | { type: "sessions" }
  | { type: "terminal" }
  | { type: "evals" }
  | { type: "clusters" }
  | { type: "ltp-monitor" }
  | { type: "skills"; view?: "proposals" }
  | { type: "settings" };

const INSPECTOR_TABS = new Set<InspectorTab>([
  "results", "brief", "goals", "jobs", "memory", "activity",
]);

export function parsePath(pathname: string, search = ""): Page {
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (!parts.length || parts[0] === "today") return { type: "today" };
  if (parts[0] === "merge") return { type: "merge" };
  if (parts[0] === "memory") return new URLSearchParams(search).get("view") === "ledger"
    ? { type: "memory", view: "ledger" } : { type: "memory" };
  if (parts[0] === "repositories") return { type: "repositories" };
  if (parts[0] === "sessions") return { type: "sessions" };
  if (parts[0] === "terminal") return { type: "terminal" };
  if (parts[0] === "evals") return { type: "evals" };
  if (parts[0] === "clusters") return { type: "clusters" };
  if (parts[0] === "ltp-monitor") return { type: "ltp-monitor" };
  if (parts[0] === "skills") return new URLSearchParams(search).get("view") === "proposals"
    ? { type: "skills", view: "proposals" } : { type: "skills" };
  if (parts[0] === "settings") return { type: "settings" };
  if (parts[0] === "programs" && parts[1]) {
    const page: Extract<Page, { type: "program" }> = { type: "program", code: parts[1].toUpperCase() };
    if (new URLSearchParams(search).get("view") === "list") page.view = "list";
    if (parts[2] === "work" && parts[3]) page.workId = parts[3].toUpperCase();
    if (parts[2] === "branches" && parts[3]) page.branchId = parts[3];
    const tab = (parts[4] === "conversation" ? "results" : parts[4]) as InspectorTab | undefined;
    if (tab && INSPECTOR_TABS.has(tab)) page.tab = tab;
    return page;
  }
  return { type: "today" };
}

export function pagePath(page: Page): string {
  if (page.type === "today") return "/today";
  if (page.type === "merge") return "/merge";
  if (page.type === "memory") return page.view === "ledger" ? "/memory?view=ledger" : "/memory";
  if (page.type === "repositories") return "/repositories";
  if (page.type === "sessions") return "/sessions";
  if (page.type === "terminal") return "/terminal";
  if (page.type === "evals") return "/evals";
  if (page.type === "clusters") return "/clusters";
  if (page.type === "ltp-monitor") return "/ltp-monitor";
  if (page.type === "skills") return page.view === "proposals" ? "/skills?view=proposals" : "/skills";
  if (page.type === "settings") return "/settings";
  const root = `/programs/${encodeURIComponent(page.code)}`;
  const view = page.view === "list" ? "?view=list" : "";
  if (page.branchId) {
    return `${root}/branches/${encodeURIComponent(page.branchId)}/${page.tab ?? "results"}${view}`;
  }
  if (page.workId) {
    return `${root}/work/${encodeURIComponent(page.workId)}/${page.tab ?? "results"}${view}`;
  }
  return `${root}${view}`;
}
