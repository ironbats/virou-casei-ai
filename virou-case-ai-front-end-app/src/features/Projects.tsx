import { useState, type FormEvent } from "react";
import { ArrowRight, FileCode2, Lightbulb, Plus } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, command } from "../shared/api";
import { ErrorNotice, Loading, Modal, useLoad } from "../shared/ui";
import type { Page, Project } from "../shared/types";

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
            {data?.quota.used_projects ?? "—"} de 2 projetos
          </p>
        </div>
      </div>
      <ErrorNotice error={error} />
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
                    <span className="badge">Rascunho</span>
                  </div>
                  <p>{p.objective}</p>
                  <span className="stack-label">
                    {p.stack === "hono-typescript-d1"
                      ? "Hono · TypeScript · D1"
                      : "FastAPI · Python · D1"}
                  </span>
                  <Link
                    className="button primary full"
                    to={"/app/projetos/" + p.id}
                  >
                    Continuar projeto <ArrowRight size={18} />
                  </Link>
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
