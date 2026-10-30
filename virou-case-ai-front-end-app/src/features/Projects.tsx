import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight, FileCode2, Lightbulb, Plus, Trash2 } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, command } from "../shared/api";
import { ErrorNotice, Loading, Modal, useLoad } from "../shared/ui";
import type { Operation, Page, Project } from "../shared/types";

type ProjectsResult = Page<Project> & {
  quota: { max_projects: number; used_projects: number; can_create: boolean };
};
export function Projects() {
  const { data, error, reload } = useLoad(() =>
      api<ProjectsResult>("/projects"),
    ),
    [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false),
    [formError, setFormError] = useState<unknown>(null),
    navigate = useNavigate();
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const [deletionProgress, setDeletionProgress] = useState<Record<string, Operation>>({});
  const deletingProjects = data?.items.filter((p) => p.state === "deleting") ?? [];
  const deletingProjectIds = deletingProjects.map((p) => p.id).join(",");
  useEffect(() => {
    if (!deletingProjectIds) return;
    const projectIds = deletingProjectIds.split(",");
    let alive = true;
    async function refreshProgress() {
      const results = await Promise.all(projectIds.map(async (id) => {
        try {
          const page = await api<Page<Operation>>(`/operations?project_id=${encodeURIComponent(id)}&kind=project_deletion&latest=true`);
          return [id, page.items[0]] as const;
        } catch {
          return [id, undefined] as const;
        }
      }));
      if (alive) {
        setDeletionProgress(Object.fromEntries(results.filter((result) => !!result[1])) as Record<string, Operation>);
      }
    }
    void refreshProgress();
    const timer = window.setInterval(() => { reload(); void refreshProgress(); }, 3000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [deletingProjectIds]);
  async function remove(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!deleting || busy || confirmation !== deleting.name) return;
    setBusy(true);
    setFormError(null);
    try {
      await command(`/projects/${deleting.id}`, { confirmation }, "DELETE", deleting.version);
      setDeleting(null);
      setConfirmation("");
      setNotice("Exclusão recebida. Novas operações foram bloqueadas e os recursos serão removidos após o encerramento seguro das atividades anteriores.");
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.code === "VERSION_CONFLICT") {
        setDeleting(null);
        setNotice("O projeto mudou antes da confirmação. A lista foi atualizada; confira o estado antes de solicitar novamente.");
        reload();
      } else {
        setFormError(e);
      }
    } finally {
      setBusy(false);
    }
  }
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setFormError(null);
    const values = new FormData(event.currentTarget);
    try {
      const p = await command<Project>("/projects", {
        name: values.get("name"),
        objective: values.get("objective"),
      });
      navigate("/app/projetos/" + p.id);
    } catch (e) {
      setFormError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">SEU ESPAÇO DE CRIAÇÃO</p>
          <h1>Meus projetos</h1>
          <p>Continue de onde parou. Dê forma à próxima ideia.</p>
        </div>
        <div>
          <button
            className="button primary"
            disabled={!data?.quota.can_create}
            onClick={() => setCreating(true)}
          >
            <Plus size={18} />
            Novo projeto
          </button>
          <p className="small muted align-right">
            {data?.quota.used_projects ?? "—"} de {data?.quota.max_projects ?? "—"} projetos
          </p>
        </div>
      </div>
      <ErrorNotice error={error} />
      {notice && <p className="notice success" role="status">{notice}</p>}
      {error ? (
        <button className="button" onClick={reload}>
          Consultar novamente
        </button>
      ) : !data ? (
        <Loading />
      ) : (
        <>
          <div className="projects-grid">
            {data.items.map((p) => (
              <article className="project-card" key={p.id}>
                <div className="project-art">
                  <FileCode2 size={44} strokeWidth={1.2} />
                  <span>Seu projeto, em construção</span>
                </div>
                <div className="project-card-body">
                  <div className="card-title">
                    <h2>{p.name}</h2>
                    <span className="badge">{p.state === "deleting" ? "Exclusão pendente" : "Rascunho"}</span>
                  </div>
                  <p>{p.objective}</p>
                  <span className="stack-label">
                    {p.stack === "hono-typescript-d1"
                      ? "Hono · TypeScript · D1"
                      : "FastAPI · Python · D1"}
                  </span>
                  {p.state !== "deleting" && <Link
                    className="button primary full"
                    to={"/app/projetos/" + p.id}
                  >
                    Continuar projeto <ArrowRight size={18} />
                  </Link>}
                  {p.state === "deleting" && (
                    <div className="small" role="status">
                      <p>{deletionProgress[p.id]?.progress_message ?? "A exclusão está em processamento. O histórico de consumo será preservado."}</p>
                      {deletionProgress[p.id]?.state === "failed" && (
                        <p>A limpeza não foi concluída. Use “Retomar exclusão” para tentar novamente sem recriar recursos.</p>
                      )}
                      {deletionProgress[p.id]?.progress_stage === "deletion_waiting_operations" && (
                        <p>Existem atividades anteriores aguardando conclusão ou reconciliação segura. Nenhum arquivo será apagado antes disso.</p>
                      )}
                    </div>
                  )}
                  <button className="button danger full project-delete" onClick={() => {
                    setDeleting(p); setConfirmation(""); setFormError(null);
                  }} aria-label={`${p.state === "deleting" ? "Retomar exclusão de" : "Excluir projeto"} ${p.name}`}>
                    <Trash2 size={16} />{p.state === "deleting" ? "Retomar exclusão" : "Excluir projeto"}
                  </button>
                </div>
              </article>
            ))}
            {data.items.length === 0 && (
              <section className="empty-state">
                <FileCode2 size={48} />
                <h2>Toda construção começa com uma ideia.</h2>
                <p>
                  Descreva o que você quer criar. A plataforma prepara a
                  estrutura e orienta os próximos passos.
                </p>
                <button
                  className="button primary"
                  disabled={!data.quota.can_create}
                  onClick={() => setCreating(true)}
                >
                  Criar meu primeiro projeto <Plus size={18} />
                </button>
              </section>
            )}
          </div>
          <div className="learning-nudge">
            <span className="round-icon">
              <Lightbulb />
            </span>
            <div>
              <p className="small">Seu próximo passo</p>
              <h2>Construa com contexto. Revise com clareza.</h2>
            </div>
            <Link className="button" to="/app/aprendizado">
              Continuar aprendendo <ArrowRight size={18} />
            </Link>
          </div>
        </>
      )}
      {deleting && (
        <Modal title="Excluir projeto" close={() => !busy && setDeleting(null)}>
          <form className="form" onSubmit={remove}>
            <p>Excluir <strong>{deleting.name}</strong> remove os arquivos, rascunhos, publicação e banco de dados hospedado deste projeto. Essa ação não pode ser desfeita. O histórico de consumo permanece disponível.</p>
            <p>Baixe uma cópia do projeto antes de continuar, se precisar conservar o código.</p>
            <label>Digite o nome do projeto para confirmar
              <input autoFocus value={confirmation} maxLength={100} autoComplete="off" disabled={busy}
                onChange={(e) => setConfirmation(e.target.value)} />
            </label>
            <ErrorNotice error={formError} />
            <div className="actions">
              <button type="button" className="button" disabled={busy} onClick={() => setDeleting(null)}>Cancelar</button>
              <button className="button danger" disabled={busy || confirmation !== deleting.name}>
                <Trash2 size={16} />{busy ? "Solicitando…" : "Confirmar exclusão"}
              </button>
            </div>
          </form>
        </Modal>
      )}
      {creating && (
        <Modal title="Uma nova ideia" close={() => !busy && setCreating(false)}>
          <form className="form" onSubmit={create}>
            <label>
              Nome do projeto
              <input
                name="name"
                required
                maxLength={100}
                placeholder="Como você quer chamar sua ideia?"
                autoFocus
              />
            </label>
            <label>
              O que você quer construir?
              <textarea
                name="objective"
                required
                minLength={10}
                maxLength={3000}
                rows={5}
                placeholder="Descreva para quem é, qual problema resolve e o resultado esperado."
              />
            </label>
            <p className="small muted">
              A tecnologia é definida pela plataforma. Você pode manter até dois
              projetos.
            </p>
            <ErrorNotice error={formError} />
            <button className="button primary" disabled={busy}>
              {busy ? "Preparando…" : "Criar projeto"}
              <ArrowRight size={18} />
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
