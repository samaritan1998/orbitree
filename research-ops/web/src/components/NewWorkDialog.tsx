import { useEffect, useState } from "react";
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
  Textarea,
} from "@fluentui/react-components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { AgentRuntime, Program } from "../types";

type Props = {
  open: boolean;
  programs: Program[];
  initialProgram: string;
  onClose: () => void;
  onCreated: (programCode: string, workId: string) => void;
};

export default function NewWorkDialog({ open, programs, initialProgram, onClose, onCreated }: Props) {
  const queryClient = useQueryClient();
  const [program, setProgram] = useState(initialProgram);
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [runtime, setRuntime] = useState<AgentRuntime>("codex");
  const [mode, setMode] = useState("write");
  const [repositoryId, setRepositoryId] = useState("");
  const [parentId, setParentId] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.repositories });
  const tree = useQuery({
    queryKey: ["tree", program],
    queryFn: () => api.tree(program),
    enabled: open && Boolean(program),
  });
  const availableRepositories = (repositories.data ?? []).filter(
    (item) => !item.program_codes.length || item.program_codes.includes(program),
  );

  useEffect(() => {
    setProgram(initialProgram);
    setParentId("");
  }, [initialProgram, open]);

  const resetDraft = () => {
    setTitle("");
    setObjective("");
    setRuntime("codex");
    setMode("write");
    setRepositoryId("");
    setParentId("");
    setAcceptance("");
  };
  const closeAndDiscard = () => {
    resetDraft();
    onClose();
  };
  const hasDraft = Boolean(
    title.trim()
    || objective.trim()
    || acceptance.trim()
    || repositoryId
    || parentId
    || runtime !== "codex"
    || mode !== "write",
  );

  const create = useMutation({
    mutationFn: () => api.createWork({
      program_code: program,
      title,
      objective,
      definition_mode: "defined",
      branch_mode: mode,
      runtime,
      priority: 50,
      repository_id: repositoryId || undefined,
      primary_parent_id: parentId || undefined,
      acceptance_criteria: acceptance.trim()
        ? [{ description: acceptance.trim(), kind: "manual" }]
        : [],
    }),
    onSuccess: async (workItem) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["tree", program] }),
        queryClient.invalidateQueries({ queryKey: ["today"] }),
      ]);
      resetDraft();
      onCreated(program, workItem.human_id);
    },
  });

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && closeAndDiscard()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>New work</DialogTitle>
          <DialogContent className="dialog-form">
            <Field label="Program">
              <Select value={program} onChange={(_, data) => { setProgram(data.value); setParentId(""); }}>
                {programs.map((item) => <option value={item.code} key={item.code}>{item.name}</option>)}
              </Select>
            </Field>
            <Field label="Title" required>
              <Input value={title} onChange={(_, data) => setTitle(data.value)} />
            </Field>
            <Field label="Objective (optional)">
              <Textarea resize="vertical" value={objective} onChange={(_, data) => setObjective(data.value)} />
            </Field>
            <Field
              label="Agent runtime"
              hint={runtime === "pi"
                ? "Pi runs as a Web-managed session. Local Start/Continue is unavailable."
                : runtime === "claude"
                  ? "Claude uses your existing Claude Code login and supports Web turns or the local Claude TUI."
                  : "Codex supports both the local terminal and Web turns."}
            >
              <Select
                value={runtime}
                onChange={(_, data) => setRuntime(data.value as AgentRuntime)}
              >
                <option value="codex">Codex</option>
                <option value="claude">Claude Code</option>
                <option value="pi">Pi Agent</option>
              </Select>
            </Field>
            <Field label="Parent work" hint="Optional. The task appears beneath this WorkItem in the Program tree.">
              <Select value={parentId} onChange={(_, data) => setParentId(data.value)}>
                <option value="">Program root</option>
                {(tree.data?.work_items ?? []).map((item) => (
                  <option value={item.id} key={item.id}>{item.human_id} - {item.title}</option>
                ))}
              </Select>
            </Field>
            <Field label="Acceptance" hint="One explicit completion criterion for the first iteration.">
              <Input value={acceptance} onChange={(_, data) => setAcceptance(data.value)} />
            </Field>
            <Field label="Mode">
              <Select value={mode} onChange={(_, data) => setMode(data.value)}>
                <option value="write">Writable branch</option>
                <option value="read">Read-only exploration</option>
                <option value="capture">Capture only</option>
              </Select>
            </Field>
            <Field label="Repository" hint="Optional. A registered Git repository creates a real worktree; otherwise Orbitree creates a scratch repository.">
              <Select value={repositoryId} onChange={(_, data) => setRepositoryId(data.value)}>
                <option value="">Scratch workspace</option>
                {availableRepositories.map((item) => (
                  <option value={item.id} key={item.id}>{item.name}</option>
                ))}
              </Select>
            </Field>
            {create.error && <div className="inline-error">{create.error.message}</div>}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={closeAndDiscard}>{hasDraft ? "Discard" : "Cancel"}</Button>
            <Button
              appearance="primary"
              disabled={!title.trim() || create.isPending}
              onClick={() => create.mutate()}
            >
              Create
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
