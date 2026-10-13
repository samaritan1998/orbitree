import { Tab, TabList } from "@fluentui/react-components";
import { Clock20Regular, ServerMultiple20Regular } from "@fluentui/react-icons";
import type { Page } from "../router";
import ClusterDashboardView from "./ClusterDashboardView";
import LtpMonitorView from "./LtpMonitorView";

export default function ResourcesView({ section, onNavigate }: {
  section: "clusters" | "ltp-monitor";
  onNavigate: (page: Page) => void;
}) {
  return <div className="resources-page">
    <header className="resources-header"><h1>任务与资源</h1></header>
    <TabList
      aria-label="任务与资源"
      selectedValue={section}
      onTabSelect={(_, data) => onNavigate({ type: data.value === "clusters" ? "clusters" : "ltp-monitor" })}
    >
      <Tab id="resource-monitor-tab" value="ltp-monitor" icon={<Clock20Regular />} aria-controls="resource-panel">任务监控</Tab>
      <Tab id="resource-clusters-tab" value="clusters" icon={<ServerMultiple20Regular />} aria-controls="resource-panel">集群资源</Tab>
    </TabList>
    <div id="resource-panel" role="tabpanel" tabIndex={0} aria-labelledby={section === "clusters" ? "resource-clusters-tab" : "resource-monitor-tab"}>
      {section === "clusters" ? <ClusterDashboardView embedded /> : <LtpMonitorView embedded />}
    </div>
  </div>;
}
