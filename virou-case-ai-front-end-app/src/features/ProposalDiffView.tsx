import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Search,
} from "lucide-react";
import { ErrorNotice, Loading } from "../shared/ui";
import type { ProposalContent, ReviewView } from "./proposalTypes";
import {
  visibleLines,
  type DiffLine,
  type DiffResult,
  type DisplayLine,
} from "./proposalDiff";

const PAGE_SIZE = 240;
type SplitLine =
  | { index: number; left?: DiffLine; right?: DiffLine }
  | { hidden: number; from: number; to: number };

function pairLines(rows: DisplayLine[]): SplitLine[] {
  const paired: SplitLine[] = [];
  for (let index = 0; index < rows.length;) {
    const row = rows[index];
    if ("hidden" in row) {
      paired.push(row);
      index++;
      continue;
    }
    if (row.line.kind === "context") {
      paired.push({ index: row.index, left: row.line, right: row.line });
      index++;
      continue;
    }
    const removed: { index: number; line: DiffLine }[] = [],
      added: { index: number; line: DiffLine }[] = [];
    while (index < rows.length) {
      const next = rows[index];
      if ("hidden" in next || next.line.kind === "context") break;
      (next.line.kind === "removed" ? removed : added).push(next);
      index++;
    }
    for (let i = 0; i < Math.max(removed.length, added.length); i++)
      paired.push({
        index: (removed[i] ?? added[i]).index,
        left: removed[i]?.line,
        right: added[i]?.line,
      });
  }
  return paired;
}

function LineCode({ line }: { line: DiffLine }) {
  return (
    <code className="proposal-line-code">
      {line.tokens.map((token, index) => (
        <span key={index} className={token.classes}>
          {token.text}
        </span>
      ))}
      {!line.text && " "}
      {!line.newline && line.kind !== "context" && (
        <span className="proposal-no-newline">
          {" "}
          · Sem quebra de linha ao final
        </span>
      )}
    </code>
  );
}

function UnifiedLine({
  row,
  search,
}: {
  row: { index: number; line: DiffLine };
  search: string;
}) {
  const { line } = row;
  const match =
    !!search &&
    line.text.toLocaleLowerCase().includes(search.toLocaleLowerCase());
  return (
    <div
      className={`proposal-diff-line ${line.kind}${match ? " search-match" : ""}`}
      data-diff-index={row.index}
    >
      <span className="sr-only">
        {line.kind === "added"
          ? "Adicionada"
          : line.kind === "removed"
            ? "Removida"
            : "Sem alteração"}
        . Linha {line.oldLine ?? line.newLine}.
      </span>
      <span className="proposal-line-number" aria-hidden="true">
        {line.oldLine ?? ""}
      </span>
      <span className="proposal-line-number" aria-hidden="true">
        {line.newLine ?? ""}
      </span>
      <span className="proposal-line-symbol" aria-hidden="true">
        {line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}
      </span>
      <LineCode line={line} />
    </div>
  );
}

function SplitCell({
  line,
  side,
}: {
  line?: DiffLine;
  side: "left" | "right";
}) {
  return (
    <div className={`proposal-split-cell ${line?.kind ?? "empty"}`}>
      {line ? (
        <>
          <span className="sr-only">
            {side === "left" ? "Revisão base" : "Proposta"}.{" "}
            {line.kind === "removed"
              ? "Removida"
              : line.kind === "added"
                ? "Adicionada"
                : "Sem alteração"}
            .
          </span>
          <span className="proposal-line-number" aria-hidden="true">
            {side === "left" ? line.oldLine : line.newLine}
          </span>
          <span className="proposal-line-symbol" aria-hidden="true">
            {line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "}
          </span>
          <LineCode line={line} />
        </>
      ) : (
        <span className="sr-only">Sem linha correspondente</span>
      )}
    </div>
  );
}

export function ProposalDiffView({
  content,
  initialView,
  remember,
  onReady,
}: {
  content: ProposalContent;
  initialView: ReviewView;
  remember: (view: ReviewView) => void;
  onReady: (ready: boolean) => void;
}) {
  const [result, setResult] = useState<DiffResult | null>(null),
    [error, setError] = useState<unknown>(null),
    [retry, setRetry] = useState(0),
    [view, setView] = useState(initialView),
    [search, setSearch] = useState(""),
    [wide, setWide] = useState(() => matchMedia("(min-width: 1100px)").matches),
    [target, setTarget] = useState<number | null>(null),
    [searchIndex, setSearchIndex] = useState(-1),
    [changeIndex, setChangeIndex] = useState(-1);
  const scroll = useRef<HTMLDivElement>(null),
    rememberRef = useRef(remember),
    readyRef = useRef(onReady);
  rememberRef.current = remember;
  readyRef.current = onReady;
  useEffect(() => {
    readyRef.current(!!result && !error);
  }, [result, error]);
  useEffect(() => {
    const media = matchMedia("(min-width: 1100px)");
    const update = () => setWide(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    setResult(null);
    setError(null);
    let worker: Worker | undefined;
    try {
      worker = new Worker(
        new URL("./proposalDiff.worker.ts", import.meta.url),
        { type: "module" },
      );
      worker.onmessage = ({
        data,
      }: MessageEvent<{ result?: DiffResult; error?: string }>) => {
        if (data.result) setResult(data.result);
        else
          setError(
            new Error(data.error ?? "Não foi possível preparar a comparação."),
          );
        worker?.terminate();
      };
      worker.onerror = () => {
        setError(
          new Error("A comparação não pôde ser carregada. Tente novamente."),
        );
        worker?.terminate();
      };
      worker.postMessage({
        path: content.path,
        before: content.before ?? "",
        after: content.after ?? "",
      });
    } catch {
      setError(
        new Error(
          "A comparação não pôde ser carregada neste navegador. Tente novamente.",
        ),
      );
    }
    return () => worker?.terminate();
  }, [content, retry]);
  const display = useMemo(
      () => visibleLines(result?.lines ?? [], view.expanded || !!search.trim()),
      [result, view.expanded, search],
    ),
    split = wide && view.mode === "split",
    rows = useMemo(
      () => (split ? pairLines(display) : display),
      [display, split],
    ),
    pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)),
    page = Math.max(0, Math.min(view.page, pageCount - 1)),
    shown = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    matches = useMemo(
      () =>
        result?.lines.flatMap((line, index) =>
          search.trim() &&
          line.text.toLocaleLowerCase().includes(search.toLocaleLowerCase())
            ? [index]
            : [],
        ) ?? [],
      [result, search],
    ),
    changes = useMemo(
      () =>
        result?.lines.flatMap((line, index, all) =>
          line.kind !== "context" &&
          (index === 0 || all[index - 1].kind === "context")
            ? [index]
            : [],
        ) ?? [],
      [result],
    );
  useEffect(() => {
    rememberRef.current(view);
  }, [view]);
  useEffect(() => {
    if (!result || !scroll.current) return;
    const frame = requestAnimationFrame(() => {
      if (!scroll.current) return;
      if (target !== null) {
        const row = scroll.current.querySelector<HTMLElement>(
          `[data-diff-index="${target}"]`,
        );
        if (row)
          scroll.current.scrollTop +=
            row.getBoundingClientRect().top -
            scroll.current.getBoundingClientRect().top -
            40;
      } else scroll.current.scrollTop = view.scroll;
    });
    return () => cancelAnimationFrame(frame);
  }, [result, page, target, split]);
  function moveTo(index: number) {
    const position = rows.findIndex(
      (row) =>
        "index" in row &&
        (row.index === index ||
          ("left" in row &&
            row.right?.newLine === result?.lines[index].newLine)),
    );
    if (position < 0) return;
    setView((old) => ({
      ...old,
      page: Math.floor(position / PAGE_SIZE),
      scroll: 0,
    }));
    setTarget("index" in rows[position] ? rows[position].index : index);
  }
  function jump(kind: "search" | "change", delta: number) {
    const items = kind === "search" ? matches : changes;
    if (!items.length) return;
    const current = kind === "search" ? searchIndex : changeIndex;
    const next = (current + delta + items.length) % items.length;
    (kind === "search" ? setSearchIndex : setChangeIndex)(next);
    moveTo(items[next]);
  }
  function goPage(next: number) {
    setTarget(null);
    setView((old) => ({ ...old, page: next, scroll: 0 }));
  }
  if (error)
    return (
      <div className="proposal-file-error">
        <ErrorNotice error={error} />
        <button
          className="button"
          onClick={() => setRetry((value) => value + 1)}
        >
          Tentar carregar comparação
        </button>
      </div>
    );
  if (!result)
    return (
      <div className="proposal-file-loading">
        <Loading />
        <p className="small muted">Preparando a leitura deste arquivo.</p>
      </div>
    );
  return (
    <div className="proposal-diff-panel">
      <div className="proposal-diff-tools">
        <div className="proposal-diff-options">
          <span className="small">
            {result.language}
            {!result.highlighting && " · sem realce"} ·{" "}
            <strong>+{result.added}</strong> /{" "}
            <strong>−{result.removed}</strong>
          </span>
          <label className="proposal-check">
            <input
              type="checkbox"
              checked={view.wrap}
              onChange={(event) =>
                setView((old) => ({ ...old, wrap: event.target.checked }))
              }
            />
            Quebrar linhas
          </label>
          <label className="proposal-check">
            <input
              type="checkbox"
              checked={view.expanded}
              onChange={(event) => {
                setTarget(null);
                setView((old) => ({
                  ...old,
                  expanded: event.target.checked,
                  page: 0,
                  scroll: 0,
                }));
              }}
            />
            Contexto completo
          </label>
          {wide && (
            <label className="sr-select">
              Comparação
              <select
                aria-label="Modo de comparação"
                value={view.mode}
                onChange={(event) => {
                  setTarget(null);
                  setView((old) => ({
                    ...old,
                    mode: event.target.value as ReviewView["mode"],
                    page: 0,
                    scroll: 0,
                  }));
                }}
              >
                <option value="unified">Diff unificado</option>
                <option value="split">Lado a lado</option>
              </select>
            </label>
          )}
        </div>
        <div className="proposal-diff-search">
          <label>
            <Search size={15} aria-hidden="true" />
            <span className="sr-only">Buscar texto no arquivo</span>
            <input
              type="search"
              value={search}
              placeholder="Buscar neste arquivo"
              onChange={(event) => {
                setSearch(event.target.value);
                setSearchIndex(-1);
                setTarget(null);
                setView((old) => ({ ...old, page: 0, scroll: 0 }));
              }}
            />
          </label>
          {search && (
            <span className="small" role="status">
              {matches.length} ocorrência(s)
            </span>
          )}
          {search ? (
            <>
              <button
                className="icon-button"
                disabled={!matches.length}
                aria-label="Ocorrência anterior"
                onClick={() => jump("search", -1)}
              >
                <ArrowUp size={16} />
              </button>
              <button
                className="icon-button"
                disabled={!matches.length}
                aria-label="Próxima ocorrência"
                onClick={() => jump("search", 1)}
              >
                <ArrowDown size={16} />
              </button>
            </>
          ) : (
            <>
              <button
                className="icon-button"
                disabled={!changes.length}
                aria-label="Alteração anterior"
                onClick={() => jump("change", -1)}
              >
                <ArrowUp size={16} />
              </button>
              <button
                className="icon-button"
                disabled={!changes.length}
                aria-label="Próxima alteração"
                onClick={() => jump("change", 1)}
              >
                <ArrowDown size={16} />
              </button>
            </>
          )}
        </div>
        {result.simplified && (
          <p className="small" role="status">
            Arquivo extenso: comparação por blocos completos. Todo o conteúdo
            está disponível nas páginas abaixo.
          </p>
        )}
      </div>
      <div
        className={`proposal-diff-viewport${view.wrap ? " wrap" : ""}${split ? " split" : ""}`}
        ref={scroll}
        tabIndex={0}
        role="region"
        aria-label={`Diff de ${content.path}`}
        onScroll={(event) => {
          const top = event.currentTarget.scrollTop;
          setView((old) =>
            old.scroll === top ? old : { ...old, scroll: top },
          );
        }}
      >
        {split && (
          <div className="proposal-split-heading">
            <span>Revisão base</span>
            <span>Proposta</span>
          </div>
        )}
        {!shown.length && (
          <p className="proposal-empty">
            Arquivo vazio.{" "}
            {content.change === "deleted"
              ? "A proposta remove este arquivo."
              : "A proposta mantém o conteúdo vazio."}
          </p>
        )}
        <div className="proposal-diff-lines">
          {shown.map((row) =>
            "hidden" in row ? (
              <button
                className="proposal-context"
                key={`hidden-${row.from}`}
                onClick={() => {
                  setView((old) => ({
                    ...old,
                    expanded: true,
                    page: Math.floor(row.from / PAGE_SIZE),
                    scroll: 0,
                  }));
                  setTarget(row.from);
                }}
              >
                {row.hidden} linha(s) sem alteração · Expandir contexto
              </button>
            ) : "line" in row ? (
              <UnifiedLine key={row.index} row={row} search={search} />
            ) : (
              <div
                className={`proposal-split-line${search && [row.left?.text, row.right?.text].some((text) => text?.toLocaleLowerCase().includes(search.toLocaleLowerCase())) ? " search-match" : ""}`}
                key={row.index}
                data-diff-index={row.index}
              >
                <SplitCell line={row.left} side="left" />
                <SplitCell line={row.right} side="right" />
              </div>
            ),
          )}
        </div>
      </div>
      <div className="proposal-diff-pagination">
        <span className="small">
          {pageCount > 1
            ? `Página ${page + 1} de ${pageCount} · visualização paginada, sem omissões`
            : "− Remoção · + Adição · números: base / proposta"}
        </span>
        {pageCount > 1 && (
          <div className="actions">
            <button
              className="icon-button"
              disabled={page === 0}
              aria-label="Página anterior do diff"
              onClick={() => goPage(page - 1)}
            >
              <ChevronLeft size={18} />
            </button>
            <label className="sr-select">
              Página
              <select
                aria-label="Página do diff"
                value={page}
                onChange={(event) => goPage(Number(event.target.value))}
              >
                {Array.from({ length: pageCount }, (_, index) => (
                  <option key={index} value={index}>
                    {index + 1}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="icon-button"
              disabled={page === pageCount - 1}
              aria-label="Próxima página do diff"
              onClick={() => goPage(page + 1)}
            >
              <ChevronRight size={18} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
