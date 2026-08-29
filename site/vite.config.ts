import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Deployed to Vercel from site/ as a plain static SPA, so the base is the
// domain root. It stays overridable because GitHub Pages would serve this
// from /<repo>/ instead, and a wrong base 404s every asset silently:
//
//   SITE_BASE=/assay/ npm run build
export default defineConfig({
  base: process.env.SITE_BASE ?? "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    // Matches tsconfig's "paths" so the shadcn `@/components/ui/...`
    // convention resolves identically for the type checker and the bundler.
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
