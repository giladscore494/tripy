/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The FastAPI backend (uvicorn src.api.app:app) during development. The SPA calls relative /api and /health URLs, so
// the dev proxy needs no CORS and the production bundle (served by FastAPI in PR #3) works unchanged.
const backend = process.env.TRIPY_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/health": { target: backend, changeOrigin: false },
    },
  },
  preview: {
    proxy: {
      "/api": { target: backend, changeOrigin: false },
      "/health": { target: backend, changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
