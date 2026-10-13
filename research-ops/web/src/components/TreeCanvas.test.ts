import { expect, test, vi } from "vitest";
import type { ProgramTree } from "../types";
import { buildGraph } from "./TreeCanvas";

const tree = {
  program: { id: "program-1", code: "FREE", name: "Free Work", color: "#a21caf" },
  work_items: [{
    id: "work-1",
    human_id: "FREE-0001",
    title: "Explore an idea",
    lifecycle: "active",
    flow: "actionable",
  }],
  branches: [
    { id: "main-1", work_item_id: "work-1", name: "Main", status: "active", mode: "write" },
    {
      id: "fork-1",
      work_item_id: "work-1",
      parent_branch_id: "main-1",
      name: "Alternative verifier",
      status: "active",
      mode: "write",
    },
  ],
  sessions: [],
  edges: [],
} as unknown as ProgramTree;

test("collapses the primary branch into its WorkItem node", () => {
  const graph = buildGraph(tree, "main-1", vi.fn(), false);

  expect(graph.nodes.map((node) => node.id)).toEqual([
    "program:program-1",
    "work:work-1",
    "branch:fork-1",
  ]);
  expect(graph.nodes.find((node) => node.id === "work:work-1")?.selected).toBe(true);
  expect(graph.edges.map((edge) => [edge.source, edge.target])).toContainEqual([
    "work:work-1",
    "branch:fork-1",
  ]);
});

test("connects a visible descendant across a hidden intermediary", () => {
  const hiddenTree = {
    ...tree,
    branches: [
      tree.branches[0],
      {
        id: "fork-2",
        work_item_id: "work-1",
        parent_branch_id: "hidden-fork",
        name: "Visible descendant",
        status: "active",
        hidden: false,
        mode: "write",
      },
    ],
    hidden_branches: [{
      id: "hidden-fork",
      work_item_id: "work-1",
      parent_branch_id: "main-1",
      name: "Empty intermediary",
      status: "active",
      hidden: true,
      mode: "write",
    }],
  } as unknown as ProgramTree;

  const graph = buildGraph(hiddenTree, undefined, vi.fn(), false);

  expect(graph.nodes.map((node) => node.id)).not.toContain("branch:hidden-fork");
  expect(graph.edges.map((edge) => [edge.source, edge.target])).toContainEqual([
    "work:work-1",
    "branch:fork-2",
  ]);
});
