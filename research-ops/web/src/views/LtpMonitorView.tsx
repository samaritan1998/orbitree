import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Input, Tooltip } from "@fluentui/react-components";
import {
  Add20Regular,
  ArrowClockwise20Regular,
  CheckmarkCircle16Regular,
  Clock20Regular,
  Warning20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime } from "../time";
import type { LtpMonitoredJob, LtpMonitorOverview } from "../types";

const STATE_LABELS: Record<string, string> = {
  created: "Created",
  pending: "Pending",
  running: "Running",
  failed: "Failed",
  completed: "Completed",
  cancelled: "Cancelled",
  unknown: "Unknown",
};

export default function LtpMonitorView({ embedded = false }: { embedded?: boolean }) {
  const Heading = embedded ? "h2" : "h1";
  const queryClient = useQueryClient();
  const [trackOpen, setTrackOpen] = useState(false);
  const [jobName, setJobName] = useState("");
  const monitor = useQuery({
    queryKey: ["ltp-monitor"],
    queryFn: api.ltpMonitor,
    refetchInterval: 15_000,
    staleTime: 10_000,
    retry: 1,
  });
  const refresh = useMutation({
    mutationFn: api.refreshLtpMonitor,
    onSuccess: (data) => queryClient.setQueryData(["ltp-monitor"], data),
  });
  const track = useMutation({
    mutationFn: () => api.trackLtpJob({ job_name: jobName.trim() }),
    onSuccess: async () => {
      setJobName("");
      setTrackOpen(false);
      await refresh.mutateAsync();
    },
  });
  const counts = useMemo(() => stateCounts(monitor.data), [monitor.data]);

  if (monitor.isLoading) return <MonitorSkeleton />;
  if (monitor.isError || !monitor.data) {
    return (
      <section className="ltp-monitor ltp-monitor-state-page">
        <div className="cluster-state-mark"><Warning20Regular /></div>
        <Heading>LTP Monitor unavailable</Heading>
        <p>{monitor.error?.message ?? "The local LTP monitor is unavailable."}</p>
        <Button icon={<ArrowClockwise20Regular />} onClick={() => monitor.refetch()}>Retry</Button>
      </section>
    );
  }

  const data = monitor.data;
  const jobs = new Map(data.jobs.map((job) => [job.id, job]));
  const notificationsByEvent = new Map(data.notifications.map((item) => [item.event_id, item]));
  const state = monitorState(data);
  return (
    <section className="ltp-monitor">
      <header className="ltp-monitor-header">
        <div>
          <div className="cluster-title-line">
            <Clock20Regular />
            <Heading>{embedded ? "任务监控" : "LTP Monitor"}</Heading>
            <span className={`ltp-monitor-health ${state.tone}`}>
              {state.tone === "healthy" ? <CheckmarkCircle16Regular /> : <Warning20Regular />}
              {state.label}
            </span>
          </div>
          <p>
            ltp-cli / {data.users.join(", ")}
            {data.state?.last_success_at && <> / last success {formatDateTime(data.state.last_success_at, {}, "en-GB")}</>}
          </p>
        </div>
        <div className="cluster-header-actions">
          <span className="cluster-auto-refresh"><Clock20Regular /> Auto refresh 15s</span>
          <Tooltip content="Register a known LTP task by its actual job name" relationship="description">
            <Button
              appearance="subtle"
              icon={<Add20Regular />}
              disabled={!data.enabled}
              onClick={() => setTrackOpen((open) => !open)}
            >
              Track task
            </Button>
          </Tooltip>
          <Tooltip content="Refresh LTP CLI task state now" relationship="description">
            <Button
              appearance="primary"
              icon={<ArrowClockwise20Regular />}
              disabled={refresh.isPending || !data.enabled}
              onClick={() => refresh.mutate()}
            >
              {refresh.isPending ? "Refreshing" : "Refresh"}
            </Button>
          </Tooltip>
        </div>
      </header>

      {trackOpen && (
        <form
          className="ltp-monitor-track-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (jobName.trim()) track.mutate();
          }}
        >
          <Input
            aria-label="Actual LTP job name"
            placeholder="Actual LTP job name"
            value={jobName}
            onChange={(_, value) => setJobName(value.value)}
            disabled={track.isPending}
          />
          <Button appearance="primary" type="submit" disabled={!jobName.trim() || track.isPending}>
            {track.isPending ? "Tracking" : "Track"}
          </Button>
          <Button appearance="subtle" type="button" disabled={track.isPending} onClick={() => setTrackOpen(false)}>
            Cancel
          </Button>
          {track.isError && <span role="status">{track.error.message}</span>}
        </form>
      )}

      <div className="ltp-monitor-metrics" aria-label="LTP task summary">
        <Metric label="Tracked" value={String(data.jobs.length)} detail={`${data.users.length} user${data.users.length === 1 ? "" : "s"}`} />
        <Metric label="Created" value={String(counts.created)} detail="LTP CLI state" tone="created" />
        <Metric label="Pending" value={String(counts.pending)} detail="Waiting for capacity" tone="pending" />
        <Metric label="Running" value={String(counts.running)} detail="Active LTP tasks" tone="running" />
        <Metric label="Failed" value={String(counts.failed)} detail="Requires review" tone="failed" />
      </div>

      {(!data.enabled || !data.notifications_enabled || data.state?.last_error || refresh.isError) && (
        <aside className="ltp-monitor-warning" role="status">
          <Warning20Regular />
          <div>
            {!data.enabled && <p>Monitor is disabled in this deployment.</p>}
            {!data.notifications_enabled && <p>Group delivery is not ready.</p>}
            {data.state?.last_error && <p>LTP CLI: {data.state.last_error}</p>}
            {refresh.isError && <p>{refresh.error.message}</p>}
          </div>
        </aside>
      )}

      <section className="ltp-monitor-jobs" aria-labelledby="ltp-jobs-heading">
        <header className="ltp-monitor-section-head">
          <div>
            <h2 id="ltp-jobs-heading">Tasks</h2>
            <span>{data.jobs.length} tracked</span>
          </div>
          <span className="ltp-monitor-group">Mavin group {data.group_id ?? "not configured"}</span>
        </header>
        {data.jobs.length === 0 ? (
          <div className="ltp-monitor-empty">No LTP tasks recorded yet.</div>
        ) : (
          <div className="ltp-monitor-table-scroll">
            <table className="ltp-monitor-table">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>State</th>
                  <th>Cluster</th>
                  <th>LTP status</th>
                  <th>Last seen</th>
                  <th>Probe</th>
                </tr>
              </thead>
              <tbody>
                {data.jobs.map((job) => <JobRow key={job.id} job={job} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="ltp-monitor-events" aria-labelledby="ltp-events-heading">
        <header className="ltp-monitor-section-head">
          <div>
            <h2 id="ltp-events-heading">Status history</h2>
            <span>{data.events.length} recent events</span>
          </div>
        </header>
        {data.events.length === 0 ? (
          <div className="ltp-monitor-empty">No status changes recorded yet.</div>
        ) : (
          <div className="ltp-event-list">
            {data.events.map((event) => {
              const job = jobs.get(event.job_id);
              const notification = notificationsByEvent.get(event.id);
              return (
                <article className="ltp-event-row" key={event.id}>
                  <div className="ltp-event-state">
                    <span className={`ltp-state ${event.state}`}>{stateLabel(event.state)}</span>
                    <small>{event.previous_state ? `${stateLabel(event.previous_state)} → ${stateLabel(event.state)}` : "First observed"}</small>
                  </div>
                  <div className="ltp-event-task">
                    <strong title={job?.job_name}>{job?.job_name ?? "Removed task"}</strong>
                    <span>{job?.cluster ?? "Unknown cluster"} / {event.source}</span>
                  </div>
                  <div className="ltp-event-raw">{event.provider_status ?? "No provider status"}</div>
                  <div className="ltp-event-notification">
                    <strong>{notification?.status === "sent" ? "Sent" : notification?.status ?? "Not queued"}</strong>
                    <span>{notification?.last_error ?? notification?.target ?? ""}</span>
                  </div>
                  <time dateTime={event.observed_at}>{formatDateTime(event.observed_at, {}, "en-GB")}</time>
                </article>
              );
            })}
          </div>
        )}
      </section>
    </section>
  );
}


function JobRow({ job }: { job: LtpMonitoredJob }) {
  return (
    <tr>
      <td className="ltp-job-name">
        <strong title={job.job_name}>{job.job_name}</strong>
        <span>{job.user}</span>
      </td>
      <td><span className={`ltp-state ${job.state}`}>{stateLabel(job.state)}</span></td>
      <td>{job.cluster ?? "Unknown"}</td>
      <td>{job.provider_status ?? "Unknown"}</td>
      <td>{formatDateTime(job.last_observed_at, {}, "en-GB")}</td>
      <td>{job.last_probe_at ? formatDateTime(job.last_probe_at, { hour: "2-digit", minute: "2-digit" }, "en-GB") : "-"}</td>
    </tr>
  );
}

function Metric({ label, value, detail, tone }: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
}) {
  return (
    <div className={`ltp-monitor-metric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function stateCounts(data: LtpMonitorOverview | undefined): Record<string, number> {
  const counts: Record<string, number> = { created: 0, pending: 0, running: 0, failed: 0 };
  for (const job of data?.jobs ?? []) counts[job.state] = (counts[job.state] ?? 0) + 1;
  return counts;
}

function monitorState(data: LtpMonitorOverview): { label: string; tone: "healthy" | "warning" | "disabled" } {
  if (!data.enabled) return { label: "Disabled", tone: "disabled" };
  if (data.state?.last_error) return { label: "Degraded", tone: "warning" };
  return { label: "Healthy", tone: "healthy" };
}

function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state;
}

function MonitorSkeleton() {
  return (
    <section className="ltp-monitor" aria-busy="true" aria-label="Loading LTP monitor">
      <div className="cluster-skeleton-header" />
      <div className="cluster-skeleton-metrics">
        {Array.from({ length: 5 }, (_, index) => <span key={index} />)}
      </div>
      <div className="cluster-skeleton-section"><span /><span /><span /></div>
    </section>
  );
}
