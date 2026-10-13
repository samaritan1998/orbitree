import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import ResourcesView from "./ResourcesView";

vi.mock("./ClusterDashboardView", () => ({ default: () => <p>Cluster inventory</p> }));
vi.mock("./LtpMonitorView", () => ({ default: () => <p>LTP tasks</p> }));
afterEach(cleanup);

test("mounts only the selected resource page and retains legacy routes", () => {
  const onNavigate = vi.fn();
  const { rerender } = render(<ResourcesView section="ltp-monitor" onNavigate={onNavigate} />);
  expect(screen.getByRole("heading", { name: "任务与资源" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "任务监控" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tabpanel", { name: "任务监控" })).toHaveTextContent("LTP tasks");
  expect(screen.queryByText("Cluster inventory")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "集群资源" }));
  expect(onNavigate).toHaveBeenCalledWith({ type: "clusters" });
  rerender(<ResourcesView section="clusters" onNavigate={onNavigate} />);
  expect(screen.getByRole("tabpanel", { name: "集群资源" })).toHaveTextContent("Cluster inventory");
  expect(screen.queryByText("LTP tasks")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: "任务监控" }));
  expect(onNavigate).toHaveBeenLastCalledWith({ type: "ltp-monitor" });
});
