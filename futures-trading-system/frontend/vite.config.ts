/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is served by the Python backend (same origin). `npm run dev` proxies the
// API to a terminal started with --dev on 127.0.0.1:8765.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./test/setup.ts"],
    css: false,
  },
});
