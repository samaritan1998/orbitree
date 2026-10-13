import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Tooltip } from "@fluentui/react-components";
import { ArrowSync20Regular, ChevronDown20Regular, ChevronUp20Regular, Open16Regular, Organization20Regular } from "@fluentui/react-icons";
import { api } from "../api";

export default function SessionWorkflowPanel({ branchId }: { branchId: string }) {
  const [expanded, setExpanded] = useState(false);
  const query = useQuery({
    queryKey: ["workflow-links", branchId], queryFn: () => api.workflowLinks(branchId),
    staleTime: 30_000, refetchInterval: 30_000,
    refetchIntervalInBackground: false, retry: 1,
  });
  const links = query.data?.links ?? [];
  if (!links.length && !query.isError) return null;
  return <section className="session-workflow workflow-links-panel" aria-label="Session workflows">
    <div className="workflow-links-heading">
      <Organization20Regular /><strong>Workflows</strong>
      {links.length > 2 && <Tooltip content={expanded ? "Collapse workflows" : "Show all workflows"} relationship="label">
        <Button size="small" appearance="subtle" icon={expanded ? <ChevronUp20Regular /> : <ChevronDown20Regular />}
          aria-label={expanded ? "Collapse workflows" : "Show all workflows"}
          aria-expanded={expanded} aria-controls={`workflow-links-${branchId}`} onClick={() => setExpanded(!expanded)}>{links.length}</Button>
      </Tooltip>}
      <Tooltip content="Refresh workflow links" relationship="label"><Button
        size="small" appearance="subtle" icon={<ArrowSync20Regular />} aria-label="Refresh workflow links"
        disabled={query.isFetching} onClick={() => void query.refetch()} />
      </Tooltip>
    </div>
    {query.isError && <div className="workflow-error" role="alert">Workflow links unavailable: {query.error.message}</div>}
    <ul className="workflow-links" id={`workflow-links-${branchId}`}>
      {(expanded ? links : links.slice(0, 2)).map((link) => <li key={link.url}>
        <a href={link.url} target="_blank" rel="noopener noreferrer" title={link.title}>
          <span>{link.title}</span><Open16Regular aria-hidden="true" />
        </a>
      </li>)}
    </ul>
  </section>;
}
