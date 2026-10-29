import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { useSession } from "../app/session";
import { api, command, date } from "../shared/api";
import { ErrorNotice, Loading, Modal, useLoad } from "../shared/ui";
import type { Page, Project, Revision } from "../shared/types";
import { ProjectWorkspace } from "./ProjectWorkspace";
export function Workspace() {
  const { projectId } = useParams();
  return <ProjectWorkspace key={projectId} />;
}

export function ProjectHistory() {
  const { projectId: pid } = useParams(),
    { user } = useSession(),
    [cursor, setCursor] = useState("");
  const { data, error, reload } = useLoad(
    () =>
      Promise.all([
        api<Project>("/projects/" + pid),
        api<Page<Revision>>(
          `/projects/${pid}/revisions${cursor ? "?cursor=" + cursor : ""}`,
        ),
      ]),
    [pid, cursor],
  );
  const [failure, setFailure] = useState<unknown>(null),
    [busy, setBusy] = useState(false),
    [inspect, setInspect] = useState<Record<string, string> | null>(null);
  async function restore(rid: string) {
    if (
      !data ||
      busy ||
      !confirm(
        "Criar uma nova revisão com os arquivos desta versão? O histórico será preservado.",
      )
    )
      return;
    setBusy(true);
    try {
      await command(
        `/projects/${pid}/revisions`,
        {
          base_revision_id: data[0].current_revision_id,
          restore_revision_id: rid,
        },
        "POST",
        data[0].version,
      );
      reload();
    } catch (e) {
      setFailure(e);
    } finally {
      setBusy(false);
    }
  }
  async function view(r: Revision) {
    try {
      const files = await Promise.all(
        r.files.map((f) =>
          api<{ path: string; content: string }>(
            `/projects/${pid}/revisions/${r.id}/files/${f.id}`,
          ),
        ),
      );
      setInspect(Object.fromEntries(files.map((f) => [f.path, f.content])));
    } catch (e) {
      setFailure(e);
    }
  }
  return (
    <>
      <Link className="back-link" to={"/app/projetos/" + pid}>
        <ArrowLeft size={17} />
        Voltar ao projeto
      </Link>
      <div className="page-heading">
        <div>
          <h1>Histórico de revisões</h1>
          <p>Compare resultados e restaure criando uma nova revisão.</p>
        </div>
      </div>
      <ErrorNotice error={error || failure} />
      {!data && !error && <Loading />}
      {data?.[1].items.map((r) => (
        <article className="panel history-row" key={r.id}>
          <div>
            <h2>
              {r.id.slice(0, 8)}{" "}
              {r.candidate && <span className="badge">Candidata</span>}
              {data[0].current_revision_id === r.id && (
                <span className="badge">Atual</span>
              )}
            </h2>
            <p>
              {r.origin} · {date(r.created_at)} · {r.files.length} arquivos
            </p>
          </div>
          <div className="actions">
            <button className="button" onClick={() => view(r)}>
              Inspecionar arquivos
            </button>
            <button
              className="button"
              disabled={busy || !user?.access.allowed_actions.includes("write")}
              onClick={() => restore(r.id)}
            >
              Restaurar como nova revisão
            </button>
          </div>
        </article>
      ))}
      <div className="actions">
        {cursor && (
          <button className="button" onClick={() => setCursor("")}>
            Primeira página
          </button>
        )}
        {data?.[1].next_cursor && (
          <button
            className="button"
            onClick={() => setCursor(data[1].next_cursor!)}
          >
            Próximas revisões
          </button>
        )}
      </div>
      {inspect && (
        <Modal title="Arquivos da revisão" close={() => setInspect(null)}>
          {Object.entries(inspect).map(([path, content]) => (
            <details key={path}>
              <summary>{path}</summary>
              <pre className="source-preview">{content}</pre>
            </details>
          ))}
        </Modal>
      )}
    </>
  );
}

export { PublicationPage as Publish } from "./PublicationPage";
