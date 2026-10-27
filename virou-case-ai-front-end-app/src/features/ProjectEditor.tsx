import { useEffect, useRef, useState } from "react";
import { EditorView, basicSetup } from "codemirror";
import { Annotation, EditorState } from "@codemirror/state";
import { keymap } from "@codemirror/view";
import { indentWithTab } from "@codemirror/commands";
import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { markdown } from "@codemirror/lang-markdown";
import { canFormat, formatCode } from "./formatCode";
import { useFormattedSource } from "./useFormattedSource";
const externalChange = Annotation.define<boolean>();
import {
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder,
  Plus,
  Trash2,
  X,
} from "lucide-react";

function language(path: string) {
  if (/\.[cm]?[jt]sx?$/.test(path))
    return javascript({
      typescript: /\.tsx?$/.test(path),
      jsx: /x$/.test(path),
    });
  if (/\.html?$/.test(path)) return html();
  if (/\.css$/.test(path)) return css();
  if (/\.jsonc?$/.test(path)) return json();
  if (/\.py$/.test(path)) return python();
  if (/\.sql$/.test(path)) return sql();
  if (/\.md$/.test(path)) return markdown();
  return [];
}
function Code({
  path,
  value,
  readOnly,
  onChange,
}: {
  path: string;
  value: string;
  readOnly: boolean;
  onChange: (s: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null),
    editor = useRef<EditorView | null>(null),
    change = useRef(onChange);
  change.current = onChange;
  useEffect(() => {
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          EditorView.cspNonce.of(
            document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')
              ?.content ?? "",
          ),
          language(path),
          keymap.of([indentWithTab]),
          EditorState.readOnly.of(readOnly),
          EditorState.lineSeparator.of(value.includes("\r\n") ? "\r\n" : "\n"),
          EditorView.editable.of(!readOnly),
          EditorView.contentAttributes.of({
            "aria-label": "Conteúdo de " + path,
            "aria-describedby": "editor-help",
          }),
          EditorView.updateListener.of((update) => {
            if (
              update.docChanged &&
              update.transactions.some(
                (t) => t.docChanged && !t.annotation(externalChange),
              )
            )
              change.current(update.state.sliceDoc());
          }),
          EditorView.theme({
            "&": { height: "100%", fontSize: "13px" },
            ".cm-scroller": {
              overflow: "auto",
              fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
            },
            ".cm-content": { minHeight: "360px" },
            ".cm-gutters": {
              backgroundColor: "#f8f9fb",
              color: "#536173",
              borderRight: "1px solid #e5e8ed",
            },
            "&.cm-focused": { outline: "2px solid #496887" },
          }),
        ],
      }),
    });
    view.scrollDOM.tabIndex = 0;
    view.scrollDOM.setAttribute("aria-label", "Área de código de " + path);
    editor.current = view;
    return () => {
      view.destroy();
      editor.current = null;
    };
  }, [path, readOnly]);
  useEffect(() => {
    const view = editor.current;
    if (view && view.state.sliceDoc() !== value)
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: value },
        annotations: externalChange.of(true),
      });
  }, [value]);
  return <div className="cm-host" ref={host} />;
}
export function ProjectEditor({
  files,
  base,
  selected,
  onSelect,
  onChange,
  onAdd,
  onRemove,
  readOnly,
}: {
  files: Record<string, string>;
  base: Record<string, string>;
  selected: string;
  onSelect: (p: string) => void;
  onChange: (p: string, s: string) => void;
  onAdd: () => void;
  onRemove: () => void;
  readOnly: boolean;
}) {
  const formatted = useFormattedSource(
    selected,
    files[selected] ?? "",
    (value) => onChange(selected, value),
  );
  const [tabs, setTabs] = useState<string[]>([]),
    [closed, setClosed] = useState<Set<string>>(new Set()),
    [plain, setPlain] = useState(false),
    [formatting, setFormatting] = useState(false),
    [formatMessage, setFormatMessage] = useState("");
  const latest = useRef({ files, readOnly, onChange });
  latest.current = { files, readOnly, onChange };
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function formatSelected() {
    const path = selected,
      source = files[path];
    if (formatting || readOnly || source === undefined || !canFormat(path))
      return;
    setFormatting(true);
    setFormatMessage("");
    try {
      const formatted = await formatCode(path, source);
      if (!mounted.current) return;
      if (latest.current.readOnly || latest.current.files[path] !== source) {
        setFormatMessage(
          "O arquivo mudou durante a formatação. Tente novamente; sua edição foi preservada.",
        );
      } else {
        if (formatted !== source) latest.current.onChange(path, formatted);
        setFormatMessage(
          formatted === source
            ? "O arquivo já está formatado."
            : "Arquivo formatado no rascunho. Salve para criar uma revisão.",
        );
      }
    } catch (error) {
      if (mounted.current) setFormatMessage((error as Error).message);
    } finally {
      if (mounted.current) setFormatting(false);
    }
  }
  useEffect(() => {
    setFormatMessage("");
    if (selected)
      setTabs((old) => (old.includes(selected) ? old : [...old, selected]));
  }, [selected]);
  const paths = Object.keys(files).sort(),
    opened = tabs.filter((p) => p in files);
  const entries: { path: string; folder: boolean; depth: number }[] = [];
  const seen = new Set<string>();
  for (const path of paths) {
    const parts = path.split("/");
    for (let i = 1; i < parts.length; i++) {
      const folder = parts.slice(0, i).join("/");
      if (!seen.has(folder)) {
        entries.push({ path: folder, folder: true, depth: i - 1 });
        seen.add(folder);
      }
    }
    entries.push({ path, folder: false, depth: parts.length - 1 });
  }
  const visible = entries.filter(
    (e) =>
      !e.path
        .split("/")
        .slice(0, -1)
        .some((_, i) =>
          closed.has(
            e.path
              .split("/")
              .slice(0, i + 1)
              .join("/"),
          ),
        ),
  );
  return (
    <>
      <div
        className="ide-editor"
        onKeyDown={(event) => {
          if (event.shiftKey && event.altKey && event.code === "KeyF") {
            event.preventDefault();
            void formatSelected();
          }
        }}
      >
        <aside className="file-tree">
          <div className="file-tree-heading">
            <strong>ARQUIVOS</strong>
            <div className="actions">
              <button
                className="icon-button"
                aria-label="Novo arquivo"
                disabled={readOnly}
                onClick={onAdd}
              >
                <Plus size={15} />
              </button>
              <button
                className="icon-button"
                aria-label="Remover arquivo selecionado"
                disabled={readOnly || !selected}
                onClick={onRemove}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </div>
          <div
            role="tree"
            aria-label="Arquivos do projeto"
            onKeyDown={(e) => {
              const items = Array.from(
                e.currentTarget.querySelectorAll<HTMLElement>(
                  '[role="treeitem"]',
                ),
              );
              const i = items.indexOf(document.activeElement as HTMLElement);
              if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
                e.preventDefault();
                items[
                  e.key === "Home"
                    ? 0
                    : e.key === "End"
                      ? items.length - 1
                      : Math.max(
                          0,
                          Math.min(
                            items.length - 1,
                            i + (e.key === "ArrowDown" ? 1 : -1),
                          ),
                        )
                ]?.focus();
              }
            }}
          >
            {visible.map((e) => (
              <button
                key={e.path}
                role="treeitem"
                aria-level={e.depth + 1}
                aria-expanded={e.folder ? !closed.has(e.path) : undefined}
                aria-selected={!e.folder && selected === e.path}
                style={{ paddingLeft: 10 + e.depth * 13 }}
                title={e.path}
                onKeyDown={(event) => {
                  if (
                    e.folder &&
                    ["ArrowLeft", "ArrowRight"].includes(event.key)
                  ) {
                    event.preventDefault();
                    setClosed((old) => {
                      const next = new Set(old);
                      if (event.key === "ArrowLeft") next.add(e.path);
                      else next.delete(e.path);
                      return next;
                    });
                  }
                }}
                onClick={() => {
                  if (e.folder)
                    setClosed((old) => {
                      const next = new Set(old);
                      if (next.has(e.path)) next.delete(e.path);
                      else next.add(e.path);
                      return next;
                    });
                  else onSelect(e.path);
                }}
              >
                {e.folder ? (
                  <>
                    {closed.has(e.path) ? (
                      <ChevronRight size={13} />
                    ) : (
                      <ChevronDown size={13} />
                    )}
                    <Folder size={14} />
                  </>
                ) : (
                  <FileCode2 size={14} />
                )}
                <span>{e.path.split("/").at(-1)}</span>
                {!e.folder && files[e.path] !== base[e.path] && (
                  <span aria-label="alterado">•</span>
                )}
              </button>
            ))}
          </div>
        </aside>
        <div className="editor-main">
          <div className="code-toolbar">
            <span title={selected}>
              {selected || "Nenhum arquivo selecionado"}
            </span>
            <button
              className="button"
              title="Formatar arquivo (Shift + Alt + F)"
              disabled={readOnly || formatting || !canFormat(selected)}
              onClick={() => void formatSelected()}
            >
              {formatting ? "Formatando…" : "Formatar arquivo"}
            </button>
          </div>
          <div
            className="file-tabs"
            role="tablist"
            aria-label="Arquivos abertos"
          >
            {opened.map((path) => (
              <div className={selected === path ? "active" : ""} key={path}>
                <button
                  role="tab"
                  aria-selected={selected === path}
                  title={path + " — Delete fecha a aba"}
                  onKeyDown={(event) => {
                    if (event.key === "Delete") {
                      event.preventDefault();
                      setTabs((old) => old.filter((p) => p !== path));
                      if (selected === path)
                        onSelect(opened.find((p) => p !== path) ?? "");
                    }
                  }}
                  onClick={() => onSelect(path)}
                >
                  {path.split("/").at(-1)}
                  {files[path] !== base[path] && " •"}
                  <span
                    className="tab-close"
                    aria-hidden="true"
                    onClick={(event) => {
                      event.stopPropagation();
                      setTabs((old) => old.filter((p) => p !== path));
                      if (selected === path)
                        onSelect(opened.find((p) => p !== path) ?? "");
                    }}
                  >
                    <X size={12} />
                  </span>
                </button>
              </div>
            ))}
          </div>
          {selected && selected in files ? (
            <div role="tabpanel" aria-label={selected} className="code-surface">
              {!formatted.ready ? (
                <p className="format-loading small muted" role="status">
                  Preparando código formatado…
                </p>
              ) : plain ? (
                <textarea
                  className="code-editor"
                  spellCheck={false}
                  aria-label={"Conteúdo de " + selected}
                  value={formatted.value}
                  readOnly={readOnly}
                  onChange={(e) => formatted.edit(e.target.value)}
                />
              ) : (
                <Code
                  path={selected}
                  value={formatted.value}
                  readOnly={readOnly}
                  onChange={formatted.edit}
                />
              )}
            </div>
          ) : (
            <p className="empty-editor">Selecione um arquivo na árvore.</p>
          )}
        </div>
      </div>
      {formatted.ready && formatted.error && (
        <p className="format-fallback small muted" role="status">
          {formatted.error}
        </p>
      )}
      {formatMessage && (
        <p className="format-message" role="status">
          {formatMessage}
        </p>
      )}
      <div className="editor-hint">
        <span id="editor-help">
          Tab indenta. Esc, depois Tab, sai do editor.
        </span>
        <button className="text-button" onClick={() => setPlain(!plain)}>
          {plain ? "Editor com destaque" : "Usar texto simples"}
        </button>
      </div>
    </>
  );
}
