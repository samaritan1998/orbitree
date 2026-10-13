import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: process.env.CANARY_VIEWER_OUT || "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
