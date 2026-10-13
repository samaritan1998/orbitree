import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Add20Regular, ArrowRight20Regular, BranchFork20Regular, Clock20Regular, Play20Regular } from "@fluentui/react-icons";
import { Button, Skeleton, SkeletonItem, Tooltip } from "@fluentui/react-components";
import { api } from "../api";
import { operationalStatus } from "../status";
import type { WorkItem } from "../types";

export default function TodayView({ onOpenWork, onQuickCreated, scratchProgram = "FREE" }: {
  scratchProgram?: string;
  onOpenWork: (work: WorkItem) => void;
  onQuickCreated: (work: WorkItem) => void;
}) {
  const queryClient = useQueryClient();
  const today = useQuery({ queryKey: ["today"], queryFn: api.today, refetchInterval: 15_000 });
  const quickStart = useMutation({
    mutationFn: () => api.quickStart(scratchProgram),
    onSuccess: async (work) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["today"] }),
        queryClient.invalidateQueries({ queryKey: ["tree", scratchProgram] }),
      ]);
      onQuickCreated(work);
    },
  });

  if (today.isLoading) {
    return <div className="page-loading"><Skeleton><SkeletonItem /><SkeletonItem /><SkeletonItem /></Skeleton></div>;
  }
  if (today.error || !today.data) {
    return <div className="page-error">{today.error?.message ?? "Today is unavailable"}</div>;
  }

  const data = today.data;
  return (
    <div className="page today-page">
      <header className="page-header">
        <div>
          <h1>Today</h1>
          <p>Active work, decisions, and external runs.</p>
        </div>
        <div className="today-header-actions">
          <Tooltip content={`Immediately creates an undefined WorkItem in ${scratchProgram}`} relationship="description">
            <Button
              appearance="primary"
              icon={<Add20Regular />}
              disabled={quickStart.isPending || !scratchProgram}
              aria-describedby="scratch-work-description"
              onClick={() => quickStart.mutate()}
            >
              Create scratch work
            </Button>
          </Tooltip>
          <span id="scratch-work-description" className="sr-only">
            Immediately creates an undefined WorkItem in {scratchProgram}. You can undo it after creation.
          </span>
          <div className="header-metrics">
            <span><strong>{data.active_sessions}</strong> active sessions</span>
            <span><strong>{data.pending_merges}</strong> merge reviews</span>
          </div>
        </div>
      </header>
      {quickStart.error && <div className="inline-error">{quickStart.error.message}</div>}

      <section className="today-band primary-band">
        <div className="band-heading"><Play20Regular /><h2>Continue</h2><span>{data.continue_items.length}</span></div>
        <WorkRows items={data.continue_items} empty="No active work" onOpen={onOpenWork} />
      </section>

      <div className="today-columns">
        <section className="today-band">
          <div className="band-heading"><BranchFork20Regular /><h2>Needs decision</h2><span>{data.needs_decision.length}</span></div>
          <WorkRows items={data.needs_decision} empty="No pending decisions" onOpen={onOpenWork} compact />
        </section>
        <section className="today-band">
          <div className="band-heading"><Clock20Regular /><h2>Waiting external</h2><span>{data.waiting_external.length}</span></div>
          {data.waiting_external.length ? data.waiting_external.map((job) => (
            <div className="job-row" key={job.id}>
              <div><strong>{job.provider}</strong><span>{job.remote_job_id}</span></div>
              <span className={`status-text status-${job.status}`}>{job.status}</span>
            </div>
          )) : <div className="empty-row">No external jobs</div>}
        </section>
      </div>

      <section className="today-band">
        <div className="band-heading"><ArrowRight20Regular /><h2>Ready next</h2><span>{data.suggested_next.length}</span></div>
        <WorkRows items={data.suggested_next} empty="No ready work" onOpen={onOpenWork} />
      </section>
    </div>
  );
}

function WorkRows({ items, empty, onOpen, compact = false }: {
  items: WorkItem[];
  empty: string;
  onOpen: (work: WorkItem) => void;
  compact?: boolean;
}) {
  if (!items.length) return <div className="empty-row">{empty}</div>;
  return <div className={compact ? "work-rows compact" : "work-rows"}>{items.map((item) => (
    <button className="work-row" key={item.id} onClick={() => onOpen(item)} aria-label={`Open ${item.human_id}: ${item.title}`}>
      <span className="work-id">{item.human_id}</span>
      <span className="work-title">{item.title}</span>
      <span className="work-flow">{operationalStatus({ work: item })}</span>
      <ArrowRight20Regular aria-hidden="true" />
    </button>
  ))}</div>;
}
