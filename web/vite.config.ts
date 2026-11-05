import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI is served by the API (same origin): no CORS, CSP 'self' only.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
