import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Skeleton, SkeletonItem } from "@fluentui/react-components";
import { Archive20Regular, BranchFork20Regular, Checkmark20Regular, Dismiss20Regular } from "@fluentui/react-icons";
import { api } from "../api";
import { formatDateTime } from "../time";

export default function MergeView() {
  const queryClient = useQueryClient();
  const queue = useQuery({ queryKey: ["merge-queue"], queryFn: api.mergeQueue });
  const resolve = useMutation({
    mutationFn: ({ id, resolution }: { id: string; resolution: string }) => api.resolveMerge(id, resolution),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["merge-queue"] }),
  });

  return (
    <div className="page review-page">
      <header className="page-header">
        <div><h1>Merge Queue</h1><p>Branch results waiting for a parent decision.</p></div>
        <span className="page-count">{queue.data?.length ?? 0}</span>
      </header>
      {queue.isLoading && <Skeleton><SkeletonItem /><SkeletonItem /></Skeleton>}
      {queue.error && <div className="page-error">{queue.error.message}</div>}
      {!queue.isLoading && !queue.data?.length && <div className="empty-state"><BranchFork20Regular /><strong>Queue clear</strong><span>No branch results need review.</span></div>}
      <div className="merge-list">
        {queue.data?.map((review) => (
          <article className="merge-row" key={review.id}>
            <div className="merge-branch-map">
              <code>{review.source_branch_id.slice(0, 8)}</code>
              <BranchFork20Regular />
              <code>{review.target_branch_id.slice(0, 8)}</code>
            </div>
            <div className="merge-copy">
              <strong>Branch result ready</strong>
              <span>{formatDateTime(review.created_at)}</span>
            </div>
            <div className="merge-actions">
              <Button icon={<Dismiss20Regular />} onClick={() => resolve.mutate({ id: review.id, resolution: "abandon" })}>Abandon</Button>
              <Button icon={<Archive20Regular />} onClick={() => resolve.mutate({ id: review.id, resolution: "park" })}>Park</Button>
              <Button appearance="primary" icon={<Checkmark20Regular />} onClick={() => resolve.mutate({ id: review.id, resolution: "merge" })}>Merge</Button>
            </div>
          </article>
        ))}
      </div>
      {resolve.error && <div className="inline-error">{resolve.error.message}</div>}
    </div>
  );
}
