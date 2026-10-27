import { writeFile } from "node:fs/promises";
import { previewHtml } from "./previewBundle.ts";

await writeFile("dist/preview.html", await previewHtml());
