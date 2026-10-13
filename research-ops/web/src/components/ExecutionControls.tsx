import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Field, Input, Select, Spinner, Tooltip } from "@fluentui/react-components";
import { ArrowSync20Regular, Checkmark20Regular, Copy20Regular, Person20Regular, Settings20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import { copyText } from "../clipboard";
import type { ExecutionIdentity, ExecutionModel, ExecutionPreferences } from "../types";

const CUSTOM_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"];
const ONEAPI_EFFORTS = ["none", "minimal", ...CUSTOM_EFFORTS];
const CUSTOM_MODEL = "__custom_model__";

function availableEfforts(identity: ExecutionPreferences["identity"], model?: ExecutionModel, custom = false) {
  if (identity === "oneapi") return model?.reasoning_efforts.length ? model.reasoning_efforts : ONEAPI_EFFORTS;
  return custom ? CUSTOM_EFFORTS : model?.reasoning_efforts || [];
}

export function ExecutionBadge({ value }: { value: unknown }) {
  if (!value || typeof value !== "object") return null;
  const execution = value as ExecutionIdentity;
  if (!execution.identity) return null;
  return <span className="execution-badge" title={execution.base_url}>
    <Person20Regular />
    {execution.label} · {execution.model} · {execution.reasoning_effort}
  </span>;
}

export default function ExecutionControls({ branchId }: { branchId: string }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["branch-execution", branchId], queryFn: () => api.branchExecution(branchId), staleTime: 15_000 });
  const [open, setOpen] = useState(false);
  const [identity, setIdentity] = useState<ExecutionPreferences["identity"]>("api");
  const [model, setModel] = useState("");
  const [custom, setCustom] = useState(false);
  const [effort, setEffort] = useState("max");
  const [consent, setConsent] = useState(false);
  const [notice, setNotice] = useState("");
  const [copied, setCopied] = useState(false);
  const data = query.data;
  const selected = data?.identities?.find((item) => item.id === identity);
  const catalog = useQuery({
    queryKey: ["identity-models", identity],
    queryFn: () => api.identityModels(identity),
    enabled: open && selected?.ready === true,
    staleTime: 300_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const refreshModels = useMutation({
    mutationFn: (target: ExecutionPreferences["identity"]) => api.identityModels(target, true),
    onSuccess: (result, target) => queryClient.setQueryData(["identity-models", target], result),
  });
  const models = catalog.data?.identity === identity && Array.isArray(catalog.data.models) ? catalog.data.models : [];
  const listedModel = models.find((item) => item.id === model);
  const supportsCustom = identity !== "chatgpt";
  const isCustom = supportsCustom && (custom || (Boolean(model) && !listedModel && (catalog.isSuccess || catalog.isError)));
  const efforts = availableEfforts(identity, listedModel, isCustom);
  const manualOneApiEffort = identity === "oneapi" && !listedModel?.reasoning_efforts.length;
  const validEffort = efforts.includes(effort);
  const loadingModels = catalog.isFetching || (refreshModels.isPending && refreshModels.variables === identity);
  const catalogError = catalog.error || (refreshModels.variables === identity ? refreshModels.error : null);
  const save = useMutation({
    mutationFn: () => api.updateBranchExecution(branchId, { identity, model: model.trim(), reasoning_effort: effort }),
    onSuccess: (data) => {
      queryClient.setQueryData(["branch-execution", branchId], data);
      queryClient.invalidateQueries({ queryKey: ["terminal-command", branchId] });
      queryClient.invalidateQueries({ queryKey: ["branch", branchId] });
      setOpen(false);
      setNotice("Saved for the next turn");
    },
  });
  // The generated frontend may be deployed before an idle backend restart.
  if (query.error?.message === "Not Found") return null;
  if (data?.runtime !== "codex") return null;
  if (!data?.effective || !data.identities) return query.error
    ? <div className="execution-controls"><span role="alert">Identity unavailable</span><Button size="small" icon={<ArrowSync20Regular />} onClick={() => query.refetch()}>Retry</Button></div>
    : null;
  const personalSwitch = identity === "chatgpt" && data.effective.identity !== "chatgpt";
  const edit = () => {
    setIdentity(data.effective.identity);
    setModel(data.effective.model || "");
    setCustom(false);
    setEffort(data.effective.reasoning_effort || "max");
    setConsent(false);
    setNotice("");
    save.reset();
    refreshModels.reset();
    setOpen(true);
  };
  return <div className="execution-controls">
    <span className="execution-next-label">Next turn</span>
    <Tooltip content="Change identity and model for the next turn; running work keeps its identity" relationship="description">
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
              <Field label="Identity">
                <Select value={identity} onChange={(_, value) => {
                  const next = value.value as ExecutionPreferences["identity"];
                  setIdentity(next);
                  setConsent(false);
                  setCustom(false);
                  save.reset();
                  refreshModels.reset();
                  const defaults = data.identities.find((item) => item.id === next);
                  setModel(next === data.effective.identity ? data.effective.model || "" : defaults?.default_model || "");
                  setEffort(next === data.effective.identity ? data.effective.reasoning_effort || "max" : defaults?.default_reasoning_effort || "");
                }}>
                  {data.identities.map((item) => <option value={item.id} key={item.id}>{item.label}{item.ready ? "" : item.id === "chatgpt" ? " (not signed in)" : " (not configured)"}</option>)}
                </Select>
              </Field>
              <Field label="Model" required>
                <div className="execution-model-select">
                  <Select aria-label="Model" aria-busy={loadingModels} value={isCustom ? CUSTOM_MODEL : model} disabled={!selected?.ready || loadingModels} onChange={(_, value) => {
                    save.reset();
                    if (value.value === CUSTOM_MODEL) {
                      setCustom(true);
                      setModel("");
                      setEffort(identity === "oneapi" && ONEAPI_EFFORTS.includes(effort) ? effort : "high");
                    } else {
                      const next = models.find((item) => item.id === value.value);
                      const nextEfforts = availableEfforts(identity, next);
                      const defaultEffort = next?.default_reasoning_effort || (identity === "oneapi" ? selected?.default_reasoning_effort || "high" : "");
                      setCustom(false);
                      setModel(value.value);
                      setEffort(nextEfforts.includes(effort) ? effort : nextEfforts.includes(defaultEffort) ? defaultEffort : nextEfforts[0] || "");
                    }
                  }}>
                    <option value="" disabled>{loadingModels ? "Loading models..." : "Select model"}</option>
                    {model && !listedModel && !isCustom && <option value={model} disabled>{model} (not listed)</option>}
                    {models.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                    {supportsCustom && <option value={CUSTOM_MODEL}>Custom model...</option>}
                  </Select>
                  <Tooltip content="Refresh model list" relationship="label">
                    <Button icon={loadingModels ? <Spinner size="tiny" /> : <ArrowSync20Regular />} disabled={!selected?.ready || loadingModels} onClick={() => refreshModels.mutate(identity)} />
                  </Tooltip>
                </div>
              </Field>
              {isCustom && <Field label="Custom model ID" required>
                <Input aria-label="Custom model ID" value={model} onChange={(_, value) => setModel(value.value)} />
              </Field>}
              <Field label={manualOneApiEffort ? "Reasoning effort (manual)" : "Reasoning effort"} required>
                <Select aria-label="Reasoning effort" title={manualOneApiEffort ? "Manual protocol setting. Model support is not advertised by OneAPI." : undefined} value={validEffort ? effort : ""} disabled={!model || !efforts.length || loadingModels} onChange={(_, value) => setEffort(value.value)}>
                  {!validEffort && <option value="" disabled>Select reasoning effort</option>}
                  {efforts.map((value) => <option key={value}>{value}</option>)}
                </Select>
              </Field>
              {catalogError && selected?.ready && <p className="execution-model-status" role="alert">{catalogError.message}</p>}
              {catalog.isSuccess && !models.length && selected?.ready && <p className="execution-model-status" role="status">No models available</p>}
              {listedModel && !efforts.length && !isCustom && <p className="execution-model-status" role="status">Reasoning options unavailable for this model</p>}
              {isCustom && <small className="execution-model-status">Custom model capabilities are not verified</small>}
            </div>
            {!selected?.ready && <div className="execution-login" role="status">
              <p>{selected?.reason}</p>
              {identity === "chatgpt" && <div className="execution-login-command">
                <code>{data.login_command}</code>
                <Tooltip content="Copy personal login command" relationship="label">
                  <Button icon={copied ? <Checkmark20Regular /> : <Copy20Regular />} onClick={async () => {
                    try { await copyText(data.login_command); setCopied(true); } catch { setNotice("Clipboard unavailable"); }
                  }} />
                </Tooltip>
              </div>}
              <Button size="small" icon={<ArrowSync20Regular />} disabled={query.isFetching} onClick={() => query.refetch()}>Refresh login status</Button>
            </div>}
            {personalSwitch && <Checkbox checked={consent} onChange={(_, value) => setConsent(value.checked === true)} label="Use my personal account for this branch's conversation and work context" />}
            {data.native_goal_execution?.identity && <p className="execution-goal-note">Existing Goal: {data.native_goal_execution.label} · {data.native_goal_execution.model}</p>}
            {save.error && <p role="alert">{save.error.message}</p>}
          </DialogContent>
          <DialogActions>
            <Button disabled={save.isPending} onClick={() => setOpen(false)}>Cancel</Button>
            <Button appearance="primary" icon={<Checkmark20Regular />} disabled={save.isPending || loadingModels || !selected?.ready || !model.trim() || !validEffort || (!listedModel && !isCustom) || (personalSwitch && !consent)} onClick={() => save.mutate()}>{save.isPending ? "Saving" : "Apply"}</Button>
          </DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  </div>;
}
