import { readFile } from "node:fs/promises";
import { build } from "vite";

export async function previewHtml() {
  const result = await build({
    configFile: false,
    publicDir: false,
    logLevel: "error",
    build: {
      write: false,
      sourcemap: false,
      lib: {
        entry: "src/preview/runtime.ts",
        name: "ViraPreview",
        formats: ["iife"],
      },
    },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!("output" in bundle)) throw Error("Não foi possível preparar a prévia.");
  const script = bundle.output.find((entry) => entry.type === "chunk");
  if (!script) throw Error("A prévia não gerou seu runtime.");
  const template = await readFile("public/preview.html", "utf8");
  return template.replace("/*__VIRA_PREVIEW_RUNTIME__*/", () =>
    script.code.replace(/<\/script/gi, "<\\/script"),
  );
}
