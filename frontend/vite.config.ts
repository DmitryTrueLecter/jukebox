import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` talks to the real Pi; override with JUKEBOX_URL=http://host:port
const target = process.env.JUKEBOX_URL ?? "http://jukebox.local:8080";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target, changeOrigin: true } } },
  build: { outDir: "dist", emptyOutDir: true },
});