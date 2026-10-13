import { ArrowRight20Regular, BranchFork20Regular } from "@fluentui/react-icons";
import { operationalStatus } from "../status";
import type { ProgramTree } from "../types";

export default function ProgramList({ tree, selectedBranchId, onSelectBranch }: {
  tree: ProgramTree;
  selectedBranchId?: string;
  onSelectBranch: (branchId: string) => void;
}) {
  return <div className="program-list-view">
    <div className="program-list-head"><span>ID / Branch</span><span>Title</span><span>Status</span><span /></div>
    {tree.work_items.map((work) => {
      const branches = tree.branches.filter((branch) => branch.work_item_id === work.id);
      const main = branches.find((branch) => !branch.parent_branch_id);
      const forks = branches.filter((branch) => branch.parent_branch_id);
      return <section key={work.id}>
        <button
          className={main?.id === selectedBranchId ? "program-list-work selected" : "program-list-work"}
          disabled={!main}
          onClick={() => main && onSelectBranch(main.id)}
        >
          <span className="work-id">{work.human_id}</span><strong>{work.title}</strong>
          <span className="status-text">{operationalStatus({ work })}</span><ArrowRight20Regular aria-hidden="true" />
        </button>
        {forks.map((branch) => <button
          className={`program-list-branch${branch.id === selectedBranchId ? " selected" : ""}${branch.hidden ? " hidden" : ""}`}
          key={branch.id}
          onClick={() => onSelectBranch(branch.id)}
        >
          <span><BranchFork20Regular />{branch.slug}</span><strong>{branch.name}</strong>
          <span className="status-text">{branch.hidden ? "Hidden" : operationalStatus({ work, branch, session: tree.sessions.find((item) => item.branch_id === branch.id) })}</span><ArrowRight20Regular aria-hidden="true" />
        </button>)}
      </section>;
    })}
    {!tree.work_items.length && <div className="empty-row">No work in this Program</div>}
  </div>;
}
