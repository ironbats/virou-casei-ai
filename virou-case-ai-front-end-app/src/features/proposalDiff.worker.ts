import { classHighlighter, highlightTree } from "@lezer/highlight";
import { compareLines, sourceLines, type CodeToken } from "./proposalDiff";

async function parserFor(path: string) {
  if (/\.[cm]?[jt]sx?$/i.test(path)) {
    const { javascript } = await import("@codemirror/lang-javascript");
    const typescript = /\.[cm]?tsx?$/i.test(path);
    return {
      language: typescript ? "TypeScript" : "JavaScript",
      parser: javascript({ typescript, jsx: /x$/i.test(path) }).language.parser,
    };
  }
  if (/\.html?$/i.test(path))
    return {
      language: "HTML",
      parser: (await import("@codemirror/lang-html")).html().language.parser,
    };
  if (/\.css$/i.test(path))
    return {
      language: "CSS",
      parser: (await import("@codemirror/lang-css")).css().language.parser,
    };
  if (/\.jsonc?$/i.test(path))
    return {
      language: "JSON",
      parser: (await import("@codemirror/lang-json")).json().language.parser,
    };
  if (/\.py$/i.test(path))
    return {
      language: "Python",
      parser: (await import("@codemirror/lang-python")).python().language
        .parser,
    };
  if (/\.sql$/i.test(path))
    return {
      language: "SQL",
      parser: (await import("@codemirror/lang-sql")).sql().language.parser,
    };
  return null;
}

async function highlight(path: string, source: string) {
  const language = await parserFor(path);
  if (!language) return null;
  const spans: { from: number; to: number; classes: string }[] = [];
  highlightTree(
    language.parser.parse(source),
    classHighlighter,
    (from, to, classes) => spans.push({ from, to, classes }),
  );
  let cursor = 0;
  const lines = sourceLines(source).map((line) => {
    const tokens: CodeToken[] = [];
    let offset = line.from;
    const end = line.from + line.text.length;
    while (cursor < spans.length && spans[cursor].to <= offset) cursor++;
    for (
      let index = cursor;
      index < spans.length && spans[index].from < end;
      index++
    ) {
      const span = spans[index],
        start = Math.max(offset, span.from),
        finish = Math.min(end, span.to);
      if (start > offset) tokens.push({ text: source.slice(offset, start) });
      if (finish > start)
        tokens.push({
          text: source.slice(start, finish),
          classes: span.classes,
        });
      offset = Math.max(offset, finish);
    }
    if (offset < end) tokens.push({ text: source.slice(offset, end) });
    return tokens.length ? tokens : [{ text: line.text }];
  });
  return { language: language.language, lines };
}

self.onmessage = async ({
  data,
}: MessageEvent<{ path: string; before: string; after: string }>) => {
  try {
    const result = compareLines(data.before, data.after);
    try {
      const [before, after] = await Promise.all([
        highlight(data.path, data.before),
        highlight(data.path, data.after),
      ]);
      if (before && after) {
        result.language = after.language;
        result.highlighting = true;
        for (const line of result.lines)
          line.tokens =
            line.kind === "removed"
              ? before.lines[line.oldLine! - 1]
              : after.lines[line.newLine! - 1];
      }
    } catch {
      result.highlighting = false;
    }
    self.postMessage({ result });
  } catch {
    self.postMessage({
      error:
        "Não foi possível calcular a comparação. Reabra este arquivo para tentar novamente.",
    });
  }
};
