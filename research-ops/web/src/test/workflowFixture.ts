import type { SessionWorkflowView, WorkflowLinkView } from "../types";

export const workflowLinksFixture: WorkflowLinkView = {
  branch_id: "workflow-test",
  links: [
    { title: "Workflow: Long-context paired evaluation", url: "http://octo.example.test/#workflow/context-eval", source: "conversation", record_id: "turn:one", at: "2026-09-16T08:00:00Z" },
    { title: "Workflow: Prepare trajectories, train checkpoints, deploy replicas and evaluate at 256K context", url: "http://octo.example.test/#workflow/training-pipeline", source: "conversation", record_id: "turn:two", at: "2026-09-16T07:00:00Z" },
    { title: "DAG: Rollout and reward", url: "https://example.test/rollout-workflow", source: "conversation", record_id: "turn:three", at: "2026-09-16T06:00:00Z" },
  ],
};

export const workflowFixture: SessionWorkflowView = {
  branch_id: "workflow-test", enabled: true, observing: true,
  analysis_status: "ready", analysis_error: null, analysis_stale: false,
  analyzed_at: "2026-09-15T08:00:00Z", observed_at: "2026-09-15T08:00:00Z", next_analysis_at: null,
  octo: { state: "not_configured", last_synced_at: null, error: null }, warnings: [],
  graphs: [{
    id: "analysis", title: "Session analysis", source: "analysis", observed_at: "2026-09-15T08:00:00Z",
    summary: "Training completed. Paired evaluation is still running.", stale: false,
    nodes: [
      { id: "prepare", title: "Prepare trajectories", status: "completed", raw_status: "completed", status_basis: "reported", evidence_refs: ["turn:1"], detail: "Dataset passed validation.", associated: true },
      { id: "train", title: "Train model", status: "completed", raw_status: "Succeeded", status_basis: "observed", evidence_refs: ["job:1"], detail: "Checkpoint is available.", associated: true },
      { id: "eval", title: "Long-context paired evaluation", status: "running", raw_status: "Running", status_basis: "observed", evidence_refs: ["job:2"], detail: "The registered evaluator is running.", associated: true },
    ],
    edges: [{ source: "prepare", target: "train", evidence_refs: ["turn:1"] }, { source: "train", target: "eval", evidence_refs: ["job:2"] }],
  }],
  evidence: [
    { id: "turn:1", kind: "turn", label: "Prepare the trajectories", excerpt: "Query: Prepare the trajectories\nFinal answer: Dataset passed validation.", at: "2026-09-15T06:00:00Z", status: "completed" },
    { id: "job:1", kind: "job", label: "training-job-r1", excerpt: "LTP: training-job-r1; status=Succeeded", at: "2026-09-15T07:00:00Z", status: "Succeeded" },
    { id: "job:2", kind: "job", label: "evaluation-job-r1", excerpt: "LTP: evaluation-job-r1; status=Running", at: "2026-09-15T08:00:00Z", status: "Running" },
  ],
};
