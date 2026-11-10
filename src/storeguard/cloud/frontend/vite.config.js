import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Builds straight into ../static, which is what storeguard.cloud.app serves:
// GET "/" returns static/index.html directly, and "/static/*" is mounted for
// everything else — so base must be "/static/" for the built asset URLs to
// resolve, even though the built index.html itself is served from "/".
export default defineConfig(({ command }) => ({
  plugins: [vue()],
  // Built files are served from /static/; the dev server serves from / so
  // http://localhost:5173/overview (and refreshes) work.
  base: command === "build" ? "/static/" : "/",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  // `npm run dev` -> http://localhost:5173, talking to the backend on 8000
  // (`make serve`). /live is the detection view; ws: true lets its live
  // video WebSocket through too.
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/live": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
}));
