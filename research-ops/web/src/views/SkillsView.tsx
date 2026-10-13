import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
  Tooltip,
} from "@fluentui/react-components";
import {
  ArrowClockwise20Regular,
  BookToolbox20Regular,
  Checkmark20Regular,
  Copy20Regular,
  Dismiss20Regular,
  Search20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDate } from "../time";
import { copyText } from "../clipboard";
import type { InstalledSkill, SkillProposal } from "../types";

type ReviewAction = { proposal: SkillProposal; mode: "accept" | "reject" };

const ACTIVE_STATUSES = new Set(["pending", "analyzing", "installing"]);
const REVIEW_STATUSES = new Set(["pending", "analyzing", "ready", "installing", "failed"]);

export default function SkillsView({ view = "installed" }: { view?: "installed" | "proposals" }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [scope, setScope] = useState("all");
  const [filter, setFilter] = useState("review");
  const [reviewAction, setReviewAction] = useState<ReviewAction>();
  const [reviewerNote, setReviewerNote] = useState("");
  const [copiedPath, setCopiedPath] = useState("");
  const installed = useQuery({
    queryKey: ["installed-skills"],
    queryFn: () => api.installedSkills(),
    enabled: view === "installed",
  });
  const proposals = useQuery({
    queryKey: ["skill-proposals"],
    queryFn: () => api.skillProposals(),
    enabled: view === "proposals",
    refetchInterval: 5_000,
  });
  const visible = useMemo(() => {
    const entries = proposals.data ?? [];
    if (filter === "all") return entries;
    if (filter === "review") {
      return entries.filter((item) => REVIEW_STATUSES.has(item.status));
    }
    return entries.filter((item) => item.status === filter);
  }, [filter, proposals.data]);
  const ready = proposals.data?.filter((item) => item.status === "ready").length ?? 0;
  const processing = proposals.data?.filter((item) => ACTIVE_STATUSES.has(item.status)).length ?? 0;
  const visibleInstalled = useMemo(() => {
    const query = search.trim().toLowerCase();
    return (installed.data ?? []).filter((skill) => {
      if (scope !== "all" && skill.scope !== scope) return false;
      if (!query) return true;
      return `${skill.name} ${skill.directory_name} ${skill.description}`.toLowerCase().includes(query);
    });
  }, [installed.data, scope, search]);

  const copyPath = async (path: string) => {
    await copyText(path);
    setCopiedPath(path);
    window.setTimeout(() => setCopiedPath(""), 1800);
  };

  const review = useMutation({
    mutationFn: async ({ proposal, mode }: ReviewAction) => mode === "accept"
      ? api.acceptSkillProposal(proposal.id, reviewerNote.trim())
      : api.rejectSkillProposal(proposal.id, reviewerNote.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["skill-proposals"] });
      setReviewAction(undefined);
      setReviewerNote("");
    },
  });
  const retry = useMutation({
    mutationFn: (id: string) => api.retrySkillProposal(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["skill-proposals"] }),
  });

  return <div className="page skills-page">
    <header className="page-header">
      <div><h1>{view === "installed" ? "技能" : "技能审核"}</h1></div>
      <div className="header-metrics">
        {view === "installed"
          ? <div><strong>{installed.data?.length ?? 0}</strong>Installed</div>
          : <div><strong>{ready}</strong>Ready</div>}
      </div>
    </header>

    {view === "installed" && <>
      <div className="skills-toolbar">
        <Input
          contentBefore={<Search20Regular />}
          placeholder="Search installed skills"
          aria-label="Search installed skills"
          value={search}
          onChange={(_event, data) => setSearch(data.value)}
        />
        <Select value={scope} onChange={(_event, data) => setScope(data.value)} aria-label="Filter installed skill scope">
          <option value="all">All scopes</option>
          <option value="personal">Personal</option>
          <option value="system">System</option>
        </Select>
        <Tooltip content="Rescan local skill directories" relationship="description">
          <Button
            appearance="subtle"
            icon={<ArrowClockwise20Regular />}
            aria-label="Refresh installed skills"
            onClick={() => installed.refetch()}
          />
        </Tooltip>
      </div>
      {installed.isLoading && <div className="page-loading"><Spinner label="Loading installed skills" /></div>}
      {installed.error && <div className="page-error">{installed.error.message}</div>}
      {!installed.isLoading && !visibleInstalled.length && <div className="empty-state">
        <BookToolbox20Regular />
        <strong>No installed skills found</strong>
        <span>Change the search or scope filter, then refresh the local catalog.</span>
      </div>}
      <div className="installed-skill-list">
        {visibleInstalled.map((skill) => <InstalledSkillRow
          key={`${skill.scope}:${skill.install_path}`}
          skill={skill}
          copiedPath={copiedPath}
          onCopy={copyPath}
        />)}
      </div>
    </>}

    {view === "proposals" && <>
      <div className="skills-toolbar proposals-toolbar">
        <span>{processing ? `${processing} extraction job${processing === 1 ? "" : "s"} running` : "Completed rounds proposed for review"}</span>
        <Select value={filter} onChange={(_, data) => setFilter(data.value)} aria-label="Filter skill proposals">
          <option value="review">Needs review</option>
          <option value="all">All proposals</option>
          <option value="ready">Ready</option>
          <option value="accepted">Accepted</option>
          <option value="rejected">Rejected</option>
          <option value="failed">Failed</option>
          <option value="no_skill">No reusable skill</option>
        </Select>
      </div>
      {proposals.isLoading && <div className="page-loading"><Spinner label="Loading skill proposals" /></div>}
      {proposals.error && <div className="page-error">{proposals.error.message}</div>}
      {!proposals.isLoading && !visible.length && <div className="empty-state">
        <BookToolbox20Regular />
        <strong>No skill proposals</strong>
        <span>Choose skill extraction when ending a work round.</span>
      </div>}
      <div className="skill-proposal-list">
        {visible.map((proposal) => <article key={proposal.id}>
        <header className="skill-proposal-head">
          <div className="skill-proposal-meta">
            <strong className={`skill-status status-${proposal.status}`}>{formatStatus(proposal.status)}</strong>
            {proposal.action && <span>{proposal.action}</span>}
            <code>{proposal.target_skill ?? proposal.candidate_name ?? "unclassified"}</code>
          </div>
          <div className="skill-source">
            <strong>{proposal.source_context.work_human_id ?? "Work"}</strong>
            <span>{proposal.source_context.work_title ?? proposal.source_context.branch_name}</span>
          </div>
        </header>

        {ACTIVE_STATUSES.has(proposal.status) && <div className="skill-processing"><Spinner size="tiny" /> AI extraction is {formatStatus(proposal.status).toLowerCase()}.</div>}
        {!ACTIVE_STATUSES.has(proposal.status) && <div className="skill-proposal-body">
          <div className="skill-summary">
            <h2>{proposal.title || "Untitled proposal"}</h2>
            {proposal.description && <p>{proposal.description}</p>}
            {proposal.rationale && <p className="skill-rationale">{proposal.rationale}</p>}
            {!!proposal.evidence_refs.length && <div className="evidence-refs">
              {proposal.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}
            </div>}
          </div>

          {!!proposal.reusable_steps.length && <section className="skill-facts">
            <h3>Reusable procedure</h3>
            <ol>{proposal.reusable_steps.map((step) => <li key={step}>{step}</li>)}</ol>
          </section>}
          {!!proposal.risks.length && <section className="skill-facts risk-list">
            <h3>Risks</h3>
            <ul>{proposal.risks.map((risk) => <li key={risk}>{risk}</li>)}</ul>
          </section>}
          {proposal.error && <div className="inline-error">{proposal.error}</div>}
          {proposal.diff_text && <details className="skill-diff">
            <summary>Review proposed diff</summary>
            <pre>{proposal.diff_text}</pre>
          </details>}
          {proposal.status === "accepted" && <div className="skill-paths">
            {proposal.source_path && <div><span>Source</span><code>{proposal.source_path}</code></div>}
            {proposal.install_path && <div><span>Installed</span><code>{proposal.install_path}</code></div>}
          </div>}
        </div>}

        <footer className="skill-proposal-actions">
          {["failed", "no_skill", "rejected"].includes(proposal.status) && <Button
            icon={<ArrowClockwise20Regular />}
            disabled={retry.isPending}
            onClick={() => retry.mutate(proposal.id)}
          >Retry extraction</Button>}
          {proposal.status === "ready" && <>
            <Button icon={<Dismiss20Regular />} onClick={() => openReview(proposal, "reject", setReviewAction, setReviewerNote)}>Reject</Button>
            <Button appearance="primary" icon={<Checkmark20Regular />} onClick={() => openReview(proposal, "accept", setReviewAction, setReviewerNote)}>Accept and install</Button>
          </>}
        </footer>
        </article>)}
      </div>
    </>}

    <Dialog open={Boolean(reviewAction)} onOpenChange={(_, data) => !data.open && setReviewAction(undefined)}>
      <DialogSurface><DialogBody>
        <DialogTitle>{reviewAction?.mode === "accept" ? "Accept and install skill" : "Reject skill proposal"}</DialogTitle>
        <DialogContent className="dialog-form">
          {reviewAction?.mode === "accept" && <div className="inline-note">
            This writes the reviewed SKILL.md to the source skill repository and installs it for future Codex sessions.
          </div>}
          <div className="session-dialog-source">
            <code>{reviewAction?.proposal.target_skill ?? reviewAction?.proposal.candidate_name}</code>
            <span>{reviewAction?.proposal.action === "merge" ? "Merge into existing skill" : "Create new skill"}</span>
          </div>
          <Field label="Reviewer note">
            <Textarea resize="vertical" value={reviewerNote} onChange={(_, data) => setReviewerNote(data.value)} />
          </Field>
          {review.error && <div className="inline-error">{review.error.message}</div>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setReviewAction(undefined)}>Cancel</Button>
          <Button
            appearance={reviewAction?.mode === "accept" ? "primary" : "secondary"}
            disabled={!reviewAction || review.isPending}
            onClick={() => reviewAction && review.mutate(reviewAction)}
          >{reviewAction?.mode === "accept" ? "Accept and install" : "Reject"}</Button>
        </DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>
  </div>;
}

function InstalledSkillRow({
  skill,
  copiedPath,
  onCopy,
}: {
  skill: InstalledSkill;
  copiedPath: string;
  onCopy: (path: string) => void;
}) {
  const paths = [
    { label: "Installed", value: skill.install_path },
    ...(skill.source_path ? [{ label: "Source", value: skill.source_path }] : []),
  ];
  return <article>
    <BookToolbox20Regular />
    <div className="installed-skill-copy">
      <div className="installed-skill-title">
        <h2>{skill.name}</h2>
        {skill.directory_name !== skill.name && <code>{skill.directory_name}</code>}
      </div>
      {skill.description
        ? <p>{skill.description}</p>
        : <p className="muted-row">No readable description in SKILL.md.</p>}
      {skill.error && <div className="inline-error">{skill.error}</div>}
      <div className="installed-skill-paths">
        {paths.map((path) => <div key={path.label}>
          <span>{path.label}</span>
          <code title={path.value}>{path.value}</code>
          <Tooltip content={copiedPath === path.value ? "Copied" : `Copy ${path.label.toLowerCase()} path`} relationship="label">
            <Button
              appearance="subtle"
              size="small"
              icon={copiedPath === path.value ? <Checkmark20Regular /> : <Copy20Regular />}
              aria-label={`Copy ${path.label.toLowerCase()} path for ${skill.name}`}
              onClick={() => onCopy(path.value)}
            />
          </Tooltip>
        </div>)}
      </div>
    </div>
    <div className="installed-skill-meta">
      <strong className={`skill-scope scope-${skill.scope}`}>{formatStatus(skill.scope)}</strong>
      <span className={skill.valid ? "skill-valid" : "skill-invalid"}>{skill.valid ? "Valid" : "Invalid"}</span>
      {skill.scope === "personal" && <span>{skill.source_available ? "Source linked" : "Installed only"}</span>}
      <span>{skill.supporting_files} supporting file{skill.supporting_files === 1 ? "" : "s"}</span>
      <time dateTime={skill.updated_at}>{formatDate(skill.updated_at)}</time>
    </div>
  </article>;
}

function openReview(
  proposal: SkillProposal,
  mode: "accept" | "reject",
  setReviewAction: (value: ReviewAction) => void,
  setReviewerNote: (value: string) => void,
) {
  setReviewerNote("");
  setReviewAction({ proposal, mode });
}

function formatStatus(status: string): string {
  return status.split("_").map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`).join(" ");
}
