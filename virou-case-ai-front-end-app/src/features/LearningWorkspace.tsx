import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  Check,
  Copy,
  Search,
  ShieldCheck,
} from "lucide-react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, command } from "../shared/api";
import { ErrorNotice, Loading, Modal, useLoad } from "../shared/ui";
import type {
  Draft,
  Guide,
  LearningDraft,
  Page,
  Project,
} from "../shared/types";
import type { BuildDraft } from "./guidedTypes";
import { useDraft } from "./useDraft";
import { DraftConflict } from "./DraftConflict";

const categories: Record<string, string> = {
  "": "Todos",
  planning: "Planejamento",
  context: "Contexto",
  quality: "Qualidade",
  security: "Segurança",
};
const normalize = (s: string) =>
  s
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("pt-BR");
export function LearningView() {
  const { slug } = useParams(),
    [search, setSearch] = useSearchParams();
  const { data, error, reload } = useLoad(
    () => api<Page<Guide>>("/learning/guides"),
    [],
  );
  const category = search.get("category") ?? "",
    query = search.get("q") ?? "";
  const pendingFilters = useRef(new URLSearchParams(search));
  useEffect(() => {
    const restore = () => {
      pendingFilters.current = new URLSearchParams(location.search);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  function filter(key: string, value: string) {
    const next = new URLSearchParams(pendingFilters.current);
    if (value) next.set(key, value);
    else next.delete(key);
    pendingFilters.current = next;
    setSearch(next, { replace: true });
  }
  const items = data?.items.filter(
    (g) =>
      (!category || g.category === category) &&
      normalize(
        g.title + " " + g.objective + " " + (g.search_terms ?? ""),
      ).includes(normalize(query)),
  );
  return (
    <div className={"learning-workspace " + (slug ? "has-guide" : "")}>
      <div className="page-heading">
        <div>
          <p className="eyebrow">CONHECIMENTO QUE VIRA PRÁTICA</p>
          <h1>Aprender e aplicar</h1>
          <p>Guias práticos para transformar sua ideia em um projeto.</p>
        </div>
        <span className="badge soft">
          <BookOpen size={16} />
          Sem consumir créditos
        </span>
      </div>
      <div className="learning-split">
        <section className="learning-catalog" aria-label="Catálogo de guias">
          <label className="search">
            <Search size={18} />
            <span className="sr-only">Buscar guias</span>
            <input
              value={query}
              onChange={(e) => filter("q", e.target.value)}
              placeholder="Buscar um guia, skill ou template"
            />
          </label>
          <div
            className="filter-tabs learning-filters"
            role="group"
            aria-label="Categorias"
          >
            {Object.entries(categories).map(([key, label]) => (
              <button
                key={key}
                aria-pressed={category === key}
                className={category === key ? "selected" : ""}
                onClick={() => filter("category", key)}
              >
                {label}
              </button>
            ))}
          </div>
          <ErrorNotice error={error} />
          {error ? (
            <button className="button" onClick={reload}>
              Tentar novamente
            </button>
          ) : !items ? (
            <Loading />
          ) : (
            <div className="guide-grid compact-guides">
              {items.map((g) => (
                <article
                  key={g.slug}
                  className={
                    "guide-card " + (slug === g.slug ? "selected-guide" : "")
                  }
                >
                  <div className="card-title">
                    <span className="round-icon">
                      {g.category === "security" ? (
                        <ShieldCheck size={19} />
                      ) : (
                        <BookOpen size={19} />
                      )}
                    </span>
                    <span className="category-label">
                      {categories[g.category]}
                    </span>
                  </div>
                  <h2>{g.title}</h2>
                  <p>{g.objective}</p>
                  <Link
                    className="button"
                    to={`/app/aprendizado/${g.slug}?${search}`}
                  >
                    Ver guia <ArrowRight size={15} />
                  </Link>
                </article>
              ))}
              {items.length === 0 && (
                <p>Nenhum guia corresponde à busca. Ajuste os filtros.</p>
              )}
            </div>
          )}
          <div className="notice security">
            <ShieldCheck size={20} />
            <p>
              Construa com segurança. Cada guia inclui cuidados com dados e
              acessos.
            </p>
          </div>
        </section>
        {slug ? (
          <GuideApplication key={slug} slug={slug} />
        ) : (
          <aside className="panel learning-empty">
            <BookOpen size={42} />
            <h2>Entenda. Adapte. Aplique.</h2>
            <p>
              Escolha um guia, prepare um pedido com o contexto do seu projeto e
              leve o rascunho à conversa.
            </p>
            <p>A geração só começa quando você enviar o pedido no projeto.</p>
          </aside>
        )}
      </div>
    </div>
  );
}
const learningPayload = (d: LearningDraft) => ({
  fields: d.fields,
  prompt: d.prompt,
  manual: d.manual,
});
function interpolate(g: Guide, fields: Record<string, string>) {
  return g.application_template.replace(
    /\{\{(\w+)\}\}/g,
    (all, key) => fields[key]?.trim() || all,
  );
}
function GuideApplication({ slug }: { slug: string }) {
  const draft = useDraft<LearningDraft>(
      `/learning/guides/${slug}/draft`,
      learningPayload,
    ),
    [step, setStep] = useState(0),
    [search] = useSearchParams(),
    navigate = useNavigate();
  const [target, setTarget] = useState(search.get("project") ?? ""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [copied, setCopied] = useState(false),
    [replacePrompt, setReplacePrompt] = useState(false);
  const projects = useLoad(
    () =>
      api<
        Page<Project> & {
          quota: {
            max_projects: number;
            used_projects: number;
            can_create: boolean;
          };
        }
      >("/projects"),
    [],
  );
  const d = draft.data,
    g = d?.guide;
  useEffect(() => {
    const guard = (event: MouseEvent) => {
      const anchor = (event.target as Element)?.closest(
        "a[href]",
      ) as HTMLAnchorElement | null;
      if (
        !anchor ||
        anchor.origin !== location.origin ||
        anchor.target ||
        anchor.href === location.href ||
        event.ctrlKey ||
        event.metaKey
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      void draft
        .flush()
        .then(() => navigate(anchor.pathname + anchor.search))
        .catch(setError);
    };
    document.addEventListener("click", guard, true);
    return () => document.removeEventListener("click", guard, true);
  }, [draft.flush, navigate]);
  const incomplete =
      !!g && g.fields.some((f) => f.required && !d!.fields[f.key]?.trim()),
    markers = /\{\{[^}]+\}\}/.test(d?.prompt ?? "");
  const overLimit =
    !!d &&
    (d.prompt.length > 12000 ||
      d.guide.fields.some(
        (f) => (d.fields[f.key]?.length ?? 0) > f.max_length,
      ));
  async function act(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  async function transfer(version: number) {
    await draft.flush();
    const result = await command<{ project_id: string }>(
      `/learning/guides/${slug}/attach`,
      {
        project_id: target,
        source_version: draft.version(),
        target_version: version,
        mode: "append",
      },
    );
    navigate(`/app/projetos/${result.project_id}?prompt=guide`);
  }
  function fields(key: string, value: string) {
    draft.setData((old) => {
      const values = { ...old.fields, [key]: value };
      return {
        ...old,
        fields: values,
        prompt: old.manual ? old.prompt : interpolate(old.guide, values),
      };
    });
    setCopied(false);
  }
  if (!d || !g)
    return (
      <section className="panel">
        <ErrorNotice error={draft.error} />
        {!draft.error && <Loading />}
      </section>
    );
  return (
    <section className="panel guide-application" aria-label="Aplicar guia">
      <Link
        className="mobile-catalog-back"
        to={`/app/aprendizado?${search}`}
        onClick={(e) => {
          e.preventDefault();
          void act(async () => {
            await draft.flush();
            navigate(`/app/aprendizado?${search}`);
          });
        }}
      >
        ← Voltar aos guias
      </Link>
      <p className="eyebrow">
        {categories[g.category]} · {g.template_version}
      </p>
      <h2>{g.title}</h2>
      <p className="muted">{g.objective}</p>
      <div className="guide-steps" role="tablist" aria-label="Etapas do guia">
        {["Entenda", "Adapte", "Aplique"].map((label, i) => (
          <button
            key={label}
            role="tab"
            aria-selected={step === i}
            onClick={() => setStep(i)}
          >
            <span>{i + 1}</span>
            {label}
          </button>
        ))}
      </div>
      <ErrorNotice error={error || draft.error} />
      <p className="small muted" role="status">
        {draft.status}
      </p>
      {!!draft.error && !draft.conflict && (
        <button className="button" onClick={() => void act(draft.flush)}>
          Tentar salvar rascunho
        </button>
      )}
      <div hidden={step !== 0} role="tabpanel" aria-label="Entenda">
        <h3>Entenda o conceito</h3>
        <p>{g.concept}</p>
        <h3>Quando usar</h3>
        <p>{g.when_to_use}</p>
        <h3>Na prática</h3>
        <ol>
          {g.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
        <details>
          <summary>Template original e checklist</summary>
          <pre className="source-preview">{g.template}</pre>
          {g.checklist.map((s, i) => (
            <label className="check-row" key={s}>
              <input type="checkbox" name={"check-" + i} />
              {s}
            </label>
          ))}
        </details>
        <div className="security-panel">
          <ShieldCheck size={20} />
          <h3>Construa com segurança</h3>
          {g.security_notes.map((s) => (
            <p key={s}>{s}</p>
          ))}
        </div>
        <details>
          <summary>Aplicação, limites e fontes</summary>
          <p>{g.application}</p>
          <p>{g.limitations}</p>
          {g.sources.map((s) => (
            <p key={s.url}>
              <a href={s.url} target="_blank" rel="noopener noreferrer">
                {s.title} ↗
              </a>
            </p>
          ))}
          <p className="small">Revisão editorial: {g.reviewed_at}</p>
        </details>
        <button className="button primary full" onClick={() => setStep(1)}>
          Adaptar ao meu projeto <ArrowRight size={16} />
        </button>
      </div>
      <div hidden={step !== 1} role="tabpanel" aria-label="Adapte">
        <h3>Adapte ao seu projeto</h3>
        <p>
          Preencha os campos com um resultado claro. Use dados sintéticos e não
          inclua segredos.
        </p>
        <div className="form">
          {g.fields.map((f) => (
            <label key={f.key} htmlFor={"guide-" + f.key}>
              {f.label}
              {f.required ? " *" : ""}
              <textarea
                id={"guide-" + f.key}
                required={f.required}
                aria-invalid={(d.fields[f.key]?.length ?? 0) > f.max_length}
                rows={f.key === "acceptance" ? 4 : 2}
                value={d.fields[f.key] ?? ""}
                onChange={(e) => fields(f.key, e.target.value)}
              />
              <span className="small muted">
                {(d.fields[f.key] ?? "").length}/{f.max_length}
              </span>
            </label>
          ))}
        </div>
        {d.manual && (
          <div className="notice">
            Você editou o prompt manualmente. Alterações nos campos preservam
            essa versão.
            <button className="button" onClick={() => setReplacePrompt(true)}>
              Gerar novamente a partir dos campos
            </button>
          </div>
        )}
        <button className="button primary full" onClick={() => setStep(2)}>
          Revisar e aplicar <ArrowRight size={16} />
        </button>
      </div>
      <div hidden={step !== 2} role="tabpanel" aria-label="Aplique">
        <div className="card-title">
          <h3>Seu prompt personalizado</h3>
          <button
            className="button"
            disabled={!d.prompt || markers || incomplete || overLimit}
            onClick={() =>
              void act(async () => {
                await navigator.clipboard.writeText(d.prompt);
                setCopied(true);
              })
            }
          >
            {copied ? <Check size={15} /> : <Copy size={15} />}{" "}
            {copied ? "Copiado" : "Copiar template"}
          </button>
        </div>
        <label className="form-label" htmlFor="guide-prompt">
          Revise e edite o texto que será transferido
        </label>
        <textarea
          id="guide-prompt"
          className="prompt-preview"
          rows={12}
          aria-invalid={d.prompt.length > 12000}
          value={d.prompt}
          onChange={(e) => {
            draft.setData((old) => ({
              ...old,
              prompt: e.target.value,
              manual: true,
            }));
            setCopied(false);
          }}
        />
        <p className="small muted">
          {d.prompt.length}/12.000 caracteres · sem consumo de IA
        </p>
        {overLimit && (
          <p className="notice error" role="alert">
            O texto excede os limites indicados. Reduza o conteúdo para salvar,
            copiar ou transferir; nenhuma parte foi cortada.
          </p>
        )}
        {(incomplete || markers) && (
          <p className="notice">
            Preencha os campos obrigatórios e os marcadores antes de copiar ou
            transferir.
          </p>
        )}
        <h3>Aplique ao seu projeto</h3>
        <ErrorNotice error={projects.error} />
        {!!projects.error && (
          <button className="button" onClick={projects.reload}>
            Atualizar projetos
          </button>
        )}
        <label className="form-label" htmlFor="target-project">
          Projeto de destino
        </label>
        <select
          id="target-project"
          value={target}
          onChange={(e) => {
            setTarget(e.target.value);
          }}
        >
          <option value="">Escolha um projeto</option>
          {projects.data?.items.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        {projects.data && (
          <p className="small muted">
            Você tem {projects.data.quota.used_projects} de{" "}
            {projects.data.quota.max_projects} projetos permitidos.{" "}
            {projects.data.quota.can_create && (
              <Link to="/app/projetos">Gerenciar projetos</Link>
            )}
          </p>
        )}
        {projects.data?.items.length === 0 && (
          <p>
            Crie um projeto em Meus projetos para receber este rascunho. Seu
            guia continua salvo.
          </p>
        )}
        <button
          className="button primary full"
          disabled={
            busy ||
            !!draft.conflict ||
            !target ||
            !projects.data?.items.some((p) => p.id === target) ||
            incomplete ||
            overLimit ||
            markers ||
            !d.prompt.trim() ||
            d.prompt.length > 12000
          }
          onClick={() =>
            void act(async () => {
              await draft.flush();
              const remote = await api<Draft<BuildDraft>>(
                `/projects/${target}/build-draft`,
              );
              await transfer(remote.version);
            })
          }
        >
          Levar rascunho para o projeto <ArrowRight size={16} />
        </button>
        <p className="small muted">
          Sua cópia será anexada ao pedido para revisão. A mensagem já escrita
          será preservada. Envie no projeto quando estiver pronto para usar IA.
        </p>
      </div>
      {replacePrompt && (
        <Modal
          title="Atualizar o prompt a partir dos campos?"
          close={() => setReplacePrompt(false)}
        >
          <p>Sua edição manual será substituída pelo texto abaixo.</p>
          <pre className="source-preview">{interpolate(g, d.fields)}</pre>
          <div className="actions">
            <button className="button" onClick={() => setReplacePrompt(false)}>
              Manter edição manual
            </button>
            <button
              className="button primary"
              onClick={() => {
                draft.setData((old) => ({
                  ...old,
                  prompt: interpolate(g, old.fields),
                  manual: false,
                }));
                setReplacePrompt(false);
              }}
            >
              Substituir prompt
            </button>
          </div>
        </Modal>
      )}
      {draft.conflict && (
        <DraftConflict
          local={d}
          remote={draft.conflict.data}
          onResolve={draft.resolve}
        />
      )}
    </section>
  );
}
