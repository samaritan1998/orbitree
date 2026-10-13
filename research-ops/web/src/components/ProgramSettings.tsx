import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Field, Input, Textarea } from "@fluentui/react-components";
import { Add20Regular, Edit20Regular, Save20Regular, Dismiss20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import type { Program } from "../types";

const EMPTY = { code: "", name: "", objective: "", color: "#32766d", sort_order: 50, active: true };
export default function ProgramSettings() {
  const client = useQueryClient();
  const programs = useQuery({ queryKey: ["programs-all"], queryFn: api.allPrograms });
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [notice, setNotice] = useState("");
  const saved = async () => {
    setEditing(null); setNotice("工作方向已保存");
    await client.invalidateQueries({ queryKey: ["programs"] });
    await client.invalidateQueries({ queryKey: ["programs-all"] });
  };
  const save = useMutation({ mutationFn: () => {
    const { code, active, ...fields } = form;
    return editing ? api.updateProgram(editing, { ...fields, active }) : api.createProgram({ code, ...fields });
  }, onSuccess: saved });
  const archive = useMutation({ mutationFn: (program: Program) => api.updateProgram(program.code, {
    name: program.name, objective: program.objective, color: program.color,
    sort_order: program.sort_order, active: !program.active,
  }), onSuccess: saved });
  return <section className="settings-section program-settings">
    <header><h2>工作方向</h2><Button icon={<Add20Regular />} onClick={() => { setForm(EMPTY); setEditing(""); save.reset(); setNotice(""); }}>新增方向</Button></header>
    <div className="program-settings-list">{programs.data?.map((program) => <div className="program-settings-row" key={program.code}>
      <span className="program-color-dot" style={{ background: program.color }} />
      <div><strong>{program.name}</strong><small>{program.code} · {program.active ? "已启用" : "已归档"}</small></div>
      <Button icon={<Edit20Regular />} aria-label={`编辑 ${program.name}`} onClick={() => { setForm(program); setEditing(program.code); save.reset(); setNotice(""); }} />
      <Button disabled={archive.isPending} onClick={() => {
        if (!program.active || window.confirm(`归档「${program.name}」？任务和文件会保留，可以随时恢复。`)) archive.mutate(program);
      }}>{program.active ? "归档" : "恢复"}</Button>
    </div>)}</div>
    {editing !== null && <form className="program-editor" onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
      <div className="settings-fields">
        <Field label="方向代码" required><Input disabled={Boolean(editing)} value={form.code} onChange={(_, data) => setForm({ ...form, code: data.value.toUpperCase() })} maxLength={16} placeholder="RESEARCH" /></Field>
        <Field label="方向名称" required><Input value={form.name} maxLength={160} onChange={(_, data) => setForm({ ...form, name: data.value })} /></Field>
        <Field label="颜色"><input type="color" aria-label="方向颜色" value={form.color} onChange={(event) => setForm({ ...form, color: event.target.value })} /></Field>
        <Field label="排序"><Input type="number" min={0} max={10000} value={String(form.sort_order)} onChange={(_, data) => setForm({ ...form, sort_order: Number(data.value) })} /></Field>
      </div>
      <Field label="目标（选填）"><Textarea value={form.objective} onChange={(_, data) => setForm({ ...form, objective: data.value })} /></Field>
      <div className="program-editor-actions"><Button type="submit" appearance="primary" icon={<Save20Regular />} disabled={save.isPending || !/^[A-Z][A-Z0-9]{1,15}$/.test(form.code) || !form.name.trim()}>保存方向</Button><Button icon={<Dismiss20Regular />} onClick={() => setEditing(null)}>取消</Button></div>
    </form>}
    {(programs.error || save.error || archive.error) && <p role="alert">{(programs.error || save.error || archive.error)?.message}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
