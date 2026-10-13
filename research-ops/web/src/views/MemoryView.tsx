import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Select, Skeleton, SkeletonItem } from "@fluentui/react-components";
import {
  ArrowSync20Regular,
  Checkmark20Regular,
  Dismiss20Regular,
} from "@fluentui/react-icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { DailyReflection, Program } from "../types";

type MemorySection = "reflections" | "ledger";

export default function MemoryView({ programs, section = "reflections" }: { programs: Program[]; section?: MemorySection }) {
  const queryClient = useQueryClient();
  const [program, setProgram] = useState("");
  const memory = useQuery({
    queryKey: ["memory", program],
    queryFn: () => api.memory(program || undefined),
    enabled: section === "ledger",
  });
  const reflections = useQuery({
    queryKey: ["daily-reflections"],
    queryFn: api.dailyReflections,
    enabled: section === "reflections",
    refetchInterval: (query) => query.state.data?.some((item) => (
      item.status === "queued" || item.status === "running"
    )) ? 5_000 : 30_000,
  });
  const review = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => api.reviewMemory(id, status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["memory"] }),
  });
  const runReflection = useMutation({
    mutationFn: ({ day, force }: { day?: string; force: boolean }) => (
      api.runDailyReflection(day, force)
    ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["daily-reflections"] }),
  });

  return (
    <div className="page memory-page">
      <header className="page-header memory-header">
        <div><h1>{section === "reflections" ? "每日复盘" : "记忆账本"}</h1></div>
        {section === "ledger" && <Select
          value={program}
          onChange={(_, data) => setProgram(data.value)}
          aria-label="Filter Program"
        >
          <option value="">All Programs</option>
          {programs.map((item) => <option value={item.code} key={item.code}>{item.name}</option>)}
        </Select>}
        {section === "reflections" && <Button
          icon={<ArrowSync20Regular />}
          disabled={runReflection.isPending || reflections.data?.some((item) => item.status === "running")}
          onClick={() => runReflection.mutate({ force: false })}
        >
          Run previous day
        </Button>}
      </header>

      {section === "reflections" && <DailyReflections
        reflections={reflections.data}
        loading={reflections.isLoading}
        error={reflections.error ?? runReflection.error}
        retrying={runReflection.isPending}
        onRetry={(day) => runReflection.mutate({ day, force: true })}
      />}

      {section === "ledger" && <>
        {memory.isLoading && <Skeleton><SkeletonItem /><SkeletonItem /></Skeleton>}
        {memory.error && <div className="page-error">{memory.error.message}</div>}
        {!memory.isLoading && !memory.data?.length && <div className="empty-state"><strong>No memory entries</strong><span>Accepted branch results will appear here.</span></div>}
        <div className="memory-ledger">
          {memory.data?.map((entry) => (
            <article key={entry.id}>
              <div className="memory-meta">
                <span>{entry.kind}</span>
                <span>{entry.scope_type}</span>
                <span>{Math.round(entry.confidence * 100)}%</span>
                <strong>{entry.status}</strong>
              </div>
              <h2>{entry.title}</h2>
              <p>{entry.content}</p>
              {!!entry.evidence_refs.length && <div className="evidence-refs">{entry.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}</div>}
              {entry.status === "proposed" && <div className="memory-actions">
                <Button icon={<Dismiss20Regular />} onClick={() => review.mutate({ id: entry.id, status: "rejected" })}>Reject</Button>
                <Button appearance="primary" icon={<Checkmark20Regular />} onClick={() => review.mutate({ id: entry.id, status: "accepted" })}>Accept</Button>
              </div>}
            </article>
          ))}
        </div>
      </>}
    </div>
  );
}

function DailyReflections({
  reflections,
  loading,
  error,
  retrying,
  onRetry,
}: {
  reflections?: DailyReflection[];
  loading: boolean;
  error: Error | null;
  retrying: boolean;
  onRetry: (day: string) => void;
}) {
  if (loading) return <Skeleton><SkeletonItem /><SkeletonItem /><SkeletonItem /></Skeleton>;
  if (error) return <div className="page-error">{error.message}</div>;
  if (!reflections?.length) {
    return <div className="empty-state">
      <strong>No daily reflections yet</strong>
      <span>The first report runs after the next Shanghai midnight, or use Run previous day.</span>
    </div>;
  }
  return <div className="daily-reflection-list">
    {reflections.map((reflection, index) => (
      <article className={`daily-reflection status-${reflection.status}`} key={reflection.id}>
        <details open={index === 0 || reflection.status !== "completed"}>
          <summary>
            <div>
              <time dateTime={reflection.reflection_date}>{reflection.reflection_date}</time>
              <strong>{reflection.status}</strong>
            </div>
            <ReflectionCoverage reflection={reflection} />
            <span>{reflection.notification_status === "sent" ? "Infoflow sent" : reflection.notification_status}</span>
          </summary>
          <div className="daily-reflection-body">
            {reflection.status === "completed" && <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {reflection.report_markdown}
            </ReactMarkdown>}
            {(reflection.status === "queued" || reflection.status === "running") && <div className="reflection-progress">
              <span className="live-status-dot" />
              <strong>{reflection.status === "running" ? "Reflection agent is analyzing the day" : "Reflection is queued"}</strong>
            </div>}
            {reflection.status === "failed" && <div className="reflection-failure">
              <strong>Reflection failed</strong>
              <p>{reflection.error || "The agent did not produce a report."}</p>
              <Button
                icon={<ArrowSync20Regular />}
                disabled={retrying}
                onClick={() => onRetry(reflection.reflection_date)}
              >
                Retry
              </Button>
            </div>}
            <footer>
              <span>Shanghai calendar day · {reflection.reflection_date} 00:00-24:00 (UTC+8)</span>
              <span>{reflection.agent_model || "Configured Codex model"}</span>
              {numberValue(reflection.source_counts.completed_turns) > 0 && <span>{numberValue(reflection.source_counts.completed_turns)} recorded turns</span>}
              {numberValue(reflection.source_counts.external_jobs) > 0 && <span>{numberValue(reflection.source_counts.external_jobs)} registered jobs</span>}
              <span>{reflection.evidence_refs.length} evidence records</span>
            </footer>
          </div>
        </details>
      </article>
    ))}
  </div>;
}

function ReflectionCoverage({ reflection }: { reflection: DailyReflection }) {
  const counts = reflection.source_counts;
  const tasks = taskLabels(counts.work_item_titles, counts.work_items);
  const gaps = numberValue(counts.unassigned_windows);
  return <div className="reflection-task-summary">
    {tasks.length === 0 && <span>{reflection.status === "running" ? "Collecting task activity..." : "No task activity found"}</span>}
    {tasks.slice(0, 3).map((task) => <span key={task.id} title={`${task.id} · ${task.title}`}>
      <code>{task.id}</code>{task.title}
    </span>)}
    {tasks.length > 3 && <em>+{tasks.length - 3} more</em>}
    {gaps > 0 && <em>{gaps} unassigned windows</em>}
  </div>;
}

function taskLabels(details: unknown, fallback: unknown): Array<{ id: string; title: string }> {
  if (Array.isArray(details)) {
    const parsed = details.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const id = "id" in item && typeof item.id === "string" ? item.id : "";
      const title = "title" in item && typeof item.title === "string" ? item.title : "";
      return id && title ? [{ id, title }] : [];
    });
    if (parsed.length) return parsed;
  }
  return Array.isArray(fallback)
    ? fallback.filter((item): item is string => typeof item === "string").map((id) => ({ id, title: id }))
    : [];
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
