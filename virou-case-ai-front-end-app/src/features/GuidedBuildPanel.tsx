import { GuidedTemplateCard } from "./GuidedTemplateCard";
import { GuidedSteps } from "./GuidedSteps";
import { BuildAttachments } from "./BuildAttachments";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ArrowRight,
  FileText,
  Plus,
  Search,
  Send,
  Sparkles,
} from "lucide-react";
import { api, command } from "../shared/api";
import { ErrorNotice, Loading, Modal } from "../shared/ui";
import type { Capabilities, Operation } from "../shared/types";
import { useDraft } from "./useDraft";
import { DraftConflict } from "./DraftConflict";
import { TemplateEditor } from "./TemplateEditor";
import {
  buildPayload,
  type Attachment,
  type BuildDraft,
  type Recommendation,
  type Template,
} from "./guidedTypes";
import "./guided.css";

const categories: Record<string, string> = {
  planning: "Planejamento",
  context: "Contexto",
  quality: "Qualidade",
  security: "Segurança",
};
export function GuidedBuildPanel({
  pid,
  caps,
  canWrite,
  blocked,
  active,
  pendingProposal,
  hasHistory,
  children,
  beforeSend,
  onAccepted,
  reuseRequest,
  onReused,
  replyTo,
  onFlushReady,
}: {
  pid: string;
  caps: Capabilities;
  canWrite: boolean;
  blocked: boolean;
  active: boolean;
  pendingProposal: boolean;
  hasHistory: boolean;
  children: ReactNode;
  beforeSend: () => Promise<string>;
  onAccepted: (op: Operation) => void;
  onFlushReady: (fn: () => Promise<void>) => void;
  reuseRequest: string | null;
  onReused: () => void;
  replyTo: string | null;
}) {
  const policy = caps.guided_build;
  const draft = useDraft<BuildDraft>(
    `/projects/${pid}/build-draft`,
    buildPayload,
    undefined,
    policy.autosave_ms,
  );
  const [catalog, setCatalog] = useState<Template[] | null>(null),
    [catalogError, setCatalogError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false),
    [preparing, setPreparing] = useState(false);
  const [tab, setTab] = useState("all"),
    [query, setQuery] = useState(""),
    [category, setCategory] = useState("");
  const [recommendation, setRecommendation] = useState<Recommendation | null>(
      null,
    ),
    [recommendationObjective, setRecommendationObjective] = useState("");
  const [editor, setEditor] = useState<{
      template: Template;
      attachment: Attachment;
    } | null>(null),
    [opened, setOpened] = useState<Attachment | null>(null);
  const current = useRef(draft.data),
    request = useRef(0),
    scope = useRef(pid);
  current.current = draft.data;
  scope.current = pid;
  const messageInput = useRef<HTMLTextAreaElement>(null);
  async function loadCatalog() {
    setCatalogError(null);
    try {
      setCatalog((await api<{ items: Template[] }>("/templates")).items);
    } catch (e) {
      setCatalogError(e);
    }
  }
  useEffect(() => {
    void loadCatalog();
    return () => {
      request.current++;
    };
  }, [pid]);
  useEffect(() => {
    if (reuseRequest && draft.data) {
      if (
        !draft.data.message_text ||
        draft.data.message_text === reuseRequest ||
        confirm("Substituir o texto do próximo pedido?")
      ) {
        draft.setData((d) => ({
          ...d,
          message_text: reuseRequest,
          stage: "conversation",
        }));
        messageInput.current?.focus();
      }
      onReused();
    }
  }, [reuseRequest, !!draft.data]);
  useEffect(() => {
    if (replyTo && draft.data) {
      draft.setData((d) => ({ ...d, stage: "conversation" }));
      messageInput.current?.focus();
    }
  }, [replyTo]);
  useEffect(() => {
    onFlushReady(draft.flush);
  }, [draft.flush]);
  useEffect(() => {
    const id = draft.data?.recommendation_id;
    if (!id || recommendation?.recommendation_id === id) return;
    let cancelled = false;
    void api<Recommendation>(`/projects/${pid}/recommendations/${id}`)
      .then(async (result) => {
        if (cancelled) return;
        setRecommendation(result);
        const objective = draft.data?.objective || "";
        const hash = Array.from(
          new Uint8Array(
            await crypto.subtle.digest(
              "SHA-256",
              new TextEncoder().encode(JSON.stringify(objective)),
            ),
          ),
        )
          .map((b) => b.toString(16).padStart(2, "0"))
          .join("");
        if (!cancelled)
          setRecommendationObjective(
            hash === result.objective_hash ? objective : "",
          );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [pid, draft.data?.recommendation_id]);
  async function editAttachment(value: Attachment) {
    try {
      const template = await api<Template>(
        `/templates/versions/${value.source_version_id}`,
      );
      setEditor({ template, attachment: value });
    } catch (e) {
      setError(e);
    }
  }
  const d = draft.data;
  if (!d)
    return (
      <>
        <ErrorNotice error={draft.error} />
        {!draft.error && <Loading />}
      </>
    );
  const selected = d.attachments.filter((a) => a.enabled),
    overLimit =
      d.objective.length > policy.max_objective_chars ||
      d.message_text.length > policy.max_message_chars ||
      selected.length > policy.max_attachments,
    disable = busy || blocked || !canWrite || !!draft.conflict;
  const recommended = new Set(
    recommendation?.items.map((i) => i.template_id) || [],
  );
  const stale = !!recommendation && recommendationObjective !== d.objective;
  const templates = (catalog || []).filter(
    (t) =>
      (tab !== "recommended" || recommended.has(t.id)) &&
      (!category || t.category === category) &&
      `${t.title} ${t.summary}`
        .toLocaleLowerCase("pt-BR")
        .includes(query.toLocaleLowerCase("pt-BR")),
  );
  function attachment(template: Template) {
    return (
      d!.attachments.find((a) => a.template_id === template.id) || {
        template_id: template.id,
        source_version_id: template.version_id,
        edited_content: template.content,
        fields: {},
        enabled: false,
        title: template.title,
        version: template.version,
        category: template.category,
      }
    );
  }
  function update(value: Attachment) {
    draft.setData((old) => ({
      ...old,
      attachments: [
        ...old.attachments.filter((a) => a.template_id !== value.template_id),
        value,
      ],
    }));
  }
  async function step(stage: BuildDraft["stage"], without = false) {
    setError(null);
    draft.setData((old) => ({
      ...old,
      stage,
      message_text:
        old.stage === "idea" && !old.message_text
          ? old.objective
          : old.message_text,
      attachments: without
        ? old.attachments.map((a) => ({ ...a, enabled: false }))
        : old.attachments,
    }));
    try {
      await draft.flush();
    } catch (e) {
      setError(e);
    }
  }
  async function prepare() {
    if (preparing || !d!.objective.trim()) return;
    const id = ++request.current,
      objective = d!.objective,
      project = pid;
    setPreparing(true);
    setError(null);
    try {
      await draft.flush();
      const result = await command<Recommendation>(
        `/projects/${pid}/recommendations`,
        { expected_draft_revision: draft.version() },
      );
      if (
        id !== request.current ||
        scope.current !== project ||
        current.current?.objective !== objective
      )
        return;
      setRecommendation(result);
      setRecommendationObjective(objective);
      setTab(result.items.length ? "recommended" : "all");
      draft.setData((old) => ({
        ...old,
        recommendation_id: result.recommendation_id || null,
      }));
      await step("templates");
    } catch (e) {
      setError(e);
      if (scope.current === project && current.current?.objective === objective)
        await step("templates");
    } finally {
      if (scope.current === project) setPreparing(false);
    }
  }
  async function send() {
    if (
      disable ||
      overLimit ||
      active ||
      pendingProposal ||
      !caps.generation.enabled ||
      !d!.message_text.trim()
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await draft.flush();
      const revision = await beforeSend();
      const op = await command<Operation>(`/projects/${pid}/messages`, {
        expected_draft_revision: draft.version(),
        base_project_revision: revision,
        policy_version: caps.policy_version,
        reply_to: replyTo,
      });
      onAccepted(op);
      await draft.reload();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const stageNumber = { idea: 1, templates: 2, review: 3, conversation: 0 }[
    d.stage
  ];
  return (
    <div className="guided-build">
      {hasHistory && d.stage !== "conversation" && (
        <button
          className="text-button"
          onClick={() => void step("conversation")}
          disabled={disable}
        >
          Voltar à conversa
        </button>
      )}
      <GuidedSteps stageNumber={stageNumber} />
      <ErrorNotice error={error || draft.error} />
      {overLimit && (
        <p className="notice error" role="alert">
          O pedido excede os limites indicados. Reduza o conteúdo para
          continuar; nenhum trecho foi cortado.
        </p>
      )}
      <p className="small muted" role="status">
        {draft.status}
      </p>
      {!!draft.error && !draft.conflict && (
        <button
          className="button"
          onClick={() => void draft.flush().catch(setError)}
        >
          Tentar salvar novamente
        </button>
      )}
      {d.stage === "idea" && (
        <>
          <div className="guided-intro">
            <Sparkles size={27} />
            <div>
              <h3>O que você quer construir?</h3>
              <p>Vamos preparar seu pedido antes de gerar código.</p>
            </div>
          </div>
          <label className="guided-label" htmlFor="build-idea">
            Descreva sua ideia
          </label>
          <textarea
            id="build-idea"
            value={d.objective}
            aria-invalid={d.objective.length > policy.max_objective_chars}
            rows={6}
            disabled={!canWrite || busy}
            placeholder="Quero uma aplicação com login e dados privados por usuário…"
            onChange={(e) =>
              draft.setData((old) => ({ ...old, objective: e.target.value }))
            }
          />
          <p className="guided-counter">
            {d.objective.length}/{policy.max_objective_chars}
          </p>
          <div className="guided-actions">
            <button
              className="button primary"
              disabled={
                disable || overLimit || preparing || !d.objective.trim()
              }
              onClick={() => void prepare()}
            >
              <Sparkles size={17} />
              {preparing ? "Preparando sugestões…" : "Preparar construção"}
            </button>
            <button
              className="button"
              disabled={
                disable || overLimit || preparing || !d.objective.trim()
              }
              onClick={() => void step("review", true)}
            >
              <FileText size={17} />
              Revisar sem templates
            </button>
          </div>
          <p className="small muted">
            Preparar sugere templates. Somente o envio inicia a geração de
            código.
          </p>
        </>
      )}
      {d.stage === "templates" && (
        <>
          <h3>Escolha e adapte</h3>
          <p>Selecione os templates que vão orientar seu pedido.</p>
          <p className="guided-objective">{d.objective}</p>
          {stale && (
            <p className="notice">
              A ideia mudou. Volte à ideia para solicitar novas sugestões. Suas
              escolhas foram preservadas.
            </p>
          )}
          {(!recommendation || recommendation.status !== "ready") && (
            <p className="notice">
              Você pode explorar todo o catálogo ou continuar sem templates.
            </p>
          )}
          <div
            className="filter-tabs"
            role="group"
            aria-label="Catálogo de templates"
          >
            <button
              className={tab === "recommended" ? "selected" : ""}
              aria-pressed={tab === "recommended"}
              onClick={() => setTab("recommended")}
            >
              Recomendados
            </button>
            <button
              className={tab === "all" ? "selected" : ""}
              aria-pressed={tab === "all"}
              onClick={() => setTab("all")}
            >
              Todos os templates
            </button>
          </div>
          <div className="guided-filters">
            <label className="search">
              <Search size={16} />
              <span className="sr-only">Buscar templates</span>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Buscar templates…"
              />
            </label>
            <label>
              <span className="sr-only">Categoria</span>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                <option value="">Todas as categorias</option>
                {Object.entries(categories).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <ErrorNotice error={catalogError} />
          {catalogError && (
            <button className="button" onClick={() => void loadCatalog()}>
              Recarregar catálogo
            </button>
          )}
          {!catalog && !catalogError && <Loading />}
          <div className="template-list">
            {templates.map((template) => (
              <GuidedTemplateCard
                key={template.id}
                template={template}
                attachment={attachment(template)}
                category={categories[template.category]}
                recommended={recommended.has(template.id)}
                disabled={disable}
                atLimit={selected.length >= policy.max_attachments}
                update={update}
                edit={(a) => void editAttachment(a)}
              />
            ))}
          </div>
          {catalog && !templates.length && (
            <p>
              Nenhum template nesta busca. Explore outra aba ou limpe os
              filtros. Sua seleção continua salva.
            </p>
          )}
          <p className="small">
            <strong>{selected.length} selecionado(s)</strong> · Selecionar e
            editar não inicia a construção.
          </p>
          <div className="guided-actions">
            <button
              className="button"
              disabled={disable}
              onClick={() => void step("idea")}
            >
              <ArrowLeft size={15} />
              Editar ideia
            </button>
            <button
              className="button"
              disabled={disable}
              onClick={() => void step("review", true)}
            >
              Continuar sem templates
            </button>
            <button
              className="button primary"
              disabled={disable}
              onClick={() => void step("review")}
            >
              {selected.length
                ? `Revisar com ${selected.length} templates`
                : "Revisar sem templates"}
              <ArrowRight size={15} />
            </button>
          </div>
        </>
      )}
      {d.stage === "conversation" && children}
      {["review", "conversation"].includes(d.stage) && (
        <>
          {d.stage === "review" && (
            <>
              <h3>Revise antes de construir</h3>
              <p>{d.objective}</p>
            </>
          )}
          <BuildAttachments
            selected={selected}
            catalog={catalog}
            disable={disable}
            open={setOpened}
            edit={(a) => void editAttachment(a)}
            remove={(a) => update({ ...a, enabled: false })}
          />

          <button
            className="text-button guided-add"
            disabled={disable}
            onClick={() => void step("templates")}
          >
            <Plus size={16} />
            {d.stage === "conversation"
              ? "Editar anexos do próximo pedido"
              : "Adicionar outro template"}
          </button>
          <form
            className="chat-composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            {replyTo && (
              <p className="small muted">Respondendo à pergunta da conversa.</p>
            )}
            <label htmlFor="build-message">
              {replyTo
                ? "Sua resposta"
                : d.stage === "conversation"
                  ? "O que vamos fazer em seguida?"
                  : "O que vamos construir?"}
            </label>
            <textarea
              id="build-message"
              ref={messageInput}
              aria-label="Pedido para a IA"
              value={d.message_text}
              aria-invalid={d.message_text.length > policy.max_message_chars}
              rows={4}
              disabled={!canWrite || busy}
              placeholder="Descreva o resultado esperado e como verificar…"
              onChange={(e) =>
                draft.setData((old) => ({
                  ...old,
                  message_text: e.target.value,
                }))
              }
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <p className="guided-counter">
              {d.message_text.length}/{policy.max_message_chars}
            </p>
            {active && (
              <p className="small">
                Você pode preparar o próximo pedido. O envio exige um novo
                clique após a conclusão.
              </p>
            )}
            {pendingProposal && (
              <p className="notice">
                Revise e aplique ou descarte a proposta anterior antes de enviar
                outro pedido.
              </p>
            )}
            {!caps.generation.enabled && (
              <p className="notice">
                A integração de IA precisa ser habilitada pelo responsável.
              </p>
            )}
            <div className="guided-actions">
              <button
                type="button"
                className="button"
                disabled={disable}
                onClick={() => void step("templates")}
              >
                {d.stage === "conversation"
                  ? "Rever recomendações"
                  : "Voltar aos templates"}
              </button>
              <button
                className="button primary"
                disabled={
                  disable ||
                  overLimit ||
                  active ||
                  pendingProposal ||
                  !caps.generation.enabled ||
                  !d.message_text.trim()
                }
              >
                <Send size={16} />
                {busy
                  ? "Enviando…"
                  : active
                    ? "Enviar após concluir"
                    : "Enviar para construir"}
              </button>
            </div>
            <p className="small muted">
              Só este envio inicia a geração. As alterações exigem sua revisão
              antes de serem aplicadas.
            </p>
          </form>
        </>
      )}
      {editor && (
        <TemplateEditor
          {...editor}
          limit={policy.max_attachment_chars}
          close={() => setEditor(null)}
          onSave={async (value) => {
            update(value);
            await draft.flush();
          }}
        />
      )}
      {opened && (
        <Modal
          title={`Conteúdo do anexo · ${opened.title || opened.template_id}`}
          close={() => setOpened(null)}
        >
          <pre className="source-preview">{opened.edited_content}</pre>
        </Modal>
      )}
      {draft.conflict && (
        <DraftConflict
          local={d}
          remote={draft.conflict.data}
          onResolve={(remote) => {
            draft.resolve(remote);
            if (!remote) void draft.flush().catch(setError);
          }}
        />
      )}
    </div>
  );
}
