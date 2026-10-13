import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { ProgramTree } from "../types";
import ProgramList from "./ProgramList";

const tree = {
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
      slug: "alternative",
      name: "Alternative verifier",
      status: "active",
      mode: "write",
    },
  ],
  sessions: [],
} as unknown as ProgramTree;

test("lists the primary path as the WorkItem and only names real forks", () => {
  render(<ProgramList tree={tree} selectedBranchId="main-1" onSelectBranch={vi.fn()} />);

  expect(screen.queryByText("Main")).not.toBeInTheDocument();
  expect(screen.getByText("Explore an idea")).toBeInTheDocument();
  expect(screen.getByText("Alternative verifier")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Explore an idea/ })).toHaveClass("selected");
});
