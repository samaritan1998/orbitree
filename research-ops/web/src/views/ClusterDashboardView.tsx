import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Input, Select, Tooltip } from "@fluentui/react-components";
import {
  ArrowClockwise20Regular,
  CheckmarkCircle16Regular,
  Clock20Regular,
  Search20Regular,
  ServerMultiple20Regular,
  Warning20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime, timestampMillis } from "../time";
import type { ClusterJob, ClusterOverview, ClusterQueue } from "../types";

type StatusFilter = "all" | "running" | "queued";

const CATEGORY_LABELS: Record<string, string> = {
  large_training: "Large training",
  small_training: "Small training",
  conversion: "Checkpoint conversion",
  inference: "Inference",
  unknown: "Unclassified",
};

export default function ClusterDashboardView({ embedded = false }: { embedded?: boolean }) {
  const Heading = embedded ? "h2" : "h1";
  const queryClient = useQueryClient();
  const overview = useQuery({
    queryKey: ["cluster-overview"],
    queryFn: () => api.clusterOverview(),
    refetchInterval: 30_000,
    staleTime: 25_000,
    retry: 1,
  });
  const refresh = useMutation({
    mutationFn: () => api.clusterOverview(true),
    onSuccess: (data) => queryClient.setQueryData(["cluster-overview"], data),
  });
  const [status, setStatus] = useState<StatusFilter>("all");
  const [cluster, setCluster] = useState("all");
  const [category, setCategory] = useState("all");
  const [search, setSearch] = useState("");
  const now = useSecondClock();

  const filtered = useMemo(
    () => filterClusters(overview.data, { status, cluster, category, search }),
    [overview.data, status, cluster, category, search],
  );
  const visibleJobs = filtered.reduce(
    (count, item) => count + item.running_jobs.length + item.queued_jobs.length,
    0,
  );

  if (overview.isLoading) return <ClusterDashboardSkeleton />;
  if (overview.isError || !overview.data) {
    return (
      <section className="cluster-dashboard cluster-state-page">
        <div className="cluster-state-mark"><ServerMultiple20Regular /></div>
        <Heading>Cluster data unavailable</Heading>
        <p>{overview.error?.message ?? "The read-only LTP source is not configured."}</p>
        <Button icon={<ArrowClockwise20Regular />} onClick={() => overview.refetch()}>
          Retry
        </Button>
      </section>
    );
  }

  const data = overview.data;
  return (
    <section className="cluster-dashboard">
      <header className="cluster-dashboard-header">
        <div>
          <div className="cluster-title-line">
            <ServerMultiple20Regular />
            <Heading>{embedded ? "集群资源" : "Cluster Control"}</Heading>
            <SourceState overview={data} />
          </div>
          <p>
            {data.coverage.successful_users}/{data.coverage.requested_users} users observed
            <span aria-hidden="true"> / </span>
            jobs at {formatDateTime(data.jobs_observed_at, {}, "en-GB")}
          </p>
        </div>
        <div className="cluster-header-actions">
          <span className="cluster-auto-refresh"><Clock20Regular /> Auto refresh 30s</span>
          <Tooltip content="Refresh live inventory now" relationship="description">
            <Button
              appearance="primary"
              icon={<ArrowClockwise20Regular />}
              disabled={refresh.isPending}
              onClick={() => refresh.mutate()}
            >
              {refresh.isPending ? "Refreshing" : "Refresh"}
            </Button>
          </Tooltip>
        </div>
      </header>

      <div className="cluster-metrics" aria-label="Cluster summary">
        <Metric label="Free nodes" value={formatNumber(data.summary.free_nodes)} detail={`${formatNumber(data.summary.available_cards)} cards across ${data.summary.resource_domains} domains`} />
        <Metric label="Running" value={formatNumber(data.summary.running_jobs)} detail={`${formatNumber(data.summary.running_nodes)} requested nodes`} tone="running" />
        <Metric label="Queued" value={formatNumber(data.summary.queued_jobs)} detail={`${formatNumber(data.summary.queued_nodes)} requested nodes`} tone="queued" />
        <Metric label="ETA coverage" value={coverageText(data.summary.estimated_jobs, data.summary.running_jobs + data.summary.queued_jobs)} detail={`${data.summary.profiled_jobs} jobs explicitly profiled`} />
        <Metric label="Checkpoint coverage" value={coverageText(data.summary.checkpoint_interval_jobs, trainingJobCount(data))} detail="Training jobs with an interval" />
      </div>

      {(data.warnings.length > 0 || refresh.isError) && (
        <aside className="cluster-warning" role="status">
          <Warning20Regular />
          <div>
            {refresh.isError && <p>{refresh.error.message}</p>}
            {data.coverage.failed_user_names.length > 0 && (
              <p>User coverage gap: {data.coverage.failed_user_names.join(", ")}</p>
            )}
            {data.warnings.map((warning) => <p key={warning}>{warning}</p>)}
          </div>
        </aside>
      )}

      <div className="cluster-toolbar">
        <div className="status-tabs" role="tablist" aria-label="Job status">
          <StatusButton label="All" count={data.summary.running_jobs + data.summary.queued_jobs} value="all" selected={status} onChange={setStatus} />
          <StatusButton label="Running" count={data.summary.running_jobs} value="running" selected={status} onChange={setStatus} />
          <StatusButton label="Queued" count={data.summary.queued_jobs} value="queued" selected={status} onChange={setStatus} />
        </div>
        <div className="cluster-filters">
          <Input
            aria-label="Search jobs or users"
            contentBefore={<Search20Regular />}
            placeholder="Search job or user"
            value={search}
            onChange={(_, value) => setSearch(value.value)}
          />
          <Select aria-label="Filter by cluster" value={cluster} onChange={(_, value) => setCluster(value.value)}>
            <option value="all">All clusters</option>
            {data.clusters.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
          </Select>
          <Select aria-label="Filter by category" value={category} onChange={(_, value) => setCategory(value.value)}>
            <option value="all">All categories</option>
            {Object.keys(data.categories).sort().map((value) => (
              <option key={value} value={value}>{categoryLabel(value)}</option>
            ))}
          </Select>
        </div>
        <span className="cluster-result-count">{visibleJobs} jobs in {filtered.length} clusters</span>
      </div>

      {filtered.length === 0 ? (
        <div className="cluster-empty">
          <strong>No jobs match these filters</strong>
          <span>Clear the search or select a different cluster, status, or category.</span>
          <Button onClick={() => {
            setStatus("all");
            setCluster("all");
            setCategory("all");
            setSearch("");
          }}>Clear filters</Button>
        </div>
      ) : (
        <div className="cluster-list">
          {filtered.map((item) => (
            <ClusterSection key={item.name} cluster={item} status={status} now={now} />
          ))}
        </div>
      )}
    </section>
  );
}

function SourceState({ overview }: { overview: ClusterOverview }) {
  const state = overview.source.stale
    ? "Stale"
    : overview.source.degraded
      ? "Partial"
      : overview.source.kind === "live"
        ? "Live"
        : "Snapshot";
  return (
    <span className={`cluster-source-state ${state.toLowerCase()}`}>
      {state === "Live" ? <CheckmarkCircle16Regular /> : <Warning20Regular />}
      {state}
    </span>
  );
}

function Metric({ label, value, detail, tone }: {
  label: string;
  value: string;
  detail: string;
  tone?: "running" | "queued";
}) {
  return (
    <div className={`cluster-metric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function StatusButton({ label, count, value, selected, onChange }: {
  label: string;
  count: number;
  value: StatusFilter;
  selected: StatusFilter;
  onChange: (value: StatusFilter) => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={selected === value}
      className={selected === value ? "active" : ""}
      onClick={() => onChange(value)}
    >
      {label}<span>{count}</span>
    </button>
  );
}

function ClusterSection({ cluster, status, now }: {
  cluster: ClusterQueue;
  status: StatusFilter;
  now: number;
}) {
  const showRunning = status !== "queued" && cluster.running_jobs.length > 0;
  const showQueued = status !== "running" && cluster.queued_jobs.length > 0;
  return (
    <section className="cluster-section">
      <header className="cluster-section-header">
        <div className="cluster-identity">
          <h2>{cluster.name}</h2>
          {cluster.resource ? (
            <span>{cluster.resource.hardware} cards / {cluster.resource.scheduling_class}</span>
          ) : (
            <span>Tidal capacity not mapped</span>
          )}
        </div>
        <ResourceFacts cluster={cluster} />
        <div className="cluster-queue-facts">
          <span><strong>{cluster.running_jobs.length}</strong> running / {cluster.running_nodes} nodes</span>
          <span><strong>{cluster.queued_jobs.length}</strong> queued / {cluster.queued_nodes} nodes</span>
        </div>
      </header>
      {showRunning && <JobTable label="Running" jobs={cluster.running_jobs} now={now} />}
      {showQueued && <JobTable label="Queued" jobs={cluster.queued_jobs} now={now} />}
      {!showRunning && !showQueued && (
        <div className="cluster-section-empty">No jobs in the selected state.</div>
      )}
    </section>
  );
}

function ResourceFacts({ cluster }: { cluster: ClusterQueue }) {
  const resource = cluster.resource;
  if (!resource) {
    return <div className="cluster-resource-unavailable">Resource telemetry unavailable</div>;
  }
  const stability = resource.forecast_samples > 1
    ? `${formatDuration(resource.stable_window_seconds)} stable`
    : "Forecast warming up";
  return (
    <div className="cluster-resource-facts">
      <Tooltip content={resource.available_formula ?? "Reported by Tidal"} relationship="description">
        <span><strong>{formatNumber(resource.free_nodes)}</strong> free nodes</span>
      </Tooltip>
      <span><strong>{formatNumber(resource.available_cards)}</strong> available cards</span>
      <span><strong>{formatPercent(resource.utilization_pct)}</strong> utilized</span>
      <span><strong>{formatPercent(resource.volatility * 100)}</strong> volatility</span>
      <span>{stability}</span>
      {resource.fragment_cards > 0 && <span>{formatNumber(resource.fragment_cards)} fragment cards</span>}
    </div>
  );
}

function JobTable({ label, jobs, now }: { label: "Running" | "Queued"; jobs: ClusterJob[]; now: number }) {
  return (
    <section className={`cluster-job-group ${label.toLowerCase()}`}>
      <header>
        <h3>{label}</h3>
        <span>{jobs.length}</span>
      </header>
      <div className="cluster-table-scroll">
        <table className="cluster-job-table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Status</th>
              <th>Nodes</th>
              <th>ETA / ready</th>
              <th>Checkpoint interval</th>
              <th>Next checkpoint</th>
              <th>Submitted</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => <JobRow key={job.id} job={job} now={now} />)}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function JobRow({ job, now }: { job: ClusterJob; now: number }) {
  const checkpointApplicability = isTraining(job.category)
    || job.checkpoint_interval_seconds != null
    || job.last_checkpoint_at != null
    || job.next_checkpoint_at != null
    ? "training"
    : job.category === "unknown"
      ? "unknown"
      : "not_applicable";
  return (
    <tr>
      <td className="cluster-task-cell">
        <strong title={job.name}>{job.name}</strong>
        <span>{job.user} / {categoryLabel(job.category)}</span>
      </td>
      <td>
        <span className={`cluster-job-state ${job.state}`}>{job.provider_status}</span>
      </td>
      <td className="cluster-number-cell">
        <strong>{job.nodes ?? "?"}</strong>
        <span>{job.nodes === 1 ? "node" : "nodes"}</span>
      </td>
      <td><EstimateCell job={job} /></td>
      <td><CheckpointInterval job={job} applicability={checkpointApplicability} /></td>
      <td><NextCheckpoint job={job} applicability={checkpointApplicability} now={now} /></td>
      <td className="cluster-submitted-cell">
        <strong>{job.age ?? "Unknown"}</strong>
        <span title={job.submission_time ?? undefined}>{job.submission_time ?? "No timestamp"}</span>
      </td>
    </tr>
  );
}

function EstimateCell({ job }: { job: ClusterJob }) {
  if (job.eta_seconds == null) {
    return <UnavailableValue label="Awaiting profile" detail={job.blocked_reason ?? undefined} />;
  }
  const confidence = job.estimate_confidence == null
    ? "Confidence pending"
    : `${Math.round(job.estimate_confidence * 100)}% confidence`;
  return (
    <span className="cluster-value-stack">
      <strong>{job.eta_kind === "ready" ? "Ready " : ""}{formatDuration(job.eta_seconds)}</strong>
      <small>{confidence}</small>
    </span>
  );
}

function CheckpointInterval({ job, applicability }: {
  job: ClusterJob;
  applicability: "training" | "unknown" | "not_applicable";
}) {
  if (applicability === "not_applicable") return <UnavailableValue label="Not applicable" />;
  if (applicability === "unknown") return <UnavailableValue label="Type unknown" />;
  if (job.checkpoint_interval_seconds == null) {
    return <UnavailableValue label="Not instrumented" />;
  }
  return (
    <span className="cluster-value-stack">
      <strong>{formatDuration(job.checkpoint_interval_seconds)}</strong>
      <small>{job.checkpoint_source ?? "Policy"}</small>
    </span>
  );
}

function NextCheckpoint({ job, applicability, now }: {
  job: ClusterJob;
  applicability: "training" | "unknown" | "not_applicable";
  now: number;
}) {
  if (applicability === "not_applicable") return <UnavailableValue label="Not applicable" />;
  if (applicability === "unknown") return <UnavailableValue label="Type unknown" />;
  if (job.next_checkpoint_at) {
    const remaining = Math.max(0, timestampMillis(job.next_checkpoint_at) - now) / 1000;
    return (
      <span className="cluster-value-stack">
        <strong>{remaining <= 0 ? "Due now" : `in ${formatDuration(remaining)}`}</strong>
        <small>{formatDateTime(job.next_checkpoint_at, { hour: "2-digit", minute: "2-digit", second: "2-digit" }, "en-GB")}</small>
      </span>
    );
  }
  if (job.checkpoint_interval_seconds != null) {
    return <UnavailableValue label="Receipt missing" />;
  }
  return <UnavailableValue label="Not instrumented" />;
}

function UnavailableValue({ label, detail }: { label: string; detail?: string }) {
  return (
    <Tooltip content={detail ?? label} relationship="description">
      <span className="cluster-unavailable">{label}</span>
    </Tooltip>
  );
}

function ClusterDashboardSkeleton() {
  return (
    <section className="cluster-dashboard" aria-busy="true" aria-label="Loading cluster control">
      <div className="cluster-skeleton-header" />
      <div className="cluster-skeleton-metrics">
        {Array.from({ length: 5 }, (_, index) => <span key={index} />)}
      </div>
      <div className="cluster-skeleton-toolbar" />
      {Array.from({ length: 3 }, (_, index) => (
        <div className="cluster-skeleton-section" key={index}>
          <span />
          <span />
          <span />
        </div>
      ))}
    </section>
  );
}

export function filterClusters(
  overview: ClusterOverview | undefined,
  filters: { status: StatusFilter; cluster: string; category: string; search: string },
): ClusterQueue[] {
  if (!overview) return [];
  const needle = filters.search.trim().toLocaleLowerCase();
  return overview.clusters.flatMap((item) => {
    if (filters.cluster !== "all" && item.name !== filters.cluster) return [];
    const select = (jobs: ClusterJob[]) => jobs.filter((job) => {
      if (filters.category !== "all" && job.category !== filters.category) return false;
      if (!needle) return true;
      return `${job.name} ${job.user}`.toLocaleLowerCase().includes(needle);
    });
    const running = filters.status === "queued" ? [] : select(item.running_jobs);
    const queued = filters.status === "running" ? [] : select(item.queued_jobs);
    const filtersActive = Boolean(needle) || filters.category !== "all";
    if (!running.length && !queued.length && (filtersActive || !item.resource)) return [];
    return [{ ...item, running_jobs: running, queued_jobs: queued }];
  });
}

function useSecondClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

function trainingJobCount(overview: ClusterOverview): number {
  return Object.entries(overview.categories).reduce(
    (count, [category, values]) => isTraining(category) ? count + values.running + values.queued : count,
    0,
  );
}

function isTraining(category: string): boolean {
  return category === "large_training" || category === "small_training";
}

function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? category.replaceAll("_", " ");
}

export function formatDuration(seconds: number): string {
  const value = Math.max(0, Math.round(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) {
    const hours = Math.floor(value / 3600);
    const minutes = Math.round((value % 3600) / 60);
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  const days = Math.floor(value / 86400);
  const hours = Math.round((value % 86400) / 3600);
  return hours ? `${days}d ${hours}h` : `${days}d`;
}

function coverageText(covered: number, total: number): string {
  if (total <= 0) return "N/A";
  return `${covered}/${total}`;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(value);
}

function formatPercent(value: number): string {
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(value)}%`;
}
