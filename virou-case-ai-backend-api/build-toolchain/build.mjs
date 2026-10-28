import { build } from "esbuild";
import fs from "node:fs";
import path from "node:path";
const [stack, revision] = process.argv.slice(2);
const endpoint = "/api/__viroucaseai_revision";
const modules = {};
if (stack === "hono-typescript-d1") {
  const dependencies =
    JSON.parse(fs.readFileSync("/project/package.json", "utf8")).dependencies ||
    {};
  if (Object.keys(dependencies).some((name) => name !== "hono"))
    throw Error("dependency_outside_catalog");
  const entry = `import app from '/project/src/index.ts';
export default { async fetch(request, env, ctx) {
  const url = new URL(request.url);
  if (url.pathname === '${endpoint}') {
    await env.DB.prepare('SELECT 1').first();
    return Response.json({revision_id:${JSON.stringify(revision)},database:true});
  }
  if (url.pathname.startsWith('/api/')) {
    const response = await app.fetch(request, env, ctx);
    if ((response.headers.get('content-type') || '').includes('text/html'))
      return Response.json({error:'API_ROUTE_NOT_FOUND'}, {status:404});
    return response;
  }
  return env.ASSETS.fetch(request);
}};`;
  const result = await build({
    stdin: {
      contents: entry,
      resolveDir: "/project",
      sourcefile: "worker.ts",
      loader: "ts",
    },
    bundle: true,
    write: false,
    platform: "browser",
    format: "esm",
    target: "es2022",
    logLevel: "silent",
    nodePaths: ["/toolchain/node_modules"],
    plugins: [
      {
        name: "authorized-imports",
        setup(builder) {
          builder.onResolve({ filter: /^(node:|https?:|data:)/ }, () => ({
            errors: [{ text: "Import outside catalog" }],
          }));
        },
      },
    ],
  });
  modules["worker.js"] = {
    content: result.outputFiles[0].text,
    mime: "application/javascript+module",
  };
  process.stdout.write(
    JSON.stringify({
      main_module: "worker.js",
      compatibility_flags: [],
      modules,
    }),
  );
} else if (stack === "fastapi-python-d1") {
  const vendor = "/toolchain/python-vendor.json";
  if (!fs.existsSync(vendor)) throw Error("python_vendor_not_qualified");
  Object.assign(modules, JSON.parse(fs.readFileSync(vendor, "utf8")));
  function collect(directory) {
    for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
      const full = path.join(directory, item.name);
      if (item.isSymbolicLink()) throw Error("symlink");
      if (item.isDirectory()) collect(full);
      else if (item.name.endsWith(".py"))
        modules[path.relative("/project/src", full)] = {
          content: fs.readFileSync(full, "utf8"),
          mime: "text/x-python",
        };
    }
  }
  collect("/project/src");
  if (!modules["entry.py"]) throw Error("python_entrypoint_missing");
  modules["__publication.py"] = {
    mime: "text/x-python",
    content: `from entry import app
from workers import asgi
from starlette.responses import JSONResponse

@app.middleware("http")
async def publication_routes(request, call_next):
    if request.url.path == "${endpoint}":
        await request.scope["env"].DB.prepare("SELECT 1").first()
        return JSONResponse({"revision_id": ${JSON.stringify(revision)}, "database": True})
    response = await call_next(request)
    if request.url.path.startswith("/api/") and "text/html" in response.headers.get("content-type", ""):
        return JSONResponse({"error": "API_ROUTE_NOT_FOUND"}, status_code=404)
    return response

Default = asgi.entrypoint(app)
`,
  };
  process.stdout.write(
    JSON.stringify({
      main_module: "__publication.py",
      compatibility_flags: ["python_workers", "python_workers_314"],
      modules,
    }),
  );
} else throw Error("unsupported_stack");
