import { defineConfig } from "vite";
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    allowedHosts: ["127.0.0.1", "localhost"],
    proxy: {
      "/admin-api": { target: "http://127.0.0.1:8001", changeOrigin: false },
    },
  },
  build: { sourcemap: false },
});
