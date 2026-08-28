import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api to the FastAPI app so the browser sees one
// origin during development, matching how the built bundle is served
// (FastAPI mounts dist/ at "/" -- see reviewer/api.py).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // Matches tsconfig.json's "paths" so shadcn's `@/components/ui/...`
    // convention resolves identically to the type checker and the bundler.
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
