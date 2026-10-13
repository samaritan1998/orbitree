import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  ProgressBar,
  Select,
  Spinner,
  Tooltip,
} from "@fluentui/react-components";
import {
  Add20Regular,
  ArrowClockwise20Regular,
  ArrowSync20Regular,
  Beaker20Regular,
  CheckmarkCircle20Regular,
  Pause20Regular,
  Play20Regular,
  Warning20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime } from "../time";
import type { EvalRunOverview } from "../types";

type RunAction = "start" | "pause" | "requeue" | "import";

export default function EvalHarnessView() {
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["eval-runs"],
    queryFn: api.evalRuns,
    refetchInterval: 3_000,
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string>();
  const [workId, setWorkId] = useState("");
  const [name, setName] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [taskFile, setTaskFile] = useState("");
  const [resultRoot, setResultRoot] = useState("");
  const [mode, setMode] = useState<"canonical" | "fast">("fast");
  const [toolParser, setToolParser] = useState(true);
  const [batchSize, setBatchSize] = useState(16);
  const [maxAttempts, setMaxAttempts] = useState(3);

  useEffect(() => {
    if (!selectedRunId && runs.data?.length) setSelectedRunId(runs.data[0].run.id);
  }, [runs.data, selectedRunId]);

  const selected = runs.data?.find((item) => item.run.id === selectedRunId);
  const audit = useQuery({
    queryKey: ["eval-audit", selectedRunId],
    queryFn: () => api.auditEvalRun(selectedRunId!),
    enabled: Boolean(selectedRunId),
    refetchInterval: selected?.run.status === "running" ? 3_000 : false,
  });
  const isolatedUnits = useQuery({
    queryKey: ["eval-isolated-units", selectedRunId],
    queryFn: () => api.evalUnits(selectedRunId!, { status: "quarantined", limit: 20 }),
    enabled: Boolean(selectedRunId && selected?.summary.quarantined_units),
  });
  const metrics = useMemo(() => {
    const entries = runs.data ?? [];
    return {
      active: entries.filter((item) => item.run.status === "running").length,
      workers: entries.reduce((sum, item) => sum + item.summary.active_workers, 0),
      quarantined: entries.reduce((sum, item) => sum + item.summary.quarantined_units, 0),
    };
  }, [runs.data]);

  const resetCreate = () => {
    setName("");
    setCandidateId("");
    setTaskFile("");
    setResultRoot("");
    setMode("fast");
    setToolParser(true);
    setBatchSize(16);
    setMaxAttempts(3);
  };

  const create = useMutation({
    mutationFn: async () => {
      const run = await api.createEvalRun({
        work_item_id: workId.trim(),
        name: name.trim(),
        adapter: "presentbench",
        mode,
        candidate_id: candidateId.trim(),
        source_uri: taskFile.trim(),
        artifact_uri: resultRoot.trim(),
        tool_parser: toolParser,
        batch_size: batchSize,
        max_attempts: maxAttempts,
        config_json: { judge_protocol: "presentbench-rubric-v1" },
      });
      await api.importPresentBench(run.id, {
        task_file: taskFile.trim(),
        result_root: resultRoot.trim(),
        auto_start: true,
      });
      return run;
    },
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ["eval-runs"] });
      setSelectedRunId(run.id);
      setCreateOpen(false);
      resetCreate();
    },
  });

  const action = useMutation({
    mutationFn: ({ item, action: nextAction }: { item: EvalRunOverview; action: RunAction }) => {
      if (nextAction === "start") return api.startEvalRun(item.run.id);
      if (nextAction === "pause") return api.pauseEvalRun(item.run.id);
      if (nextAction === "requeue") return api.requeueEvalRun(item.run.id);
      if (!item.run.source_uri || !item.run.artifact_uri) {
        throw new Error("Run has no saved task file or result root");
      }
      return api.importPresentBench(item.run.id, {
        task_file: item.run.source_uri,
        result_root: item.run.artifact_uri,
        auto_start: true,
      });
    },
    onSuccess: async (_data, variables) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["eval-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["eval-audit", variables.item.run.id] }),
        queryClient.invalidateQueries({ queryKey: ["eval-isolated-units", variables.item.run.id] }),
      ]);
    },
  });

  return <div className="page eval-page">
    <header className="page-header eval-header">
      <div><h1>Eval Harness</h1><p>PresentBench and agent rubric runs</p></div>
      <div className="eval-header-actions">
        <div className="header-metrics eval-metrics">
          <div><strong>{metrics.active}</strong>Active runs</div>
          <div><strong>{metrics.workers}</strong>Workers</div>
          <div><strong>{metrics.quarantined}</strong>Quarantined</div>
        </div>
        <Tooltip content="Refresh runs" relationship="description">
          <Button
            appearance="subtle"
            icon={<ArrowClockwise20Regular />}
            aria-label="Refresh eval runs"
            onClick={() => runs.refetch()}
          />
        </Tooltip>
        <Button appearance="primary" icon={<Add20Regular />} onClick={() => setCreateOpen(true)}>
          New run
        </Button>
      </div>
    </header>

    {runs.isLoading && <div className="page-loading"><Spinner label="Loading eval runs" /></div>}
    {runs.error && <div className="page-error">{runs.error.message}</div>}
    {action.error && <div className="inline-error">{action.error.message}</div>}
    {!runs.isLoading && !runs.data?.length && <div className="empty-state">
      <Beaker20Regular />
      <strong>No eval runs</strong>
      <span>Create a run from a task manifest and candidate result root.</span>
    </div>}

    {!!runs.data?.length && <div className="eval-run-table">
      <div className="eval-run-table-head">
        <span>Run</span><span>Configuration</span><span>Progress</span><span>Rate</span><span>State</span><span />
      </div>
      {runs.data.map((item) => <EvalRunRow
        key={item.run.id}
        item={item}
        selected={item.run.id === selectedRunId}
        busy={action.isPending && action.variables?.item.run.id === item.run.id}
        onSelect={() => setSelectedRunId(item.run.id)}
        onAction={(nextAction) => action.mutate({ item, action: nextAction })}
      />)}
    </div>}

    {selected && <section className="eval-audit-band" aria-label="Selected eval audit">
      <header>
        <div>
          {audit.data?.valid ? <CheckmarkCircle20Regular /> : <Warning20Regular />}
          <div><strong>Audit</strong><code>{selected.run.id}</code></div>
        </div>
        <span className={audit.data?.valid ? "audit-valid" : "audit-warning"}>
          {audit.isLoading ? "Checking" : audit.data?.valid ? "Valid" : "Needs review"}
        </span>
      </header>
      <dl>
        <div><dt>Cases</dt><dd>{selected.summary.total_cases}</dd></div>
        <div><dt>Official score</dt><dd>{formatScore(selected.summary.aggregate_score)}</dd></div>
        <div><dt>Unit mean</dt><dd>{formatScore(selected.summary.mean_score)}</dd></div>
        <div><dt>Attempts</dt><dd>{selected.summary.attempts.toLocaleString()}</dd></div>
        <div><dt>Cache hits</dt><dd>{selected.summary.cache_hits.toLocaleString()}</dd></div>
        <div><dt>Config</dt><dd><code>{selected.run.config_hash.slice(0, 12)}</code></dd></div>
        <div><dt>Updated</dt><dd>{formatDateTime(selected.run.updated_at)}</dd></div>
      </dl>
      {!!audit.data?.issues.length && <div className="eval-audit-issues">
        {audit.data.issues.map((issue, index) => <div key={`${String(issue.code)}-${index}`}>
          <strong>{String(issue.code ?? "audit_issue").replaceAll("_", " ")}</strong>
          <span>{formatIssue(issue)}</span>
        </div>)}
      </div>}
      {!!isolatedUnits.data?.length && <div className="eval-isolated-list">
        <div className="eval-isolated-head"><span>Case</span><span>Rubric</span><span>Error</span><span>Attempts</span></div>
        {isolatedUnits.data.map((unit) => <div className="eval-isolated-row" key={unit.id}>
          <code title={unit.case_key}>{unit.case_key}</code>
          <span>{unit.rubric_key}</span>
          <div><strong>{unit.error_category ?? "unknown"}</strong><small>{unit.error_message}</small></div>
          <span>{unit.attempt_count} / {unit.max_attempts}</span>
        </div>)}
      </div>}
    </section>}

    <Dialog open={createOpen} onOpenChange={(_, data) => setCreateOpen(data.open)}>
      <DialogSurface><DialogBody><DialogTitle>New PresentBench run</DialogTitle>
        <DialogContent className="dialog-form eval-create-form">
          <div className="eval-form-grid">
            <Field label="Work item" required><Input value={workId} placeholder="HARN-0004" onChange={(_, data) => setWorkId(data.value)} /></Field>
            <Field label="Run name" required><Input value={name} onChange={(_, data) => setName(data.value)} /></Field>
          </div>
          <Field label="Candidate ID" required><Input value={candidateId} onChange={(_, data) => setCandidateId(data.value)} /></Field>
          <Field label="Task JSONL" required><Input value={taskFile} onChange={(_, data) => setTaskFile(data.value)} /></Field>
          <Field label="Result root" required><Input value={resultRoot} onChange={(_, data) => setResultRoot(data.value)} /></Field>
          <div className="eval-form-grid three">
            <Field label="Mode"><Select value={mode} onChange={(_, data) => setMode(data.value as "canonical" | "fast")}>
              <option value="canonical">Canonical</option><option value="fast">Fast batch</option>
            </Select></Field>
            <Field label="Batch size"><Input type="number" min={1} max={128} value={String(batchSize)} onChange={(_, data) => setBatchSize(Number(data.value) || 1)} /></Field>
            <Field label="Max attempts"><Input type="number" min={1} max={10} value={String(maxAttempts)} onChange={(_, data) => setMaxAttempts(Number(data.value) || 1)} /></Field>
          </div>
          <Checkbox checked={toolParser} onChange={(_, data) => setToolParser(data.checked === true)} label="Tool parser" />
          {create.error && <div className="inline-error">{create.error.message}</div>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          <Button
            appearance="primary"
            disabled={!workId.trim() || !name.trim() || !candidateId.trim() || !taskFile.trim() || !resultRoot.trim() || create.isPending}
            onClick={() => create.mutate()}
          >
            Create and start
          </Button>
        </DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>
  </div>;
}

function EvalRunRow({
  item,
  selected,
  busy,
  onSelect,
  onAction,
}: {
  item: EvalRunOverview;
  selected: boolean;
  busy: boolean;
  onSelect: () => void;
  onAction: (action: RunAction) => void;
}) {
  const { run, summary } = item;
  const pending = (summary.status_counts.pending ?? 0) + (summary.status_counts.retryable ?? 0);
  return <article className={selected ? "eval-run-row selected" : "eval-run-row"}>
    <button className="eval-run-identity" onClick={onSelect}>
      <strong>{run.name}</strong><span>{run.candidate_id}</span><code>{run.id.slice(0, 8)}</code>
    </button>
    <div className="eval-run-config">
      <strong>{run.mode}</strong><span>{run.tool_parser ? "parser on" : "parser off"}</span><small>batch {run.batch_size}</small>
    </div>
    <div className="eval-run-progress">
      <div><strong>{summary.completed_units.toLocaleString()} / {summary.expected_units.toLocaleString()}</strong><span>{(summary.completion_ratio * 100).toFixed(1)}%</span></div>
      <ProgressBar value={summary.completion_ratio} max={1} />
      <small>{pending.toLocaleString()} pending · {summary.quarantined_units.toLocaleString()} isolated</small>
    </div>
    <div className="eval-run-rate">
      <strong>{summary.throughput_units_per_minute?.toFixed(1) ?? "—"}</strong>
      <span>units/min</span><small>{formatEta(summary.eta_seconds)}</small>
    </div>
    <div className={`eval-run-state state-${run.status}`}>
      <strong>{run.status.replaceAll("_", " ")}</strong><span>{summary.active_workers} workers</span>
    </div>
    <div className="eval-run-actions">
      {run.status === "running" && <Tooltip content="Pause run" relationship="description"><Button disabled={busy} appearance="subtle" icon={<Pause20Regular />} aria-label="Pause eval run" onClick={() => onAction("pause")} /></Tooltip>}
      {["draft", "paused"].includes(run.status) && run.expected_units > 0 && <Tooltip content="Start run" relationship="description"><Button disabled={busy} appearance="subtle" icon={<Play20Regular />} aria-label="Start eval run" onClick={() => onAction("start")} /></Tooltip>}
      {run.status === "draft" && run.expected_units === 0 && run.source_uri && run.artifact_uri && <Tooltip content="Retry manifest import" relationship="description"><Button disabled={busy} appearance="subtle" icon={<ArrowSync20Regular />} aria-label="Retry eval import" onClick={() => onAction("import")} /></Tooltip>}
      {summary.quarantined_units > 0 && <Tooltip content="Requeue isolated units" relationship="description"><Button disabled={busy} appearance="subtle" icon={<ArrowClockwise20Regular />} aria-label="Requeue isolated eval units" onClick={() => onAction("requeue")} /></Tooltip>}
    </div>
  </article>;
}

function formatScore(value?: number): string {
  return value === undefined || value === null ? "—" : value.toFixed(4);
}

function formatEta(seconds?: number): string {
  if (seconds === undefined || seconds === null) return "ETA —";
  if (seconds < 60) return `ETA ${seconds}s`;
  if (seconds < 3600) return `ETA ${Math.ceil(seconds / 60)}m`;
  return `ETA ${(seconds / 3600).toFixed(1)}h`;
}

function formatIssue(issue: Record<string, unknown>): string {
  if (typeof issue.message === "string") return issue.message;
  if (typeof issue.count === "number") return `${issue.count.toLocaleString()} units`;
  return String(issue.severity ?? "review");
}
