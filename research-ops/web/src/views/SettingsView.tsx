import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Field,
  Input,
  Select,
  Spinner,
} from "@fluentui/react-components";
import {
  CheckmarkCircle20Regular,
  Copy20Regular,
  ArrowSync20Regular,
  Settings20Regular,
  Warning20Regular,
} from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime } from "../time";
import { copyText } from "../clipboard";
import ProgramSettings from "../components/ProgramSettings";
import { TeamAccountSettings } from "../team";

const FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export default function SettingsView() {
  const queryClient = useQueryClient();
  const identities = useQuery({ queryKey: ["identity-status"], queryFn: api.identityStatus, staleTime: 15_000 });
  const [loginCopied, setLoginCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const settings = useQuery({
    queryKey: ["runtime-model-settings"],
    queryFn: api.runtimeModelSettings,
    staleTime: 15_000,
  });
  const [codexModel, setCodexModel] = useState("");
  const [codexEffort, setCodexEffort] = useState("max");
  const [piModel, setPiModel] = useState("");
  const [piThinking, setPiThinking] = useState("max");
  const [syncPi, setSyncPi] = useState(true);

  useEffect(() => {
    if (!settings.data) return;
    setCodexModel(settings.data.codex_model);
    setCodexEffort(settings.data.codex_reasoning_effort);
    setPiModel(settings.data.pi_model);
    setPiThinking(settings.data.pi_thinking);
    setSyncPi(
      settings.data.pi_model === settings.data.codex_model
      && settings.data.pi_thinking === settings.data.codex_reasoning_effort,
    );
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => api.updateRuntimeModelSettings({
      codex_model: codexModel.trim(),
      codex_reasoning_effort: codexEffort,
      pi_model: (syncPi ? codexModel : piModel).trim(),
      pi_thinking: syncPi ? codexEffort : piThinking,
    }),
    onSuccess: (data) => {
      queryClient.setQueryData(["runtime-model-settings"], data);
      setCodexModel(data.codex_model);
      setCodexEffort(data.codex_reasoning_effort);
      setPiModel(data.pi_model);
      setPiThinking(data.pi_thinking);
    },
  });

  const data = settings.data;
  const efforts = useMemo(
    () => data?.reasoning_efforts?.length ? data.reasoning_efforts : FALLBACK_EFFORTS,
    [data?.reasoning_efforts],
  );
  const presets = data?.model_presets ?? [];
  const invalid = !codexModel.trim() || (!syncPi && !piModel.trim());

  if (settings.isLoading) {
    return <section className="settings-page page"><TeamAccountSettings /><ProgramSettings /><Spinner label="Loading settings" /></section>;
  }
  if (settings.isError || !data) {
    return (
      <section className="settings-page page-error" role="alert">
        <TeamAccountSettings /><ProgramSettings />
        <Warning20Regular />
        <h1>Settings unavailable</h1>
        <p>{settings.error?.message ?? "The platform settings could not be loaded."}</p>
        <Button onClick={() => settings.refetch()}>Retry</Button>
      </section>
    );
  }

  return (
    <section className="settings-page page">
      <header className="page-header settings-header">
        <div>
          <div className="settings-title-line"><Settings20Regular /><h1>Settings</h1></div>
          <p>Choose the model and reasoning level used by new Research Ops turns.</p>
        </div>
        <div className="settings-source">
          {data.source === "platform" ? <CheckmarkCircle20Regular /> : <Warning20Regular />}
          <span>{data.source === "platform" ? "Platform override" : "Environment default"}</span>
        </div>
      </header>

      <TeamAccountSettings />
      <ProgramSettings />

      <section className="settings-section">
        <header><h2>Codex identities</h2><Button size="small" icon={<ArrowSync20Regular />} disabled={identities.isFetching} onClick={() => identities.refetch()}>Refresh status</Button></header>
        {identities.error && <p role="alert">{identities.error.message}</p>}
        {identities.data && <>
          <div className="identity-status-list">{identities.data.identities.map((identity) => <span key={identity.id}>{identity.ready ? <CheckmarkCircle20Regular /> : <Warning20Regular />} {identity.label}: {identity.ready ? "Credentials present" : identity.reason}</span>)}</div>
          <div className="execution-login-command">
            <code>{identities.data.login_command}</code>
            <Button size="small" icon={<Copy20Regular />} onClick={async () => {
              try { await copyText(identities.data!.login_command); setLoginCopied(true); setCopyError(""); }
              catch { setCopyError("Clipboard unavailable"); }
            }}>{loginCopied ? "Copied" : "Copy login command"}</Button>
          </div>
          {copyError && <p role="alert">{copyError}</p>}
        </>}
      </section>

      <form className="settings-form" onSubmit={(event) => { event.preventDefault(); if (!invalid) save.mutate(); }}>
        <section className="settings-section">
          <header><div><h2>Codex / Web</h2><p>Used for Web turns, local Codex launches managed by Research Ops, and daily reflections.</p></div></header>
          <div className="settings-fields">
            <Field label="Model" required hint="Enter any model ID accepted by your configured gateway.">
              <Input list="runtime-model-presets" value={codexModel} onChange={(_, value) => setCodexModel(value.value)} />
            </Field>
            <Field label="Reasoning effort" required>
              <Select value={codexEffort} onChange={(_, value) => setCodexEffort(value.value)}>
                {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
              </Select>
            </Field>
          </div>
        </section>

        <section className="settings-section">
          <header><div><h2>Pi Agent</h2><p>Pi is optional. Keep it aligned with Codex or choose an independent fallback.</p></div></header>
          <label className="settings-sync-row">
            <input type="checkbox" checked={syncPi} onChange={(event) => setSyncPi(event.target.checked)} />
            <span>Use the same model and reasoning effort as Codex</span>
          </label>
          {!syncPi && <div className="settings-fields">
            <Field label="Model" required><Input list="runtime-model-presets" value={piModel} onChange={(_, value) => setPiModel(value.value)} /></Field>
            <Field label="Thinking" required>
              <Select value={piThinking} onChange={(_, value) => setPiThinking(value.value)}>
                {efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
              </Select>
            </Field>
          </div>}
        </section>

        <datalist id="runtime-model-presets">
          {presets.map((model) => <option key={model} value={model} />)}
        </datalist>

        <footer className="settings-actions">
          <div>
            {save.isSuccess && <span className="settings-saved" role="status"><CheckmarkCircle20Regular /> Saved. New turns use this selection.</span>}
            {save.isError && <span className="settings-save-error" role="alert"><Warning20Regular /> {save.error.message}</span>}
            {data.updated_at && <small>Last changed {formatDateTime(data.updated_at, {}, "en-GB")}</small>}
          </div>
          <Button appearance="primary" type="submit" disabled={invalid || save.isPending}>
            {save.isPending ? "Saving" : "Save settings"}
          </Button>
        </footer>
      </form>
    </section>
  );
}
