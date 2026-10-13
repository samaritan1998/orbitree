import { useState } from "react";
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
} from "@fluentui/react-components";
import { Add20Regular, FolderOpen20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import type { Program } from "../types";

export default function RepositoriesView({ programs }: { programs: Program[] }) {
  const queryClient = useQueryClient();
  const repositories = useQuery({ queryKey: ["repositories"], queryFn: api.repositories });
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [remote, setRemote] = useState("");
  const [defaultRef, setDefaultRef] = useState("HEAD");
  const [programCodes, setProgramCodes] = useState("");

  const create = useMutation({
    mutationFn: () => api.createRepository({
      name: name.trim(),
      canonical_path: path.trim(),
      remote_url: remote.trim() || undefined,
      default_ref: defaultRef.trim() || "HEAD",
      program_codes: programCodes.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean),
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["repositories"] });
      setOpen(false);
      setName(""); setPath(""); setRemote(""); setDefaultRef("HEAD"); setProgramCodes("");
    },
  });

  return <div className="page">
    <header className="page-header">
      <div><h1>Repositories</h1><p>Canonical Git repositories available for isolated branch worktrees.</p></div>
      <Button appearance="primary" icon={<Add20Regular />} onClick={() => setOpen(true)}>Register</Button>
    </header>
    <div className="repository-list">
      {(repositories.data ?? []).map((repository) => <article key={repository.id}>
        <FolderOpen20Regular />
        <div><strong>{repository.name}</strong><code>{repository.canonical_path}</code></div>
        <span>{repository.default_ref}</span>
        <div className="repository-programs">
          {(repository.program_codes.length ? repository.program_codes : ["ALL"]).map((code) => <small key={code}>{code}</small>)}
        </div>
      </article>)}
      {!repositories.isLoading && !repositories.data?.length && <div className="empty-row">No repositories registered. New work will use scratch Git repositories.</div>}
    </div>
    <Dialog open={open} onOpenChange={(_, data) => setOpen(data.open)}>
      <DialogSurface><DialogBody><DialogTitle>Register repository</DialogTitle>
        <DialogContent className="dialog-form">
          <Field label="Name" required><Input value={name} onChange={(_, data) => setName(data.value)} /></Field>
          <Field label="Canonical path" required hint="Absolute path to an existing local Git repository."><Input value={path} onChange={(_, data) => setPath(data.value)} /></Field>
          <Field label="Remote URL"><Input value={remote} onChange={(_, data) => setRemote(data.value)} /></Field>
          <Field label="Default ref"><Input value={defaultRef} onChange={(_, data) => setDefaultRef(data.value)} /></Field>
          <Field label="Programs" hint={`Comma separated: ${programs.map((item) => item.code).join(", ")}. Empty means all.`}><Input value={programCodes} onChange={(_, data) => setProgramCodes(data.value)} /></Field>
          {create.error && <div className="inline-error">{create.error.message}</div>}
        </DialogContent>
        <DialogActions><Button onClick={() => setOpen(false)}>Cancel</Button><Button appearance="primary" disabled={!name.trim() || !path.trim() || create.isPending} onClick={() => create.mutate()}>Register</Button></DialogActions>
      </DialogBody></DialogSurface>
    </Dialog>
  </div>;
}
