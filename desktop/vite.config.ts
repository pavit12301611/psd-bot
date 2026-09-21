import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const host = process.env.TAURI_DEV_HOST;

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || "0.0.0.0",
    allowedHosts: true,
    hmr: host ? { protocol: "ws", host, port: 1421 } : undefined,
    watch: { ignored: ["**/src-tauri/**"] },
    // Browser-only UI development (no Tauri shell): forward API calls to a
    // manually started `python desktop_server.py` (PSD_AI_DEV_BACKEND=port).
    proxy: process.env.PSD_AI_DEV_BACKEND
      ? { "/api": { target: `http://127.0.0.1:${process.env.PSD_AI_DEV_BACKEND}`, changeOrigin: false } }
      : undefined,
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    // Linux-only: Tauri's WebView here is WebKitGTK, whose JS engine tracks
    // Safari, so the Safari target is the one that matters (chrome105 was the
    // Windows WebView2 target and is dead with the Windows build).
    target: "safari13",
    minify: !process.env.TAURI_ENV_DEBUG,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
});
