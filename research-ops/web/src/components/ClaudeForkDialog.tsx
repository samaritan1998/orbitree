import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Textarea } from "@fluentui/react-components";
import { api } from "../api";

export default function ClaudeForkDialog({ branchId, open, submitting, error, onClose, onSubmit }: {
  branchId: string;
  open: boolean;
  submitting: boolean;
  error?: string;
  onClose: () => void;
  onSubmit: (input: Parameters<typeof api.continueWithClaude>[1]) => void;
}) {
  const [includeContext, setIncludeContext] = useState(true);
  const preview = useQuery({
    queryKey: ["claude-handoff", branchId],
    queryFn: () => api.claudeHandoff(branchId),
    enabled: open,
    staleTime: 0,
  });
  useEffect(() => { if (open) setIncludeContext(true); }, [open, branchId]);
  return <Dialog open={open} onOpenChange={(_, state) => { if (!state.open && !submitting) onClose(); }}>
    <DialogSurface>
      <DialogBody>
        <DialogTitle>Fork with Claude</DialogTitle>
        <DialogContent>
          {preview.data && <p>{preview.data.account} · {preview.data.model} · {preview.data.effort}</p>}
          <Checkbox
            checked={includeContext}
            disabled={submitting}
            onChange={(_, state) => setIncludeContext(state.checked === true)}
            label={`Include conversation handoff${preview.data ? ` (${preview.data.turn_count} completed turns)` : ""}`}
          />
          {includeContext && <Field label="First-turn context">
            <Textarea value={preview.data?.prompt ?? ""} readOnly rows={10} style={{ maxHeight: "40vh", minWidth: 0 }} />
          </Field>}
          {preview.error && <div className="inline-error" role="alert">{preview.error.message}</div>}
          {error && <div className="inline-error" role="alert">{error}</div>}
        </DialogContent>
        <DialogActions>
          {(preview.error || error) && <Button disabled={preview.isFetching} onClick={() => void preview.refetch()}>Refresh preview</Button>}
          <Button disabled={submitting} onClick={onClose}>Cancel</Button>
          <Button appearance="primary" disabled={submitting || preview.isFetching || !preview.data?.turn_count || Boolean(preview.error)} onClick={() => onSubmit({
            include_context: includeContext,
            expected_handoff_hash: includeContext ? preview.data?.handoff_hash : undefined,
          })}>
            {submitting ? "Creating..." : "Create fork"}
          </Button>
        </DialogActions>
      </DialogBody>
    </DialogSurface>
  </Dialog>;
}
