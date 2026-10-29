export type CodeToken = { text: string; classes?: string };
export type DiffLine = {
  kind: "context" | "added" | "removed";
  oldLine: number | null;
  newLine: number | null;
  text: string;
  newline: string;
  tokens: CodeToken[];
};
export type DiffResult = {
  lines: DiffLine[];
  added: number;
  removed: number;
  language: string;
  simplified: boolean;
  highlighting: boolean;
};
type SourceLine = { text: string; raw: string; from: number; newline: string };
type Edit = { kind: DiffLine["kind"]; oldIndex?: number; newIndex?: number };

export function sourceLines(source: string): SourceLine[] {
  if (!source) return [];
  const result: SourceLine[] = [];
  let from = 0;
  for (const raw of source.match(/[^\n]*\n|[^\n]+$/g) ?? []) {
    const newline = raw.endsWith("\r\n")
      ? "\r\n"
      : raw.endsWith("\n")
        ? "\n"
        : "";
    result.push({
      text: newline ? raw.slice(0, -newline.length) : raw,
      raw,
      from,
      newline,
    });
    from += raw.length;
  }
  return result;
}

function myers(before: SourceLine[], after: SourceLine[]): Edit[] | null {
  const n = before.length,
    m = after.length;
  const frontier = new Map<number, number>([[1, 0]]);
  const history: Map<number, number>[] = [];
  let work = 0;
  for (let distance = 0; distance <= Math.min(n + m, 500); distance++) {
    history.push(new Map(frontier));
    for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
      let x =
        diagonal === -distance ||
        (diagonal !== distance &&
          (frontier.get(diagonal - 1) ?? -1) <
            (frontier.get(diagonal + 1) ?? -1))
          ? (frontier.get(diagonal + 1) ?? 0)
          : (frontier.get(diagonal - 1) ?? 0) + 1;
      let y = x - diagonal;
      while (x < n && y < m && before[x].raw === after[y].raw) {
        x++;
        y++;
        if (++work > 2_000_000) return null;
      }
      frontier.set(diagonal, x);
      if (++work > 2_000_000) return null;
      if (x < n || y < m) continue;
      const edits: Edit[] = [];
      x = n;
      y = m;
      for (let d = history.length - 1; d >= 0; d--) {
        const previous = history[d],
          k = x - y;
        const prior =
          k === -d ||
          (k !== d && (previous.get(k - 1) ?? -1) < (previous.get(k + 1) ?? -1))
            ? k + 1
            : k - 1;
        const oldX = previous.get(prior) ?? 0,
          oldY = oldX - prior;
        while (x > oldX && y > oldY)
          edits.push({ kind: "context", oldIndex: --x, newIndex: --y });
        if (!d) break;
        if (x === oldX) edits.push({ kind: "added", newIndex: --y });
        else edits.push({ kind: "removed", oldIndex: --x });
      }
      return edits.reverse();
    }
  }
  return null;
}

export function compareLines(before: string, after: string): DiffResult {
  const oldLines = sourceLines(before),
    newLines = sourceLines(after);
  let prefix = 0,
    suffix = 0;
  while (
    prefix < oldLines.length &&
    prefix < newLines.length &&
    oldLines[prefix].raw === newLines[prefix].raw
  )
    prefix++;
  while (
    suffix < oldLines.length - prefix &&
    suffix < newLines.length - prefix &&
    oldLines[oldLines.length - 1 - suffix].raw ===
      newLines[newLines.length - 1 - suffix].raw
  )
    suffix++;
  const oldMiddle = oldLines.slice(prefix, oldLines.length - suffix),
    newMiddle = newLines.slice(prefix, newLines.length - suffix);
  const minimal = myers(oldMiddle, newMiddle);
  const middle: Edit[] = minimal ?? [
    ...oldMiddle.map((_, oldIndex) => ({ kind: "removed" as const, oldIndex })),
    ...newMiddle.map((_, newIndex) => ({ kind: "added" as const, newIndex })),
  ];
  const edits: Edit[] = [
    ...oldLines
      .slice(0, prefix)
      .map((_, i) => ({ kind: "context" as const, oldIndex: i, newIndex: i })),
    ...middle.map((item) => ({
      ...item,
      oldIndex:
        item.oldIndex === undefined ? undefined : item.oldIndex + prefix,
      newIndex:
        item.newIndex === undefined ? undefined : item.newIndex + prefix,
    })),
    ...oldLines.slice(oldLines.length - suffix).map((_, i) => ({
      kind: "context" as const,
      oldIndex: oldLines.length - suffix + i,
      newIndex: newLines.length - suffix + i,
    })),
  ];
  const lines = edits.map((item): DiffLine => {
    const source =
      item.kind === "removed"
        ? oldLines[item.oldIndex!]
        : newLines[item.newIndex!];
    return {
      kind: item.kind,
      oldLine: item.oldIndex === undefined ? null : item.oldIndex + 1,
      newLine: item.newIndex === undefined ? null : item.newIndex + 1,
      text: source.text,
      newline: source.newline,
      tokens: [{ text: source.text }],
    };
  });
  return {
    lines,
    added: lines.filter((row) => row.kind === "added").length,
    removed: lines.filter((row) => row.kind === "removed").length,
    language: "Texto",
    simplified: minimal === null,
    highlighting: false,
  };
}

export type DisplayLine =
  | { index: number; line: DiffLine }
  | { hidden: number; from: number; to: number };

export function visibleLines(
  lines: DiffLine[],
  expanded: boolean,
): DisplayLine[] {
  if (expanded) return lines.map((line, index) => ({ index, line }));
  const visible = new Set<number>();
  for (let index = 0; index < lines.length; index++)
    if (lines[index].kind !== "context")
      for (
        let offset = Math.max(0, index - 3);
        offset <= Math.min(lines.length - 1, index + 3);
        offset++
      )
        visible.add(offset);
  const result: DisplayLine[] = [];
  for (let index = 0; index < lines.length;) {
    if (visible.has(index)) {
      result.push({ index, line: lines[index] });
      index++;
      continue;
    }
    const from = index;
    while (index < lines.length && !visible.has(index)) index++;
    result.push({ hidden: index - from, from, to: index - 1 });
  }
  return result;
}
