import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev server on 5173 with a same-origin `/api` proxy to the FastAPI backend.
 *
 * The proxy is the primary path: the browser only ever talks to
 * http://localhost:5173, so requests are same-origin and CORS never enters the
 * picture (`changeOrigin` rewrites the Host header to the target). The
 * backend's CORS allow-list for 5173/3000 keeps working for anyone who points
 * the client straight at http://localhost:8000 via `VITE_API_BASE`.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 5173,
    strictPort: true,
  },
});
