import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";

export function ProjectPreview({
  files,
  dirty,
}: {
  files: Record<string, string>;
  dirty: boolean;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [snapshot, setSnapshot] = useState(files),
    [entry, setEntry] = useState("index.html"),
    [sequence, setSequence] = useState(0),
    [state, setState] = useState("loading"),
    [issues, setIssues] = useState<string[]>([]),
    [mobile, setMobile] = useState(false);
  const stale =
    Object.keys(files).length !== Object.keys(snapshot).length ||
    Object.keys(files).some((path) => files[path] !== snapshot[path]);
  useEffect(() => {
    const timeout = setTimeout(
      () => setState((value) => (value === "loading" ? "timeout" : value)),
      15000,
    );
    function receive(event: MessageEvent) {
      if (
        event.source !== frame.current?.contentWindow ||
        event.origin !== "null" ||
        event.data?.source !== "viracase-preview"
      )
        return;
      const { type, detail } = event.data;
      if (type === "ready") {
        const publicFiles = Object.fromEntries(
          Object.entries(snapshot).filter(([path]) =>
            path.startsWith("public/"),
          ),
        );
        frame.current?.contentWindow?.postMessage(
          { type: "render-project", files: publicFiles, entry },
          "*",
        );
      } else if (type === "rendered") {
        clearTimeout(timeout);
        setState("ready");
      } else if (type === "notice" || type === "error") {
        if (typeof detail !== "string") return;
        setIssues((old) =>
          old.includes(detail)
            ? old
            : [...old, detail.slice(0, 400)].slice(-30),
        );
        if (type === "error") {
          clearTimeout(timeout);
          setState("error");
        }
      } else if (
        type === "navigate" &&
        typeof detail === "string" &&
        "public/" + detail in snapshot &&
        /\.html?$/.test(detail)
      ) {
        setEntry(detail);
        setState("loading");
        setIssues([]);
        setSequence((n) => n + 1);
      }
    }
    function blockedNavigation(event: SecurityPolicyViolationEvent) {
      if (event.effectiveDirective !== "frame-src") return;
      clearTimeout(timeout);
      setState("error");
      setIssues((old) =>
        [
          ...old,
          "A navegação para fora da prévia foi bloqueada. Use Atualizar prévia para continuar.",
        ].slice(-30),
      );
    }
    window.addEventListener("message", receive);
    window.addEventListener("securitypolicyviolation", blockedNavigation);
    return () => {
      clearTimeout(timeout);
      window.removeEventListener("message", receive);
      window.removeEventListener("securitypolicyviolation", blockedNavigation);
    };
  }, [snapshot, sequence, entry]);
  function refresh() {
    setSnapshot(files);
    setEntry("index.html");
    setState("loading");
    setIssues([]);
    setSequence((n) => n + 1);
  }
  return (
    <div className="project-preview">
      <div className="preview-toolbar">
        <span>
          {dirty ? "Prévia do rascunho" : "Prévia da revisão salva"} · public/
          {entry}
        </span>
        <div className="actions">
          <button
            className="button"
            aria-pressed={mobile}
            onClick={() => setMobile(!mobile)}
          >
            {mobile ? "Largura completa" : "Largura de celular"}
          </button>
          <button className="button" onClick={refresh}>
            <RefreshCw size={14} /> Atualizar prévia
          </button>
        </div>
      </div>
      <p className="small preview-scope">
        HTML, CSS e JavaScript do projeto. A API e o banco de dados não são
        executados nesta prévia.
      </p>
      {stale && (
        <p role="status" className="notice">
          O código mudou. Atualize a prévia para visualizar suas alterações.
        </p>
      )}
      {state === "loading" && (
        <p role="status" className="small">
          Carregando prévia…
        </p>
      )}
      {state === "timeout" && (
        <p role="alert" className="notice">
          A prévia não respondeu. Confira os arquivos e tente atualizar.
        </p>
      )}
      <div className={"preview-canvas" + (mobile ? " narrow" : "")}>
        <iframe
          key={sequence}
          ref={frame}
          title="Prévia interativa do projeto"
          src="/preview.html"
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
        />
      </div>
      {!!issues.length && (
        <details className="preview-issues" open={state === "error"}>
          <summary>Avisos da prévia ({issues.length})</summary>
          <ul>
            {issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
