import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@fluentui/react-components";
import { Checkmark20Regular, Dismiss20Regular, Shield20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import type { Approval } from "../types";

export default function ClaudeApprovals({ branchId, approvals }: { branchId: string; approvals: Approval[] }) {
  const client = useQueryClient();
  const resolve = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) => api.resolveApproval(id, decision),
    onSuccess: () => client.invalidateQueries({ queryKey: ["branch", branchId] }),
  });
  const pending = approvals.filter((item) => item.status === "pending" && item.method === "claude/tool/requestApproval");
  if (!pending.length) return null;
  return <section className="claude-approvals" aria-label="Claude permissions" aria-live="polite">
    {pending.map((item) => <div className="claude-approval" key={item.id}>
      <strong><Shield20Regular /> Permission required · {String(item.params.tool || "Claude tool")}</strong>
      {item.params.blocked_path ? <code>{String(item.params.blocked_path)}</code> : null}
      <pre>{JSON.stringify(item.params.input || {}, null, 2)}</pre>
      <div className="claude-approval-actions">
        <Button size="small" icon={<Dismiss20Regular />} disabled={resolve.isPending} onClick={() => resolve.mutate({ id: item.id, decision: "decline" })}>Deny</Button>
        <Button size="small" appearance="primary" icon={<Checkmark20Regular />} disabled={resolve.isPending} onClick={() => resolve.mutate({ id: item.id, decision: "accept" })}>Allow once</Button>
      </div>
    </div>)}
    {resolve.error && <p role="alert">{resolve.error.message}</p>}
  </section>;
}
