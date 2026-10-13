import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import ProgramSettings from "./ProgramSettings";

const program = { id: "one", code: "ML", slug: "ml", name: "Research", objective: "", color: "#123456", active: true, sort_order: 10 };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function show() {
  vi.spyOn(api, "allPrograms").mockResolvedValue([program]);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><ProgramSettings /></QueryClientProvider>);
}
test("creates arbitrary directions instead of the fixed catalog", async () => {
  const save = vi.spyOn(api, "createProgram").mockResolvedValue(program);
  show();
  fireEvent.click(screen.getByRole("button", { name: "新增方向" }));
  fireEvent.change(screen.getByLabelText(/方向代码/), { target: { value: "custom" } });
  fireEvent.change(screen.getByLabelText(/方向名称/), { target: { value: "Custom research" } });
  fireEvent.click(screen.getByRole("button", { name: "保存方向" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ code: "CUSTOM", name: "Custom research" })));
});
test("editing never changes the directory code", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: "编辑 Research" }));
  expect(screen.getByLabelText(/方向代码/)).toBeDisabled();
});
test("archive requires confirmation and does not delete data", async () => {
  const save = vi.spyOn(api, "updateProgram").mockResolvedValue({ ...program, active: false });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  show();
  fireEvent.click(await screen.findByRole("button", { name: "归档" }));
  expect(save).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "归档" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("ML", expect.objectContaining({ active: false })));
});
