import json


def starter(stack):
    files = {
        "README.md": "# Seu projeto\n\nFonte inicial do catálogo viroucaseai. Não foi executada na plataforma.\n\n## Desenvolvimento\nUse um ambiente isolado e revise dependências antes de instalar. Configure um D1 próprio, sem credenciais da plataforma. Aplique migrations/0001.sql pelo Wrangler e substitua o ID de exemplo na configuração. Para Hono: npm install e npm run dev. Para Python: uv sync e uv run pywrangler dev.\n\n## Publicação\nPublicar depende da validação de isolamento e de uma conta Cloudflare autorizada no plano previsto. Não inclua tokens no código. Este export contém fonte e schema; não contém dados do D1.\n",
        ".gitignore": "node_modules/\n.venv/\n.wrangler/\n.env\n.dev.vars\n",
        "migrations/0001.sql": "CREATE TABLE IF NOT EXISTS project_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);\nINSERT OR IGNORE INTO project_settings(key, value) VALUES ('title', 'Seu projeto');\n",
        "public/index.html": """<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Seu projeto</title>
    <link rel="stylesheet" href="/style.css" />
  </head>
  <body>
    <main>
      <p>Seu espaço para construir</p>
      <h1>Seu projeto</h1>
      <p id="status" role="status">Consultando conexão…</p>
    </main>
    <script src="/app.js" defer></script>
  </body>
</html>
""",
        "public/style.css": """body {
  margin: 0;
  background: #f8fafc;
  color: #11243d;
  font: 18px system-ui;
}
main {
  max-width: 800px;
  margin: 15vh auto;
  padding: 32px;
}
h1 {
  font-size: clamp(36px, 8vw, 72px);
}
""",
        "public/app.js": """fetch("/api/health")
  .then((r) => {
    if (!r.ok) throw Error();
    return r.json();
  })
  .then((d) => {
    document.getElementById("status").textContent = d.database
      ? "Backend e banco conectados."
      : "Backend disponível.";
  })
  .catch(() => {
    document.getElementById("status").textContent =
      "Não foi possível verificar a conexão.";
  });
""",
    }
    cfg = {
        "name": "seu-projeto",
        "compatibility_date": "2026-10-01",
        "assets": {"directory": "./public", "binding": "ASSETS", "run_worker_first": ["/api/*"]},
        "d1_databases": [
            {
                "binding": "DB",
                "database_name": "seu-projeto",
                "database_id": "00000000-0000-0000-0000-000000000000",
                "migrations_dir": "migrations",
            }
        ],
    }
    if stack == "hono-typescript-d1":
        cfg["main"] = "src/index.ts"
        files["src/index.ts"] = """import { Hono } from "hono";
type Bindings = { DB: D1Database; ASSETS: Fetcher };
const app = new Hono<{ Bindings: Bindings }>();
app.get("/api/health", async (c) => {
  await c.env.DB.prepare("SELECT 1").first();
  return c.json({ ok: true, database: true });
});
app.all("*", (c) => c.env.ASSETS.fetch(c.req.raw));
export default app;
"""
        files["package.json"] = json.dumps(
            {
                "name": "seu-projeto",
                "private": True,
                "type": "module",
                "scripts": {
                    "dev": "wrangler dev",
                    "deploy": "wrangler deploy",
                    "migrate": "wrangler d1 migrations apply seu-projeto --local",
                },
                "dependencies": {"hono": "4.12.9"},
                "devDependencies": {
                    "wrangler": "4.78.0",
                    "@cloudflare/workers-types": "4.20261026.0",
                    "typescript": "5.9.3",
                },
            },
            indent=2,
        )
    else:
        cfg.update(main="src/entry.py", compatibility_flags=["python_workers"])
        files["src/entry.py"] = (
            'from fastapi import FastAPI, Request\nfrom workers import asgi\n\napp = FastAPI()\nDefault = asgi.entrypoint(app)\n\n@app.get("/api/health")\nasync def health(request: Request):\n    env = request.scope["env"]\n    await env.DB.prepare("SELECT 1").first()\n    return {"ok": True, "database": True}\n'
        )
        files["pyproject.toml"] = (
            '[project]\nname = "seu-projeto"\nversion = "1.0.0"\nrequires-python = ">=3.12"\ndependencies = ["fastapi"]\n\n[dependency-groups]\ndev = ["workers-py"]\n'
        )
    files["wrangler.jsonc"] = json.dumps(cfg, indent=2)
    return files
