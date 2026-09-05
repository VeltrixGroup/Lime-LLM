import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Relative base (not "/static/" like the cloud cabinet): this app is served
// two different ways — standalone at "/" (storeguard dashboard, port 8765)
// and proxied under "/live/" through the cloud cabinet (see
// storeguard/cloud/live_proxy.py). A relative "./assets/..." reference
// resolves correctly under either prefix, since the browser resolves it
// against whatever URL is actually in the address bar; an absolute
// "/assets/..." would break one of the two.
export default defineConfig({
  plugins: [vue()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
