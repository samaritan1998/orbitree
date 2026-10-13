import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Tooltip } from "@fluentui/react-components";
import { ArrowSync20Regular, Checkmark20Regular, Settings20Regular } from "@fluentui/react-icons";
import { api } from "../api";

const CUSTOM_MODEL = "__custom_model__";

export default function ClaudeExecutionControls({ branchId }: { branchId: string }) {
  const queryClient = useQueryClient();
  const queryKey = ["claude-branch-execution", branchId];
  const query = useQuery({ queryKey, queryFn: () => api.claudeBranchExecution(branchId), staleTime: 15_000 });
  const [open, setOpen] = useState(false);
  const [model, setModel] = useState("");
  const [custom, setCustom] = useState(false);
  const [effort, setEffort] = useState("");
  const [notice, setNotice] = useState("");
  const data = query.data;
  const save = useMutation({
    mutationFn: () => api.updateClaudeBranchExecution(branchId, { model: model.trim(), reasoning_effort: effort }),
    onSuccess: (result) => {
      queryClient.setQueryData(queryKey, result);
      queryClient.invalidateQueries({ queryKey: ["terminal-command", branchId] });
      queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      setOpen(false);
      setNotice("Saved for the next turn");
    },
  });
  if (query.isError) return <div className="execution-controls">
    <span role="alert">Claude settings unavailable</span>
    <Tooltip content={query.error.message} relationship="description"><Button size="small" icon={<ArrowSync20Regular />} onClick={() => query.refetch()}>Retry</Button></Tooltip>
  </div>;
  if (!data?.effective || !Array.isArray(data.models) || !Array.isArray(data.reasoning_efforts)) return null;
  const edit = () => {
    setModel(data.effective.model || "");
    setCustom(!data.models.some((item) => item.id === data.effective.model));
    setEffort(data.effective.reasoning_effort || "");
    setNotice("");
    save.reset();
    setOpen(true);
  };
  const validEffort = data.reasoning_efforts.includes(effort);
  return <div className="execution-controls">
    <span className="execution-next-label">Next turn</span>
    <Tooltip content="Change this Branch's next Web turn or local launch; running work is unchanged" relationship="description">
      <Button size="small" appearance="subtle" icon={<Settings20Regular />} onClick={edit}>
        {data.effective.label} · {data.effective.model} · {data.effective.reasoning_effort}
      </Button>
    </Tooltip>
    {notice && <small role="status">{notice}</small>}
    <Dialog open={open} onOpenChange={(_, value) => { if (!save.isPending) setOpen(value.open); }}>
      <DialogSurface className="execution-dialog">
        <DialogBody>
          <DialogTitle>Next turn</DialogTitle>
          <DialogContent>
            <div className="execution-fields">
              <Field label="Account"><span>{data.effective.label}</span></Field>
              <Field label="Model" required>
                <Select aria-label="Model" value={custom ? CUSTOM_MODEL : model} disabled={save.isPending} onChange={(_, value) => {
                  setCustom(value.value === CUSTOM_MODEL);
                  setModel(value.value === CUSTOM_MODEL ? "" : value.value);
                  save.reset();
                }}>
                  {data.models.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                  <option value={CUSTOM_MODEL}>Custom model...</option>
                </Select>
              </Field>
              {custom && <Field label="Custom model ID" required>
                <Input aria-label="Custom model ID" value={model} maxLength={160} disabled={save.isPending} onChange={(_, value) => { setModel(value.value); save.reset(); }} />
              </Field>}
              <Field label="Reasoning effort" required>
                <Select aria-label="Reasoning effort" title="Requested effort; support depends on the selected Claude model" value={validEffort ? effort : ""} disabled={save.isPending} onChange={(_, value) => { setEffort(value.value); save.reset(); }}>
                  {!validEffort && <option value="" disabled>Select reasoning effort</option>}
                  {data.reasoning_efforts.map((value) => <option key={value}>{value}</option>)}
                </Select>
              </Field>
            </div>
            {save.error && <p role="alert">{save.error.message}</p>}
          </DialogContent>
          <DialogActions>
            <Button disabled={save.isPending} onClick={() => setOpen(false)}>Cancel</Button>
            <Button appearance="primary" icon={<Checkmark20Regular />} disabled={save.isPending || !model.trim() || !validEffort} onClick={() => save.mutate()}>{save.isPending ? "Saving" : "Apply"}</Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  </div>;
}
