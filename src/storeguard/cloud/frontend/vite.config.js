import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Builds straight into ../static, which is what storeguard.cloud.app serves:
// GET "/" returns static/index.html directly, and "/static/*" is mounted for
// everything else — so base must be "/static/" for the built asset URLs to
// resolve, even though the built index.html itself is served from "/".
export default defineConfig({
  plugins: [vue()],
  base: "/static/",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
