import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          fluent: ["@fluentui/react-components", "@fluentui/react-icons"],
          graph: ["@xyflow/react", "@dagrejs/dagre"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8020",
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});
