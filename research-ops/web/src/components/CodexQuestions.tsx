import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@fluentui/react-components";
import { api } from "../api";
import type { Approval } from "../types";

type Question = {
  id: string;
  header: string;
  question: string;
  options?: { label: string; description: string }[] | null;
  isOther?: boolean;
  isSecret?: boolean;
};

function isQuestion(value: unknown): value is Question {
  return typeof value === "object" && value !== null
    && typeof (value as Question).id === "string"
    && typeof (value as Question).question === "string";
}

function QuestionForm({ branchId, approval }: { branchId: string; approval: Approval }) {
  const client = useQueryClient();
  const questions = Array.isArray(approval.params.questions)
    ? approval.params.questions.filter(isQuestion) : [];
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const submit = useMutation({
    mutationFn: () => api.resolveApproval(approval.id, {
      answers: Object.fromEntries(questions.map((question) => [question.id, {
        answers: [selected[question.id] === "__other__"
          ? (custom[question.id] || "").trim()
          : (selected[question.id] || custom[question.id] || "").trim()],
      }])),
    }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["branch", branchId] }),
  });
  const complete = questions.length > 0 && questions.every((question) => {
    const choice = selected[question.id];
    return (choice === "__other__" ? custom[question.id] : choice || custom[question.id])?.trim();
  });
  return <form className="codex-question-form" onSubmit={(event) => { event.preventDefault(); if (complete) submit.mutate(); }}>
    {questions.map((question) => {
      const options = question.options || [];
      const inputId = `${approval.id}-${question.id}-answer`;
      const allowOther = !options.length || question.isOther !== false;
      return <fieldset key={question.id}>
        <legend>{question.header ? `${question.header} · ` : ""}{question.question}</legend>
        {options.map((option, index) => <label className="codex-question-option" key={`${option.label}-${index}`}>
          <input type="radio" name={`${approval.id}-${question.id}`} value={option.label}
            checked={selected[question.id] === option.label}
            onChange={() => setSelected((old) => ({ ...old, [question.id]: option.label }))} />
          <span><strong>{option.label}</strong><small>{option.description}</small></span>
        </label>)}
        {allowOther && <div className="codex-question-other">
          {options.length > 0 && <label className="codex-question-option">
            <input type="radio" name={`${approval.id}-${question.id}`} value="__other__"
              checked={selected[question.id] === "__other__"}
              onChange={() => setSelected((old) => ({ ...old, [question.id]: "__other__" }))} />
            <span>Other</span>
          </label>}
          <label htmlFor={inputId} className="sr-only">Answer for {question.question}</label>
          <input id={inputId} type={question.isSecret ? "password" : "text"} value={custom[question.id] || ""}
            placeholder="Your answer" autoComplete="off"
            onFocus={() => { if (options.length) setSelected((old) => ({ ...old, [question.id]: "__other__" })); }}
            onChange={(event) => setCustom((old) => ({ ...old, [question.id]: event.target.value }))} />
        </div>}
      </fieldset>;
    })}
    {questions.length === 0 && <p role="alert">Codex sent a question without a readable form. Answer it in the terminal.</p>}
    {submit.error && <p role="alert">{submit.error.message}</p>}
    <Button appearance="primary" type="submit" disabled={!complete || submit.isPending}>Send answer to Codex</Button>
  </form>;
}

export default function CodexQuestions({ branchId, approvals }: { branchId: string; approvals: Approval[] }) {
  const pending = approvals.filter((item) => item.status === "pending" && item.method === "item/tool/requestUserInput");
  if (!pending.length) return null;
  return <section className="codex-questions" aria-label="Codex questions" aria-live="polite">
    <strong>Codex is waiting for your answer</strong>
    {pending.map((item) => <QuestionForm key={item.id} branchId={branchId} approval={item} />)}
  </section>;
}
