import { format } from "prettier/standalone";
import babel from "prettier/plugins/babel";
import estree from "prettier/plugins/estree";
import typescript from "prettier/plugins/typescript";
import html from "prettier/plugins/html";
import postcss from "prettier/plugins/postcss";
import markdown from "prettier/plugins/markdown";

self.onmessage = async (
  event: MessageEvent<{ path: string; source: string }>,
) => {
  try {
    const source = event.data.source;
    const formatted = await format(source, {
      filepath: event.data.path,
      plugins: [babel, estree, typescript, html, postcss, markdown],
      tabWidth: 2,
      printWidth: 88,
      endOfLine: source.includes("\r\n") ? "crlf" : "lf",
      htmlWhitespaceSensitivity: "css",
    });
    self.postMessage({ formatted });
  } catch {
    self.postMessage({
      error:
        "Não foi possível formatar: confira a sintaxe do arquivo. Seu conteúdo foi preservado.",
    });
  }
};
