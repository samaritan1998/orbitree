import { useMemo } from "react";
import Dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import type { Branch, ProgramTree, WorkItem } from "../types";
import { operationalStatus } from "../status";

type TreeNodeData = {
  label: string;
  meta: string;
  status: string;
  kind: "program" | "work" | "branch";
  color: string;
  direction: "LR" | "TB";
  onSelect?: () => void;
};

const nodeTypes = { treeNode: TreeNode };

export default function TreeCanvas({ tree, selectedBranchId, onSelectBranch, compact = false }: {
  tree: ProgramTree;
  selectedBranchId?: string;
  onSelectBranch: (branchId: string) => void;
  compact?: boolean;
}) {
  const graph = useMemo(
    () => buildGraph(tree, selectedBranchId, onSelectBranch, compact),
    [tree, selectedBranchId, onSelectBranch, compact],
  );

  return (
    <div className="tree-canvas">
      <ReactFlow
        key={selectedBranchId ?? "program"}
        nodes={graph.nodes}
        edges={graph.edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        minZoom={0.35}
        maxZoom={1.45}
        proOptions={{ hideAttribution: true }}
        onlyRenderVisibleElements
      >
        <Background color="#d8d0ce" gap={22} size={1} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(node) => (node.data as TreeNodeData).color}
          maskColor="rgba(244, 241, 237, 0.78)"
        />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function TreeNode({ data, selected }: NodeProps<Node<TreeNodeData>>) {
  const vertical = data.direction === "TB";
  return (
    <button
      className={`tree-node tree-${data.kind}${selected ? " selected" : ""}`}
      style={{ "--node-color": data.color } as React.CSSProperties}
      onClick={data.onSelect}
    >
      <Handle type="target" position={vertical ? Position.Top : Position.Left} />
      <span className="tree-node-meta">{data.meta}</span>
      <strong>{data.label}</strong>
      <span className="tree-node-status">{data.status.replace("_", " ")}</span>
      <Handle type="source" position={vertical ? Position.Bottom : Position.Right} />
    </button>
  );
}

export function buildGraph(
  tree: ProgramTree,
  selectedBranchId: string | undefined,
  onSelectBranch: (branchId: string) => void,
  compact: boolean,
): { nodes: Node<TreeNodeData>[]; edges: Edge[] } {
  const nodes: Node<TreeNodeData>[] = [];
  const edges: Edge[] = [];
  const dagre = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  const direction = compact ? "TB" : "LR";
  dagre.setGraph({
    rankdir: direction,
    nodesep: compact ? 18 : 30,
    ranksep: compact ? 44 : 72,
    marginx: compact ? 24 : 40,
    marginy: compact ? 24 : 40,
  });

  const addNode = (id: string, data: TreeNodeData, width: number, height: number, selected = false) => {
    nodes.push({ id, type: "treeNode", data, position: { x: 0, y: 0 }, width, height, selected });
    dagre.setNode(id, { width, height });
  };
  const addEdge = (source: string, target: string, dashed = false) => {
    const id = `${source}:${target}`;
    edges.push({
      id,
      source,
      target,
      type: "smoothstep",
      animated: false,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "#8f8588" },
      style: { stroke: "#b7acad", strokeWidth: 1.4, strokeDasharray: dashed ? "5 5" : undefined },
    });
    dagre.setEdge(source, target);
  };

  const rootId = `program:${tree.program.id}`;
  addNode(rootId, {
    label: tree.program.name,
    meta: tree.program.code,
    status: `${tree.work_items.length} work items`,
    kind: "program",
    color: tree.program.color,
    direction,
  }, compact ? 150 : 190, compact ? 78 : 92);

  tree.work_items.forEach((work) => {
    const id = `work:${work.id}`;
    const main = tree.branches.find((branch) => branch.work_item_id === work.id && !branch.parent_branch_id);
    addNode(id, {
      label: work.title,
      meta: work.human_id,
      status: operationalStatus({ work }),
      kind: "work",
      color: tree.program.color,
      direction,
      onSelect: main ? () => onSelectBranch(main.id) : undefined,
    }, compact ? 174 : 220, compact ? 84 : 100, main?.id === selectedBranchId);
    addEdge(work.primary_parent_id ? `work:${work.primary_parent_id}` : rootId, id);
  });

  const visibleBranchIds = new Set(tree.branches.map((branch) => branch.id));
  const branchById = new Map(
    [...tree.branches, ...(tree.hidden_branches ?? [])].map((branch) => [branch.id, branch]),
  );
  tree.branches.filter((branch) => branch.parent_branch_id).forEach((branch) => {
    const id = `branch:${branch.id}`;
    const work = tree.work_items.find((item) => item.id === branch.work_item_id);
    const session = tree.sessions.find((item) => item.branch_id === branch.id);
    addNode(id, {
      label: branch.name,
      meta: branch.hidden ? "Hidden fork" : "Fork",
      status: branch.hidden
        ? "hidden"
        : work ? operationalStatus({ work, branch, session }) : branch.status,
      kind: "branch",
      color: branch.hidden
        ? "#a79da0"
        : branch.status === "active" ? tree.program.color : "#847b7e",
      direction,
      onSelect: () => onSelectBranch(branch.id),
    }, compact ? 156 : 184, compact ? 80 : 96, branch.id === selectedBranchId);
    addEdge(visibleParentNode(branch, visibleBranchIds, branchById), id);
  });

  tree.edges.forEach((edge) => addEdge(`work:${edge.from_work_item_id}`, `work:${edge.to_work_item_id}`, true));
  Dagre.layout(dagre);
  nodes.forEach((node) => {
    const position = dagre.node(node.id);
    const width = node.width ?? 180;
    const height = node.height ?? 80;
    node.position = { x: position.x - width / 2, y: position.y - height / 2 };
  });
  return { nodes, edges };
}

function visibleParentNode(
  branch: Branch,
  visibleBranchIds: Set<string>,
  branchById: Map<string, Branch>,
): string {
  const visited = new Set<string>();
  let parentId = branch.parent_branch_id;
  while (parentId && !visited.has(parentId)) {
    visited.add(parentId);
    const parent = branchById.get(parentId);
    if (!parent) break;
    if (visibleBranchIds.has(parent.id)) {
      return parent.parent_branch_id
        ? `branch:${parent.id}`
        : `work:${branch.work_item_id}`;
    }
    parentId = parent.parent_branch_id;
  }
  return `work:${branch.work_item_id}`;
}

export function primaryBranch(work: WorkItem, branches: Branch[]): Branch | undefined {
  return branches.find((branch) => branch.work_item_id === work.id && !branch.parent_branch_id);
}
