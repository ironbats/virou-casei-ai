import { defineConfig } from "vite";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { previewHtml } from "./scripts/previewBundle";

function previewMiddleware(production: boolean) {
  return async (
    request: { url?: string },
    response: import("node:http").ServerResponse,
    next: () => void,
  ) => {
    if (request.url?.split("?")[0] !== "/preview.html") return next();
    try {
      const nonce = randomBytes(16).toString("hex");
      const config = await readFile("nginx.conf", "utf8");
      const policy = [
        ...config.matchAll(
          /add_header Content-Security-Policy "([^"]+)" always;/g,
        ),
      ][1]?.[1];
      if (!policy) throw Error("Política da prévia ausente.");
      const html = production
        ? await readFile("dist/preview.html", "utf8")
        : await previewHtml();
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.setHeader(
        "Content-Security-Policy",
        policy.replaceAll("$request_id", nonce),
      );
      response.setHeader("Cache-Control", "no-store");
      response.setHeader("X-Content-Type-Options", "nosniff");
      response.end(html.replaceAll("__VIRA_CSP_NONCE__", nonce));
    } catch {
      response.statusCode = 503;
      response.end("Não foi possível preparar a prévia.");
    }
  };
}

export default defineConfig({
  plugins: [
    {
      name: "isolated-project-preview",
      configureServer(server) {
        server.middlewares.use(previewMiddleware(false));
      },
      configurePreviewServer(server) {
        server.middlewares.use(previewMiddleware(true));
      },
    },
  ],
  server: {
    headers: {
      "Content-Security-Policy":
        "frame-src 'self'; object-src 'none'; base-uri 'self'",
    },
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  preview: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    headers: {
      "Content-Security-Policy":
        "frame-src 'self'; object-src 'none'; base-uri 'self'",
    },
  },
  build: { sourcemap: false },
});
