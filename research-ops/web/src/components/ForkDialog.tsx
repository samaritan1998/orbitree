import { useState } from "react";
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
import { useMutation } from "@tanstack/react-query";
import { api } from "../api";

export default function ForkDialog({ branchId, open, onClose, onCreated }: {
  branchId: string;
  open: boolean;
  onClose: () => void;
  onCreated: (branchId: string, continuation: "continue" | "parallel") => void;
}) {
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [mode, setMode] = useState("write");
  const [continuation, setContinuation] = useState<"continue" | "parallel">("parallel");
  const fork = useMutation({
    mutationFn: async () => {
      const effectiveContinuation = mode === "capture" ? "parallel" : continuation;
      const branch = await api.fork(branchId, {
        name,
        objective,
        mode,
        continuation: effectiveContinuation,
      });
      const provisioning = branch.context_snapshot.provisioning as Record<string, unknown> | undefined;
      if (branch.status === "blocked" || provisioning?.state === "failed") {
        throw new Error(String(provisioning?.error ?? "The Codex fork could not be prepared"));
      }
      return { branch, continuation: effectiveContinuation };
    },
    onSuccess: ({ branch, continuation: effectiveContinuation }) => {
      setName("");
      setObjective("");
      setMode("write");
      setContinuation("parallel");
      onCreated(branch.id, effectiveContinuation);
    },
  });

  return (
    <Dialog open={open} onOpenChange={(_, data) => !data.open && onClose()}>
      <DialogSurface>
        <DialogBody>
          <DialogTitle>Fork branch</DialogTitle>
          <DialogContent className="dialog-form">
            <div role="note">
              Creating this branch is your explicit approval. The child remains idle until you
              separately start its continuation or open it locally.
            </div>
            <Field label="Branch name" required>
              <Input value={name} onChange={(_, data) => setName(data.value)} />
            </Field>
            <Field label="Objective" required>
              <Textarea resize="vertical" value={objective} onChange={(_, data) => setObjective(data.value)} />
            </Field>
            <Field label="Mode">
              <Select value={mode} onChange={(_, data) => setMode(data.value)}>
                <option value="write">Writable branch</option>
                <option value="read">Read-only exploration</option>
                <option value="capture">Capture only</option>
              </Select>
            </Field>
            {mode !== "capture" && <Field label="After fork">
              <Select
                value={continuation}
                onChange={(_, data) => setContinuation(data.value as "continue" | "parallel")}
              >
                <option value="parallel">Keep both branches active</option>
                <option value="continue">Move this conversation to the child</option>
              </Select>
            </Field>}
            {fork.error && <div className="inline-error">{fork.error.message}</div>}
          </DialogContent>
          <DialogActions>
            <Button appearance="secondary" onClick={onClose}>Cancel</Button>
            <Button
              appearance="primary"
              disabled={!name.trim() || !objective.trim() || fork.isPending}
              onClick={() => fork.mutate()}
            >
              {mode === "capture"
                ? "Capture"
                : continuation === "continue" ? "Move to child branch" : "Create parallel branch"}
            </Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
