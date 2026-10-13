import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, test, vi } from "vitest";
import ForkDialog from "./ForkDialog";

test("creates a parallel branch by default", async () => {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => Promise.resolve(
    new Response(JSON.stringify({
      id: "child-1",
      work_item_id: "work-1",
      parent_branch_id: "parent-1",
      name: "Parallel idea",
      slug: "parallel-idea",
      status: "active",
      mode: "write",
      context_snapshot: { provisioning: { state: "ready" } },
      memory_cursor: 0,
      created_at: "2026-07-20T00:00:00Z",
      updated_at: "2026-07-20T00:00:00Z",
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  ));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onCreated = vi.fn();

  render(
    <QueryClientProvider client={client}>
      <ForkDialog
        branchId="parent-1"
        open
        onClose={vi.fn()}
        onCreated={onCreated}
      />
    </QueryClientProvider>,
  );

  expect(screen.getByRole("combobox", { name: "After fork" })).toHaveValue("parallel");
  fireEvent.change(screen.getByRole("textbox", { name: /Branch name/ }), {
    target: { value: "Parallel idea" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /Objective/ }), {
    target: { value: "Explore this without closing the parent branch." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create parallel branch" }));

  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("child-1", "parallel"));
  const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
  expect(JSON.parse(String(request.body))).toMatchObject({
    continuation: "parallel",
    start_session: false,
    manual_approval: true,
  });
});
