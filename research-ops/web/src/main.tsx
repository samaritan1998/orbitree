import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { FluentProvider } from "@fluentui/react-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import App from "./App";
import { researchOpsTheme } from "./theme";
import { TeamBoundary } from "./team";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, retry: 1 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FluentProvider theme={researchOpsTheme}>
      <QueryClientProvider client={queryClient}>
        <TeamBoundary><App /></TeamBoundary>
      </QueryClientProvider>
    </FluentProvider>
  </StrictMode>,
);
