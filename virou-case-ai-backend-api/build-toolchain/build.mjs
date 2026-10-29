import { build } from "esbuild";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
const [stack, revision] = process.argv.slice(2);
const projectRoot = process.cwd();
const toolchainRoot = path.dirname(fileURLToPath(import.meta.url));
const endpoint = "/api/__viroucaseai_revision";
const modules = {};
const diagnosticBytes = 8192;

function reportFailure(error) {
  const failures = Array.isArray(error.errors) ? error.errors : [error];
  const diagnostic = {
    viracase_build_diagnostic: 1,
    tool: stack === "hono-typescript-d1" ? "esbuild" : "executor",
    issues: [],
    output_truncated: failures.length > 12,
  };
  for (const item of failures.slice(0, 12)) {
    const message = String(item.text || item.message || "Build failed");
    const characters = Array.from(message);
    const issue = {
      code:
        typeof item.id === "string" && item.id.length <= 80
          ? item.id
          : undefined,
      message: characters.slice(0, 800).join(""),
    };
    if (characters.length > 800) diagnostic.output_truncated = true;
    const location = item.location;
    if (location && typeof location.file === "string") {
      const filename = path.relative(
        projectRoot,
        path.resolve(projectRoot, location.file),
      );
      if (filename.length <= 200) issue.path = filename;
      else diagnostic.output_truncated = true;
      if (Number.isInteger(location.line) && location.line > 0)
        issue.line = location.line;
      if (Number.isInteger(location.column) && location.column >= 0) {
        issue.column =
          typeof location.lineText === "string"
            ? Array.from(
                Buffer.from(location.lineText, "utf8")
                  .subarray(0, location.column)
                  .toString("utf8"),
              ).length + 1
            : location.column + 1;
      }
    }
    const candidate = { ...diagnostic, issues: [...diagnostic.issues, issue] };
    if (
      Buffer.byteLength(JSON.stringify(candidate) + "\n", "utf8") >
      diagnosticBytes
    ) {
      diagnostic.output_truncated = true;
      break;
    }
    diagnostic.issues.push(issue);
  }
  process.stderr.write(JSON.stringify(diagnostic) + "\n");
  process.exitCode = 1;
}

try {
  if (stack === "hono-typescript-d1") {
    let packageManifest;
    try {
      packageManifest = JSON.parse(
        fs.readFileSync(path.join(projectRoot, "package.json"), "utf8"),
      );
    } catch {
      throw {
        id: "INVALID_PACKAGE_JSON",
        message: "Não foi possível ler um package.json válido.",
        location: { file: "package.json" },
      };
    }
    const dependencies = packageManifest?.dependencies || {};
    if (
      typeof dependencies !== "object" ||
      Array.isArray(dependencies) ||
      Object.keys(dependencies).some((name) => name !== "hono")
    )
      throw {
        id: "DEPENDENCY_OUTSIDE_CATALOG",
        message:
          "O package.json contém dependências fora do catálogo aprovado da stack Hono.",
        location: { file: "package.json" },
      };
    const entry = `import app from ${JSON.stringify(path.join(projectRoot, "src/index.ts"))};
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
        resolveDir: projectRoot,
        sourcefile: "worker.ts",
        loader: "ts",
      },
      bundle: true,
      write: false,
      platform: "browser",
      format: "esm",
      target: "es2022",
      logLevel: "silent",
      nodePaths: [path.join(toolchainRoot, "node_modules")],
      plugins: [
        {
          name: "authorized-imports",
          setup(builder) {
            builder.onResolve({ filter: /^(node:|https?:|data:)/ }, () => ({
              errors: [
                {
                  id: "IMPORT_OUTSIDE_CATALOG",
                  text: "Import outside catalog",
                },
              ],
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
    const vendor = path.join(toolchainRoot, "python-vendor.json");
    if (!fs.existsSync(vendor)) throw Error("python_vendor_not_qualified");
    Object.assign(modules, JSON.parse(fs.readFileSync(vendor, "utf8")));
    function collect(directory) {
      for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
        const full = path.join(directory, item.name);
        if (item.isSymbolicLink()) throw Error("symlink");
        if (item.isDirectory()) collect(full);
        else if (item.name.endsWith(".py"))
          modules[path.relative(path.join(projectRoot, "src"), full)] = {
            content: fs.readFileSync(full, "utf8"),
            mime: "text/x-python",
          };
      }
    }
    collect(path.join(projectRoot, "src"));
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
} catch (error) {
  reportFailure(error);
}
