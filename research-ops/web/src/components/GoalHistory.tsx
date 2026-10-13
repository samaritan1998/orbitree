import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Tooltip } from "@fluentui/react-components";
import { ArrowSync20Regular, Checkmark20Regular, Copy20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import { copyText } from "../clipboard";
import { formatDateTime } from "../time";
import type { GoalRun, NativeGoalHistoryEntry } from "../types";

const labels: Record<string, string> = {
  active: "Active", paused: "Paused", blocked: "Blocked", complete: "Complete",
  completed: "Complete", usageLimited: "Usage limited", budgetLimited: "Budget limited",
  cleared: "Cleared", unknown: "Unknown", needs_decision: "Needs decision",
  failed: "Failed", stopped: "Stopped",
};

export default function GoalHistory({ branchId, legacyGoals = [] }: {
  branchId: string;
  legacyGoals?: GoalRun[];
}) {
  const history = useInfiniteQuery({
    queryKey: ["native-goal-history", branchId],
    queryFn: ({ pageParam }) => api.nativeGoalHistory(branchId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: false,
  });
  const entries = history.data?.pages.flatMap((page) => page.items ?? []) ?? [];
  const total = (history.data?.pages[0]?.total ?? 0) + legacyGoals.length;
  return <section className="goal-history" aria-label="Goal history">
    <header className="goal-toolbar">
      <h3>Goal history{total > 0 ? ` (${total})` : ""}</h3>
      <Tooltip content="Refresh goal history" relationship="label">
        <Button size="small" appearance="subtle" icon={<ArrowSync20Regular />}
          disabled={history.isFetching} onClick={() => void history.refetch()} />
      </Tooltip>
    </header>
    {history.isPending && <p role="status">Loading goal history</p>}
    {history.isError && <div className="inline-error" role="alert">Goal history unavailable: {history.error.message}</div>}
    {!history.isPending && !history.isError && !total && <p>No historical goals recorded.</p>}
    {entries.map((entry) => <HistoryEntry key={entry.goal_key} entry={entry} />)}
    {legacyGoals.map((goal) => <details className="goal-history-entry" key={goal.id}>
      <summary>
        <span className="goal-history-heading"><strong>{goal.title || goal.objective}</strong>
          <small>Legacy Research Ops goal · Last status: {labels[goal.status] || goal.status}</small></span>
        <time dateTime={goal.updated_at}>{formatDateTime(goal.updated_at)}</time>
      </summary>
      <div className="goal-history-body">
        <p className="goal-history-objective">{goal.objective}</p>
        <dl className="goal-facts">
          <div><dt>Created</dt><dd>{formatDateTime(goal.created_at)}</dd></div>
          <div><dt>Last recorded</dt><dd>{formatDateTime(goal.updated_at)}</dd></div>
        </dl>
        {goal.error && <p className="goal-history-error">{goal.error}</p>}
      </div>
    </details>)}
    {history.hasNextPage && <Button size="small" appearance="secondary"
      disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
      {history.isFetchingNextPage ? "Loading" : "Load older goals"}
    </Button>}
  </section>;
}

function HistoryEntry({ entry }: { entry: NativeGoalHistoryEntry }) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const date = entry.goal_created_at
    ? new Date(entry.goal_created_at * 1_000).toISOString() : entry.first_recorded_at;
  const copySession = async () => {
    try {
      await copyText(entry.thread_id);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  };
  return <details className="goal-history-entry">
    <summary>
      <span className="goal-history-heading">
        <strong>{entry.objective}</strong>
        <small>Last status: {labels[entry.status] || entry.status}</small>
      </span>
      <time dateTime={date}>{formatDateTime(date)}</time>
    </summary>
    <div className="goal-history-body">
      <p className="goal-history-objective">{entry.objective}</p>
      <dl className="goal-facts">
        <div><dt>{entry.goal_created_at ? "Created" : "First recorded"}</dt><dd>{formatDateTime(date)}</dd></div>
        <div><dt>Last recorded</dt><dd>{formatDateTime(entry.last_recorded_at)}</dd></div>
        <div><dt>Tokens used</dt><dd>{entry.tokens_used === null ? "Not recorded" : entry.tokens_used.toLocaleString("en")}</dd></div>
        <div><dt>Elapsed</dt><dd>{entry.time_used_seconds === null ? "Not recorded" : `${Math.round(entry.time_used_seconds / 60).toLocaleString("en")} min`}</dd></div>
        <div><dt>Record source</dt><dd>{entry.source === "live" ? "Goal observation" : entry.source === "monitor" ? "Recovered monitor snapshot" : "Recovered notification"}</dd></div>
        {entry.cleared_at && <div><dt>Cleared</dt><dd>{formatDateTime(entry.cleared_at)}</dd></div>}
      </dl>
      <div className="goal-history-session">
        <span>Codex Session ID</span><code>{entry.thread_id}</code>
        <Tooltip content={copyStatus === "copied" ? "Session ID copied" : "Copy session ID"} relationship="label">
          <Button size="small" appearance="subtle" icon={copyStatus === "copied" ? <Checkmark20Regular /> : <Copy20Regular />} onClick={() => void copySession()} />
        </Tooltip>
      </div>
      {copyStatus === "failed" && <small role="status">Copy failed</small>}
      {entry.last_summary && <div className="goal-history-update"><h4>Last recorded update</h4><p>{entry.last_summary}</p></div>}
      {entry.last_error && <div className="goal-history-update goal-history-error"><h4>Last recorded error</h4><p>{entry.last_error}</p></div>}
    </div>
  </details>;
}
