import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Everything is same-origin through the dev server; all targets are 127.0.0.1.
const to = (port: number, prefix: string) => ({
  target: `http://127.0.0.1:${port}`,
  changeOrigin: true,
  rewrite: (p: string) => p.replace(new RegExp(`^${prefix}`), ""),
});

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/gw": to(8080, "/gw"),
      "/atk": to(8081, "/atk"),
      "/ctl": to(8082, "/ctl"),
      "/agent": to(8090, "/agent"),
      "/prom": to(9090, "/prom"),
    },
  },
});
