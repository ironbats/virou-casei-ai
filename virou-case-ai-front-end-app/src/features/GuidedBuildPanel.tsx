import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  ArrowLeft,
  ArrowRight,
  FileText,
  Folder,
  History,
  Info,
  Pencil,
  Plus,
  Search,
  Send,
  Sparkles,
} from "lucide-react";
import { api, ApiError, command } from "../shared/api";
import { ErrorNotice, Loading, Modal } from "../shared/ui";
import type { Capabilities, Operation } from "../shared/types";
import { useDraft } from "./useDraft";
import { useTemplateFieldGeneration } from "./useTemplateFieldGeneration";
import { DraftConflict } from "./DraftConflict";
import { TemplateEditor } from "./TemplateEditor";
import {
  TemplateFillRetryDialog,
  uncertainRetry,
} from "./TemplateFillRetryDialog";
import { GuidedTemplateCard } from "./GuidedTemplateCard";
import { GuidedSteps } from "./GuidedSteps";
import { BuildAttachments } from "./BuildAttachments";
import { BuildReplyComposer } from "./BuildReplyComposer";
import { attachmentIssues } from "./templateFields";
import { mergeBuildAttachment, mergeBuildDraft } from "./mergeBuildDraft";
import {
  buildPayload,
  type Attachment,
  type AttachmentIssue,
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

function fieldErrors(error: unknown): AttachmentIssue[] {
  if (
    !(error instanceof ApiError) ||
    !error.details ||
    typeof error.details !== "object" ||
    !("fields" in error.details) ||
    !Array.isArray(error.details.fields)
  )
    return [];
  return error.details.fields.filter(
    (item): item is AttachmentIssue =>
      !!item &&
      typeof item === "object" &&
      typeof item.attachment_id === "string" &&
      typeof item.code === "string" &&
      typeof item.message === "string",
  );
}

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
  replyText,
  replyFocusRequest,
  onNewRequest,
  onFlushReady,
  contextRevision,
  contextDirty,
  onSaveContext,
  onRefreshContext,
}: {
  pid: string;
  caps: Capabilities;
  canWrite: boolean;
  blocked: boolean;
  active: boolean;
  pendingProposal: boolean;
  hasHistory: boolean;
  children: ReactNode;
  beforeSend: () => Promise<string | null>;
  onAccepted: (op: Operation) => void;
  onFlushReady: (fn: () => Promise<void>) => void;
  reuseRequest: string | null;
  onReused: () => void;
  replyTo: string | null;
  replyText: string;
  replyFocusRequest: number;
  onNewRequest: () => void;
  contextRevision: string | null;
  contextDirty: boolean;
  onSaveContext: () => Promise<void>;
  onRefreshContext: () => Promise<void>;
}) {
  const policy = caps.guided_build;
  const knownVersions = useRef<Record<string, Template>>({});
  const draft = useDraft<BuildDraft>(
    `/projects/${pid}/build-draft`,
    buildPayload,
    (local, saved, remote) =>
      mergeBuildDraft(local, saved, remote, knownVersions.current),
    policy.selection_debounce_ms ?? policy.autosave_ms,
  );
  const [catalog, setCatalog] = useState<Template[] | null>(null);
  const [catalogError, setCatalogError] = useState<unknown>(null);
  const [versions, setVersions] = useState<Record<string, Template>>({});
  knownVersions.current = versions;
  const [schemaError, setSchemaError] = useState<unknown>(null);
  const [schemaTick, setSchemaTick] = useState(0);
  const [sendIssues, setSendIssues] = useState<AttachmentIssue[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [pollError, setPollError] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [exploring, setExploring] = useState(false);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [recommendation, setRecommendation] = useState<Recommendation | null>(
    null,
  );
  const [emptyConfirmation, setEmptyConfirmation] = useState(false);
  const [emptyChecked, setEmptyChecked] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [conversationRequested, setConversationRequested] = useState(false);
  const [replyBusy, setReplyBusy] = useState(false);
  const [editor, setEditor] = useState<{
    template: Template;
    attachment: Attachment;
    focusField?: string;
  } | null>(null);
  const [opened, setOpened] = useState<Attachment | null>(null);
  const [retrying, setRetrying] = useState<Attachment | null>(null);
  const retryIntents = useRef(
    new Map<string, { revision: number; error?: unknown }>(),
  );
  const retryTrigger = useRef<HTMLElement | null>(null);
  const current = useRef(draft.data);
  const scope = useRef(pid);
  const alive = useRef(true);
  const mutation = useRef(false);
  const editingRequest = useRef(0);
  const focusStage = useRef(false);
  const requestStarted = useRef(false);
  const handledReplyFocus = useRef(replyFocusRequest);
  const heading = useRef<HTMLHeadingElement>(null);
  const messageInput = useRef<HTMLTextAreaElement>(null);
  current.current = draft.data;
  scope.current = pid;
  const fieldGeneration = useTemplateFieldGeneration({
    pid,
    draft,
    current: () => current.current,
    blockedReason:
      blocked || !canWrite
        ? "Seu acesso não permite gerar um campo neste momento."
        : active || pendingProposal
          ? "Conclua a construção atual antes de gerar outro campo."
          : caps.template_preparation?.enabled === false
            ? "A geração de campos não está disponível neste momento."
            : draft.conflict
              ? "Resolva o conflito do rascunho antes de gerar."
              : contextDirty
                ? "Salve as alterações do projeto na etapa Ideia antes de gerar."
                : draft.data?.context_hash &&
                    draft.data.context_revision !== contextRevision
                  ? "O contexto salvo mudou. Atualize a preparação na etapa Ideia."
                  : !["ready", "fallback"].includes(
                        draft.data?.recommendation_status || "",
                      )
                    ? "Prepare o pedido atual na etapa Ideia antes de gerar."
                    : "",
  });
  const stage = draft.data?.stage;
  const conversationMode = conversationRequested || stage === "conversation";
  const sourceVersions =
    draft.data?.attachments
      .filter((item) => item.enabled)
      .map((item) => item.source_version_id)
      .sort()
      .join(",") || "";
  const fillSignature =
    draft.data?.attachments
      .filter((item) => item.enabled)
      .map(
        (item) =>
          `${item.template_id}:${item.fill_state}:${Object.entries(
            item.field_generations || {},
          )
            .map(([field, generation]) => `${field}:${generation.state}`)
            .join(",")}`,
      )
      .join(",") || "";
  const withinScope = () => alive.current && scope.current === pid;

  function changeDraft(update: (value: BuildDraft) => BuildDraft) {
    draft.setData((old) => {
      const next = update(old);
      current.current = next;
      return next;
    });
    setSendIssues([]);
  }

  async function loadCatalog() {
    setCatalogError(null);
    try {
      const items = (await api<{ items: Template[] }>("/templates")).items;
      if (!withinScope()) return;
      setCatalog(items);
      setVersions((old) => ({
        ...old,
        ...Object.fromEntries(
          items.map((template) => [template.version_id, template]),
        ),
      }));
    } catch (cause) {
      if (withinScope()) setCatalogError(cause);
    }
  }

  useEffect(() => {
    alive.current = true;
    void loadCatalog();
    return () => {
      alive.current = false;
      editingRequest.current++;
    };
  }, [pid]);

  useEffect(() => {
    if (!sourceVersions || !catalog) return;
    const missing = sourceVersions
      .split(",")
      .filter((identity) => !versions[identity]);
    if (!missing.length) return;
    let cancelled = false;
    const abort = new AbortController();
    setSchemaError(null);
    void Promise.all(
      missing.map(async (identity) => {
        const template = await api<Template>(
          `/templates/versions/${encodeURIComponent(identity)}`,
          { signal: abort.signal },
        );
        if (template.version_id !== identity)
          throw new Error("A versão do template não pôde ser conferida.");
        return [identity, template] as const;
      }),
    )
      .then((items) => {
        if (!cancelled)
          setVersions((old) => ({ ...old, ...Object.fromEntries(items) }));
      })
      .catch((cause) => {
        if (!cancelled) setSchemaError(cause);
      });
    return () => {
      cancelled = true;
      abort.abort();
    };
  }, [pid, sourceVersions, catalog, schemaTick]);

  useEffect(() => {
    const id = draft.data?.recommendation_id;
    if (!id || recommendation?.recommendation_id === id) return;
    let cancelled = false;
    void api<Recommendation>(`/projects/${pid}/recommendations/${id}`)
      .then((result) => {
        if (!cancelled) setRecommendation(result);
      })
      .catch((cause) => {
        if (!cancelled) setError(cause);
      });
    return () => {
      cancelled = true;
    };
  }, [pid, draft.data?.recommendation_id]);

  useEffect(() => {
    if (!draft.data || draft.conflict) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        await draft.reload();
        if (!cancelled) setPollError(null);
      } catch (cause) {
        if (!cancelled) setPollError(cause);
      }
      if (!cancelled)
        timer = setTimeout(
          () => void refresh(),
          /queued|filling|reconciling/.test(fillSignature) ? 1200 : 5000,
        );
    }
    timer = setTimeout(
      () => void refresh(),
      /queued|filling|reconciling/.test(fillSignature) ? 700 : 5000,
    );
    const visible = () => {
      if (document.visibilityState === "visible") {
        clearTimeout(timer);
        void refresh();
      }
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [pid, !!draft.data, !!draft.conflict, fillSignature]);

  useEffect(() => {
    onFlushReady(draft.flush);
  }, [draft.flush]);
  useEffect(() => {
    if (focusStage.current) {
      heading.current?.focus();
      focusStage.current = false;
    }
  }, [stage]);
  useEffect(() => {
    if (error) document.getElementById("build-workflow-error")?.focus();
  }, [error]);
  useEffect(() => {
    if (!reuseRequest || !draft.data) return;
    if (
      !draft.data.message_text ||
      draft.data.message_text === reuseRequest ||
      confirm("Substituir o texto do próximo pedido?")
    ) {
      changeDraft((old) => ({
        ...old,
        objective: reuseRequest,
        message_text: reuseRequest,
        stage: "idea",
        templates_completed: false,
        can_build: false,
        review_snapshot: null,
      }));
      setConversationRequested(false);
      requestStarted.current = true;
      onNewRequest();
      setShowHistory(false);
      setTimeout(() => messageInput.current?.focus(), 0);
    }
    onReused();
  }, [reuseRequest, !!draft.data]);
  useEffect(() => {
    const requested =
      replyFocusRequest > 0 && replyFocusRequest !== handledReplyFocus.current;
    handledReplyFocus.current = replyFocusRequest;
    if (!replyTo || !draft.data) return;
    const preparingNext =
      requestStarted.current ||
      draft.data.stage === "templates" ||
      draft.data.stage === "review" ||
      !!draft.data.message_text.trim();
    if (!requested && draft.data.stage !== "conversation" && preparingNext) {
      setShowHistory(true);
      return;
    }
    requestStarted.current = false;
    if (draft.data.stage !== "conversation")
      changeDraft((old) => ({ ...old, stage: "conversation" }));
    setConversationRequested(true);
    setShowHistory(true);
  }, [replyTo, replyFocusRequest, !!draft.data]);

  async function act(name: string, work: () => Promise<void>) {
    if (mutation.current) return;
    mutation.current = true;
    setBusy(name);
    setError(null);
    try {
      await work();
    } catch (cause) {
      if (withinScope()) {
        setError(cause);
        setSendIssues(fieldErrors(cause));
        if (cause instanceof ApiError && [409, 412].includes(cause.status))
          await draft.reload().catch(() => {});
      }
    } finally {
      if (withinScope()) setBusy("");
      mutation.current = false;
    }
  }

  async function goBack(next: "idea" | "templates") {
    await act("navigation", async () => {
      focusStage.current = true;
      changeDraft((old) => ({ ...old, stage: next }));
      await draft.flush();
    });
  }

  async function startNewRequest() {
    await act("navigation", async () => {
      requestStarted.current = true;
      onNewRequest();
      setConversationRequested(false);
      focusStage.current = true;
      changeDraft((old) => ({ ...old, stage: "idea" }));
      await draft.flush();
      if (withinScope()) messageInput.current?.focus();
    });
  }

  async function prepare() {
    if (!current.current?.message_text.trim()) return;
    await act("prepare", async () => {
      await draft.flush();
      const result = await command<Recommendation>(
        `/projects/${pid}/recommendations`,
        {
          expected_draft_revision: draft.version(),
          base_project_revision: contextRevision,
        },
      );
      if (!withinScope()) return;
      setRecommendation(result);
      setExploring(!result.items.length);
      focusStage.current = true;
      await draft.reload();
    });
  }

  async function review(confirmEmpty = false) {
    if (!current.current) return;
    if (
      !current.current.attachments.some((item) => item.enabled) &&
      !confirmEmpty
    ) {
      setEmptyChecked(false);
      setEmptyConfirmation(true);
      return;
    }
    await act("review", async () => {
      await draft.flush();
      await command(`/projects/${pid}/build-review`, {
        expected_draft_revision: draft.version(),
        confirm_empty: confirmEmpty,
      });
      if (!withinScope()) return;
      setEmptyConfirmation(false);
      focusStage.current = true;
      await draft.reload();
    });
  }

  async function editAttachment(value: Attachment, focusField?: string) {
    const request = ++editingRequest.current;
    try {
      const template =
        versions[value.source_version_id] ||
        (await api<Template>(
          `/templates/versions/${encodeURIComponent(value.source_version_id)}`,
        ));
      if (!withinScope() || request !== editingRequest.current) return;
      if (template.version_id !== value.source_version_id)
        throw new Error("Confira a versão deste template antes de editar.");
      setVersions((old) => ({ ...old, [template.version_id]: template }));
      const latest = current.current?.attachments.find(
        (item) => item.template_id === value.template_id,
      );
      if (latest && latest.source_version_id !== value.source_version_id)
        throw new Error("O anexo mudou. Abra a versão atual para editar.");
      setEditor({ template, attachment: latest || value, focusField });
    } catch (cause) {
      if (withinScope()) setError(cause);
    }
  }

  function updateAttachment(value: Attachment) {
    const previous = current.current?.attachments.find(
      (item) => item.template_id === value.template_id,
    );
    if (
      value.enabled &&
      !previous?.enabled &&
      !Object.keys(value.fields).length
    )
      value = { ...value, fill_state: "queued" };
    changeDraft((old) => ({
      ...old,
      attachments: [
        ...old.attachments.filter(
          (item) => item.template_id !== value.template_id,
        ),
        value,
      ],
      stage: "templates",
      templates_completed: false,
      review_snapshot: null,
      can_build: false,
      can_review: false,
    }));
  }

  function openRetry(value: Attachment) {
    if (mutation.current) return;
    retryTrigger.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setRetrying(value);
  }

  function closeRetry() {
    setRetrying(null);
    requestAnimationFrame(() => {
      if (!withinScope()) return;
      const trigger = retryTrigger.current;
      if (trigger?.isConnected && !trigger.matches(":disabled"))
        trigger.focus();
      else heading.current?.focus();
    });
  }

  async function retry(value: Attachment) {
    if (mutation.current) return;
    mutation.current = true;
    setBusy("retry");
    try {
      let intent = retryIntents.current.get(value.template_id);
      if (!intent) {
        await draft.flush();
        intent = { revision: draft.version() };
        retryIntents.current.set(value.template_id, intent);
      }
      await command(`/projects/${pid}/build-fill`, {
        expected_draft_revision: intent.revision,
        attachment_ids: [value.template_id],
      });
      retryIntents.current.delete(value.template_id);
      if (!withinScope()) return;
      closeRetry();
      await draft.reload().catch((cause) => {
        if (withinScope()) setPollError(cause);
      });
    } catch (cause) {
      const intent = retryIntents.current.get(value.template_id);
      if (uncertainRetry(cause) && intent) intent.error = cause;
      else retryIntents.current.delete(value.template_id);
      await draft.reload().catch(() => {});
      throw cause;
    } finally {
      if (withinScope()) setBusy("");
      mutation.current = false;
    }
  }

  async function saveEditedAttachment(value: Attachment, saved: Attachment) {
    if (!editor) return;
    const latest = current.current?.attachments.find(
      (item) => item.template_id === value.template_id,
    );
    if (
      latest &&
      (latest.source_version_id !== editor.attachment.source_version_id ||
        latest.enabled !== editor.attachment.enabled)
    )
      throw new Error(
        "A seleção deste anexo mudou. Feche a edição e confira o pedido atual; seu texto continua nesta janela.",
      );
    const merged = latest
      ? mergeBuildAttachment(value, saved, latest, editor.template)
      : value;
    if (!merged)
      throw new Error(
        "Este campo mudou enquanto você editava. Seu texto foi preservado nesta janela; compare com o rascunho atual antes de substituir.",
      );
    updateAttachment(merged);
    await draft.flush();
  }

  async function send() {
    const saved = current.current;
    if (
      !saved?.can_build ||
      !saved.review_snapshot ||
      active ||
      pendingProposal ||
      !caps.generation.enabled
    )
      return;
    await act("send", async () => {
      await draft.flush();
      const revision = await beforeSend();
      if (revision !== saved.review_snapshot!.context_revision)
        throw new Error(
          "A revisão salva do projeto mudou. Volte à ideia e prepare o contexto atual antes de construir.",
        );
      const operation = await command<Operation>(`/projects/${pid}/messages`, {
        expected_draft_revision: draft.version(),
        base_project_revision: revision,
        review_snapshot_hash: saved.review_snapshot!.hash,
        policy_version: caps.policy_version,
        reply_to: null,
      });
      if (!withinScope()) return;
      requestStarted.current = false;
      setConversationRequested(true);
      onAccepted(operation);
      setShowHistory(true);
      await draft.reload();
    });
  }

  const d = draft.data;
  if (!d)
    return (
      <>
        <ErrorNotice error={draft.error} />
        {!draft.error && <Loading />}
      </>
    );
  const selected = d.attachments.filter((item) => item.enabled);
  const disabled = !!busy || blocked || !canWrite || !!draft.conflict;
  const overLimit =
    d.objective.length > policy.max_objective_chars ||
    d.message_text.length > policy.max_message_chars ||
    selected.length > policy.max_attachments;
  const issuesByAttachment = Object.fromEntries(
    selected.map((item) => [
      item.template_id,
      [
        ...attachmentIssues(
          item,
          versions[item.source_version_id],
          policy.max_attachment_chars,
        ),
        ...(item.validation_errors || []),
        ...sendIssues.filter(
          (issue) => issue.attachment_id === item.template_id,
        ),
      ].filter(
        (issue, index, all) =>
          all.findIndex(
            (known) =>
              known.field_id === issue.field_id && known.code === issue.code,
          ) === index,
      ),
    ]),
  );
  const pendingIssues = Object.values(issuesByAttachment).flat();
  const filling =
    fieldGeneration.reviewPending ||
    selected.some((item) =>
      ["queued", "filling", "reconciling"].includes(item.fill_state || ""),
    );
  const recommendationReady = ["ready", "fallback"].includes(
    d.recommendation_status || "",
  );
  const staleContext =
    !!d.context_hash && d.context_revision !== contextRevision;
  const retryAttachment = retrying
    ? d.attachments.find((item) => item.template_id === retrying.template_id)
    : null;
  const retryBlockedReason =
    blocked || !canWrite
      ? "Seu acesso não permite uma nova tentativa neste momento."
      : draft.conflict
        ? "Feche esta janela e resolva o conflito do rascunho antes de continuar."
        : staleContext
          ? "O contexto salvo mudou. Feche esta janela e atualize a preparação na etapa Ideia."
          : retrying &&
              (!retryAttachment?.enabled ||
                retryAttachment.source_version_id !==
                  retrying.source_version_id)
            ? "A seleção deste template mudou. Feche esta janela e confira o pedido atual."
            : retryAttachment && retryAttachment.fill_state !== "failed"
              ? "O status deste template mudou. Feche esta janela para acompanhar o preenchimento atual."
              : fieldGeneration.pending
                ? "Aguarde ou verifique a geração de campo em andamento antes de iniciar outra tentativa."
                : "";
  const blockers = (d.blocking_reasons || []).filter(
    (item) => item.code !== "EMPTY_SELECTION_UNCONFIRMED",
  );
  const blocker = staleContext
    ? "A revisão salva mudou. Volte à ideia para atualizar a preparação."
    : filling
      ? "Aguarde o preenchimento dos templates selecionados. Você pode editar ou remover um anexo durante a preparação."
      : pendingIssues[0]?.message || blockers[0]?.message || "";
  const recommended = new Set(
    recommendation?.items.map((item) => item.template_id) || [],
  );
  const templates = (catalog || []).filter(
    (template) =>
      template.available &&
      (exploring ||
        recommended.has(template.id) ||
        selected.some((item) => item.template_id === template.id)) &&
      (!category || template.category === category) &&
      `${template.title} ${template.summary}`
        .toLocaleLowerCase("pt-BR")
        .includes(query.toLocaleLowerCase("pt-BR")),
  );
  const reviewed = d.review_snapshot;
  const reviewedAttachments = reviewed
    ? reviewed.attachments.map((item) => {
        const original = selected.find(
          (attachment) =>
            attachment.template_id ===
            (item.template_id || item.source_template_id || item.id),
        );
        return {
          ...original,
          ...item,
          template_id:
            item.template_id || item.source_template_id || item.id || "",
          enabled: true,
          edited_content: item.content ?? item.edited_content,
        } as Attachment;
      })
    : selected;
  const stageNumber = conversationMode
    ? 0
    : stage === "templates"
      ? 2
      : stage === "review"
        ? 3
        : 1;
  const displayRevision =
    stage === "idea" ? contextRevision : d.context_revision;
  const errorDisplay = error || draft.error;
  const status = (
    <p className="guided-save-status small muted" role="status">
      {draft.status}
    </p>
  );
  function attachment(template: Template): Attachment {
    return (
      d!.attachments.find((item) => item.template_id === template.id) || {
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
  return (
    <div
      className={`guided-build${conversationMode ? " is-conversation" : ""}`}
    >
      <div
        className={`guided-body${conversationMode ? " guided-conversation-body" : ""}`}
      >
        {!conversationMode && (
          <h3 ref={heading} tabIndex={-1} className="guided-stage-title">
            {stageNumber === 1
              ? "Toda melhoria começa por uma ideia"
              : stageNumber === 2
                ? "Templates para este pedido"
                : "Revise antes de construir"}
          </h3>
        )}
        {stageNumber === 1 && (
          <p className="muted">
            Descreva o que você quer. Vamos recomendar templates e preenchê-los
            com o contexto do seu projeto.
          </p>
        )}
        {!conversationMode && (
          <GuidedSteps
            stageNumber={stageNumber}
            disabled={disabled}
            onBack={(next) => void goBack(next)}
          />
        )}
        {!!errorDisplay && (
          <div id="build-workflow-error" tabIndex={-1}>
            <ErrorNotice error={errorDisplay} />
            {errorDisplay instanceof ApiError &&
              errorDisplay.code === "CONTEXT_STALE" && (
                <button
                  className="text-button"
                  disabled={disabled}
                  onClick={() => void act("refresh-context", onRefreshContext)}
                >
                  Atualizar contexto salvo
                </button>
              )}
          </div>
        )}
        {!!pollError && (
          <div className="notice" role="status">
            Não foi possível atualizar o pedido. O conteúdo foi preservado.
            <button
              className="text-button"
              onClick={() =>
                void draft
                  .reload()
                  .then(() => setPollError(null))
                  .catch(setPollError)
              }
            >
              Atualizar estado
            </button>
          </div>
        )}
        {overLimit && (
          <p className="notice error" role="alert">
            O pedido excede os limites indicados. Reduza o conteúdo para
            continuar; nenhum trecho foi cortado.
          </p>
        )}
        {!!draft.error && !draft.conflict && (
          <button
            className="button"
            onClick={() => void draft.flush().catch(setError)}
          >
            Tentar salvar novamente
          </button>
        )}
        {stageNumber === 1 && (
          <>
            <label className="guided-label" htmlFor="build-idea">
              O que vamos fazer?
            </label>
            <textarea
              id="build-idea"
              ref={messageInput}
              value={d.message_text}
              aria-invalid={
                d.message_text.length >
                Math.min(policy.max_objective_chars, policy.max_message_chars)
              }
              rows={6}
              disabled={!canWrite || !!busy}
              placeholder="Adicionar um botão Entrar na home que abre um modal de login, preservando o fluxo atual."
              onChange={(event) => {
                const text = event.target.value;
                changeDraft((old) => ({
                  ...old,
                  stage: "idea",
                  objective: text,
                  message_text: text,
                  templates_completed: false,
                  can_build: false,
                  review_snapshot: null,
                }));
              }}
            />
            <p className="guided-counter">
              {d.message_text.length}/
              {Math.min(policy.max_objective_chars, policy.max_message_chars)}
            </p>
            <details className="guided-context">
              <summary>
                <Folder size={18} aria-hidden="true" />
                Contexto:{" "}
                {contextRevision
                  ? "revisão salva do projeto"
                  : "projeto inicial vazio"}
                <Info size={16} aria-hidden="true" />
              </summary>
              <p className="small">
                {contextRevision
                  ? `Revisão ${contextRevision}. A preparação fixa esta versão do projeto.`
                  : "Este projeto ainda não possui uma revisão salva. O contexto inicial será vazio."}
              </p>
            </details>
            {contextDirty && (
              <div className="notice">
                <p>
                  Há alterações não salvas na revisão. Elas ainda não integram o
                  contexto da IA.
                </p>
                <button
                  className="text-button"
                  disabled={disabled}
                  onClick={() => void act("save-context", onSaveContext)}
                >
                  Salvar revisão do projeto
                </button>
              </div>
            )}
            <div
              className="guided-explanation"
              aria-label="Como a preparação funciona"
            >
              <span>
                <FileText size={22} aria-hidden="true" />
                Seu pedido
              </span>
              <ArrowRight size={18} aria-hidden="true" />
              <span>
                <Sparkles size={22} aria-hidden="true" />
                Jev recomenda templates
              </span>
              <ArrowRight size={18} aria-hidden="true" />
              <span>
                <FileText size={22} aria-hidden="true" />A IA preenche com seu
                contexto
              </span>
            </div>
          </>
        )}
        {stageNumber === 2 && (
          <>
            <p className="guided-objective">{d.message_text}</p>
            <div className="guided-section-title">
              <p>Jev recomendou. Você escolhe.</p>
              <button
                className="text-button"
                onClick={() => setExploring(!exploring)}
              >
                {exploring ? "Ver recomendações" : "Explorar catálogo"}
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            </div>
            {d.recommendation_status === "fallback" && (
              <p className="notice">
                O Jev está indisponível. A seleção manual no catálogo continua
                disponível; esta falha não significa que nenhum template seja
                aplicável.
              </p>
            )}
            {!recommendationReady && (
              <p className="notice">
                Conclua a preparação na etapa Ideia antes de selecionar os
                templates.
              </p>
            )}
            {staleContext && (
              <p className="notice">
                O contexto salvo mudou. Volte à ideia para atualizar a
                preparação; suas edições serão preservadas.
              </p>
            )}
            {exploring && (
              <div className="guided-filters">
                <label className="search">
                  <Search size={16} aria-hidden="true" />
                  <span className="sr-only">Buscar templates</span>
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Buscar templates…"
                  />
                </label>
                <label>
                  <span className="sr-only">Categoria</span>
                  <select
                    value={category}
                    onChange={(event) => setCategory(event.target.value)}
                  >
                    <option value="">Todas as categorias</option>
                    {Object.entries(categories).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            <ErrorNotice error={catalogError || schemaError} />
            {catalogError && (
              <button className="button" onClick={() => void loadCatalog()}>
                Recarregar catálogo
              </button>
            )}
            {schemaError && (
              <button
                className="button"
                onClick={() => setSchemaTick((tick) => tick + 1)}
              >
                Recarregar versões dos templates
              </button>
            )}
            {!catalog && !catalogError && <Loading />}
            <div className="template-list">
              {templates.map((template) => (
                <GuidedTemplateCard
                  key={template.id}
                  template={template}
                  schema={versions[attachment(template).source_version_id]}
                  attachment={attachment(template)}
                  category={categories[template.category] || template.category}
                  recommended={recommended.has(template.id)}
                  disabled={disabled || !recommendationReady || staleContext}
                  atLimit={selected.length >= policy.max_attachments}
                  update={updateAttachment}
                  edit={(value, field) => void editAttachment(value, field)}
                  retry={openRetry}
                  fieldGeneration={fieldGeneration}
                  issues={issuesByAttachment[template.id]}
                />
              ))}
            </div>
            {catalog && !templates.length && (
              <p className="guided-empty">
                {exploring
                  ? "Nenhum template nesta busca. Limpe os filtros para explorar o catálogo."
                  : "Nenhuma recomendação para este pedido. Explore o catálogo ou confirme que nenhum template é aplicável."}
              </p>
            )}
            <p className="guided-selection-note small muted">
              <Info size={16} aria-hidden="true" />
              Seleções novas são preenchidas automaticamente. Suas edições são
              preservadas.
            </p>
            {blocker && (
              <div className="notice" id="build-send-blocker" tabIndex={-1}>
                <p>{blocker}</p>
                {pendingIssues[0] && (
                  <button
                    className="text-button"
                    disabled={disabled}
                    onClick={() => {
                      const item = selected.find(
                        (value) =>
                          value.template_id === pendingIssues[0].attachment_id,
                      );
                      if (item)
                        void editAttachment(item, pendingIssues[0].field_id);
                    }}
                  >
                    Conferir campo pendente
                  </button>
                )}
              </div>
            )}
          </>
        )}
        {stageNumber === 3 && (
          <>
            <div className="guided-section-title">
              <h3>Resumo do pedido</h3>
              <button
                className="text-button"
                disabled={disabled}
                onClick={() => void goBack("idea")}
              >
                <Pencil size={15} aria-hidden="true" />
                Editar pedido
              </button>
            </div>
            <p className="guided-objective">
              {reviewed?.message_text || d.message_text || d.objective}
            </p>
            <p className="small muted">
              Contexto:{" "}
              {displayRevision
                ? `revisão ${displayRevision.slice(0, 12)}`
                : "projeto inicial vazio"}
            </p>
            <BuildAttachments
              selected={reviewedAttachments}
              catalog={catalog}
              disable={disabled}
              open={setOpened}
              edit={(value, field) => void editAttachment(value, field)}
              issues={issuesByAttachment}
              remove={(value) => {
                const original = selected.find(
                  (item) => item.template_id === value.template_id,
                );
                if (original) updateAttachment({ ...original, enabled: false });
                focusStage.current = true;
              }}
            />
            <button
              className="text-button guided-add"
              disabled={disabled}
              onClick={() => void goBack("templates")}
            >
              <Plus size={16} aria-hidden="true" />
              Adicionar ou trocar template
            </button>
            {(!d.can_build || blocker) && (
              <p className="notice" id="build-send-blocker" tabIndex={-1}>
                {blocker ||
                  "A revisão precisa ser atualizada. Volte aos templates e revise novamente antes de construir."}
              </p>
            )}
            {active && (
              <p className="notice">
                Há uma construção em andamento neste projeto. Aguarde a
                conclusão para enviar o próximo pedido.
              </p>
            )}
            {pendingProposal && (
              <p className="notice">
                Revise ou descarte a proposta anterior antes de iniciar outra
                construção.
              </p>
            )}
            {!caps.generation.enabled && (
              <p className="notice">
                A construção está temporariamente indisponível.
              </p>
            )}
          </>
        )}
        {conversationMode
          ? children
          : hasHistory && (
              <details
                className="guided-history"
                open={showHistory}
                onToggle={(event) => setShowHistory(event.currentTarget.open)}
              >
                <summary>
                  <History size={16} aria-hidden="true" />
                  Histórico de construções e propostas
                </summary>
                {children}
              </details>
            )}
      </div>
      <footer className="guided-footer">
        {conversationMode && (
          <>
            {replyTo ? (
              <BuildReplyComposer
                key={replyTo}
                pid={pid}
                operationId={replyTo}
                initialText={replyText}
                policyVersion={caps.policy_version}
                limit={Math.min(policy.max_message_chars, 12000)}
                focusRequest={replyFocusRequest}
                blockedReason={
                  blocked || !canWrite
                    ? "Seu acesso ou a conexão não permite enviar a resposta neste momento."
                    : active
                      ? "Aguarde a construção em andamento antes de responder."
                      : pendingProposal
                        ? "Revise ou descarte a proposta anterior antes de responder."
                        : !caps.generation.enabled
                          ? "A construção está temporariamente indisponível."
                          : draft.conflict
                            ? "Resolva o conflito do rascunho antes de responder."
                            : contextDirty
                              ? "Há alterações não salvas no código. Se salvar uma nova revisão, inicie uma nova construção com o contexto atualizado."
                              : ""
                }
                beforeSend={beforeSend}
                onBusyChange={(sending) => {
                  if (withinScope()) setReplyBusy(sending);
                }}
                onAccepted={(operation) => {
                  requestStarted.current = false;
                  setConversationRequested(true);
                  onAccepted(operation);
                  void draft.reload().catch(setPollError);
                }}
              />
            ) : (
              <p className="small muted" role="status">
                {active
                  ? "Acompanhe a construção nesta conversa."
                  : pendingProposal
                    ? "Revise as alterações propostas antes de aplicá-las ao projeto."
                    : "O histórico e o contexto desta construção continuam disponíveis."}
              </p>
            )}
            <button
              className="text-button"
              disabled={disabled || replyBusy}
              onClick={() => void startNewRequest()}
            >
              <Plus size={16} aria-hidden="true" />
              Nova construção
            </button>
          </>
        )}
        {stageNumber === 1 && (
          <div className="guided-consumption">
            <Sparkles size={22} aria-hidden="true" />
            <p>
              <strong>
                Jev recomenda templates. A IA preenche com seu contexto.
              </strong>
              <br />
              Selecionar autoriza o preenchimento automático. Você revisa antes
              de gerar código. Consome seu uso de IA.
            </p>
          </div>
        )}
        {stageNumber === 3 && (
          <div className="guided-consumption">
            <Info size={20} aria-hidden="true" />
            <p>
              {selected.length
                ? "Ao enviar, a IA recebe seu pedido e estes anexos."
                : "Ao enviar, a IA recebe apenas o pedido e o contexto revisado."}
            </p>
          </div>
        )}
        {!conversationMode &&
          (stageNumber === 1 ? (
            <button
              className="button primary full"
              disabled={disabled || overLimit || !d.message_text.trim()}
              onClick={() => void prepare()}
            >
              <Sparkles size={18} aria-hidden="true" />
              {busy === "prepare"
                ? "Buscando templates…"
                : "Preparar construção"}
            </button>
          ) : stageNumber === 2 ? (
            <>
              <p className="small guided-selected-count">
                {selected.length} template{selected.length === 1 ? "" : "s"}{" "}
                selecionado{selected.length === 1 ? "" : "s"}
              </p>
              <div className="guided-actions">
                <button
                  className="button"
                  disabled={disabled}
                  onClick={() => void goBack("idea")}
                >
                  <ArrowLeft size={16} aria-hidden="true" />
                  Voltar
                </button>
                <button
                  className="button primary"
                  disabled={
                    disabled ||
                    overLimit ||
                    filling ||
                    !!pendingIssues.length ||
                    staleContext ||
                    !recommendationReady ||
                    !d.can_review
                  }
                  onClick={() => void review()}
                >
                  {busy === "review" ? "Conferindo pedido…" : "Revisar pedido"}
                  <ArrowRight size={16} aria-hidden="true" />
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="guided-actions">
                <button
                  className="button primary"
                  disabled={
                    disabled ||
                    overLimit ||
                    active ||
                    pendingProposal ||
                    !caps.generation.enabled ||
                    !d.can_build ||
                    !reviewed ||
                    !!pendingIssues.length ||
                    filling ||
                    !!pollError ||
                    staleContext
                  }
                  onClick={() => void send()}
                >
                  <Send size={16} aria-hidden="true" />
                  {busy === "send" ? "Enviando…" : "Enviar para construir"}
                </button>
                <button
                  className="button"
                  disabled={disabled}
                  onClick={() => void goBack("templates")}
                >
                  <ArrowLeft size={16} aria-hidden="true" />
                  Voltar aos templates
                </button>
              </div>
              <p className="small muted">
                O código gerado será revisado antes de aplicar.
              </p>
              <p className="small muted">
                Próximo pedido: Ideia → Templates → Revisão
              </p>
            </>
          ))}
        {status}
      </footer>
      {retrying &&
        createPortal(
          <TemplateFillRetryDialog
            title={
              retrying.title ||
              versions[retrying.source_version_id]?.title ||
              "Template selecionado"
            }
            blockedReason={retryBlockedReason}
            initialError={retryIntents.current.get(retrying.template_id)?.error}
            confirm={() => retry(retrying)}
            close={closeRetry}
          />,
          document.body,
        )}
      {emptyConfirmation &&
        createPortal(
          <Modal
            title="Revisar sem anexos?"
            close={() => {
              if (!busy) setEmptyConfirmation(false);
            }}
          >
            <p>Nenhum template selecionado.</p>
            {d.recommendation_status === "fallback" && (
              <p className="notice">
                A recomendação do Jev está indisponível. Confirme a
                aplicabilidade após consultar o catálogo.
              </p>
            )}
            <label className="empty-selection-confirmation">
              <input
                type="checkbox"
                checked={emptyChecked}
                onChange={(event) => setEmptyChecked(event.target.checked)}
                disabled={!!busy}
              />
              <span>
                Confirmo que nenhum template é aplicável a este pedido e quero
                revisar sem anexos.
              </span>
            </label>
            <div className="actions">
              <button
                className="button"
                disabled={!!busy}
                onClick={() => setEmptyConfirmation(false)}
              >
                Cancelar
              </button>
              <button
                className="button primary"
                disabled={!emptyChecked || !!busy}
                onClick={() => void review(true)}
              >
                Confirmar e revisar
              </button>
            </div>
          </Modal>,
          document.body,
        )}
      {editor &&
        createPortal(
          <TemplateEditor
            {...editor}
            liveAttachment={d.attachments.find(
              (item) => item.template_id === editor.attachment.template_id,
            )}
            fieldGeneration={fieldGeneration}
            limit={policy.max_attachment_chars}
            close={() => setEditor(null)}
            onSave={saveEditedAttachment}
          />,
          document.body,
        )}
      {opened &&
        createPortal(
          <Modal
            title={`Conteúdo do anexo · ${opened.title || opened.template_id}`}
            close={() => setOpened(null)}
          >
            <pre className="source-preview guided-source-preview">
              {opened.edited_content}
            </pre>
          </Modal>,
          document.body,
        )}
      {draft.conflict &&
        createPortal(
          <DraftConflict
            local={d}
            remote={draft.conflict.data}
            onResolve={(remote) => {
              draft.resolve(remote);
              if (!remote) void draft.flush().catch(setError);
            }}
          />,
          document.body,
        )}
    </div>
  );
}
