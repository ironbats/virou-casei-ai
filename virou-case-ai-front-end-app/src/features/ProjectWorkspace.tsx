import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  BookOpen,
  Code2,
  Download,
  Eye,
  History,
  MessageSquare,
  Save,
  Send,
  Upload,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useSession } from "../app/session";
import { api, ApiError, command } from "../shared/api";
import { ErrorNotice, Loading, Modal } from "../shared/ui";
import type {
  Capabilities,
  Operation,
  Project,
  ProjectDraft,
  Revision,
} from "../shared/types";
import { BuildConversation } from "./BuildConversation";
import { useProjectOperations, inProgress } from "./useProjectOperations";
import { useDraft } from "./useDraft";
import { mergeProjectDraft } from "./mergeProjectDraft";
import { DraftConflict } from "./DraftConflict";
import { ProjectPreview } from "./ProjectPreview";
import { lazy, Suspense, type ComponentProps } from "react";
const Editor = lazy(() =>
  import("./ProjectEditor").then((m) => ({ default: m.ProjectEditor })),
);
function ProjectEditor(
  props: ComponentProps<typeof import("./ProjectEditor").ProjectEditor>,
) {
  return (
    <Suspense fallback={<Loading />}>
      <Editor {...props} />
    </Suspense>
  );
}

export async function revisionFiles(pid: string, rid: string | null) {
  if (!rid) return {};
  const revision = await api<Revision>(`/projects/${pid}/revisions/${rid}`);
  const entries = await Promise.all(
    revision.files.map((f) =>
      api<{ path: string; content: string }>(
        `/projects/${pid}/revisions/${rid}/files/${f.id}`,
      ),
    ),
  );
  return Object.fromEntries(entries.map((f) => [f.path, f.content]));
}
const draftPayload = (d: ProjectDraft) => ({
  base_revision_id: d.base_revision_id,
  files: d.files,
  prompt: d.prompt,
});
export function ProjectWorkspace() {
  const { projectId: pid = "" } = useParams(),
    { user } = useSession(),
    navigate = useNavigate();
  const draft = useDraft<ProjectDraft>(
    `/projects/${pid}/draft`,
    draftPayload,
    mergeProjectDraft,
  );
  const [project, setProject] = useState<Project | null>(null),
    [caps, setCaps] = useState<Capabilities | null>(null),
    [baseFiles, setBaseFiles] = useState<Record<string, string> | null>(null),
    [selected, setSelected] = useState("public/index.html"),
    [tab, setTab] = useState("code"),
    [mobile, setMobile] = useState("code"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [message, setMessage] = useState(""),
    [sending, setSending] = useState(""),
    [replyTo, setReplyTo] = useState<string | null>(null),
    [newFile, setNewFile] = useState(false),
    [editing, setEditing] = useState(false),
    [exporting, setExporting] = useState(false),
    [review, setReview] = useState<{
      op: Operation;
      files: Record<string, string>;
    } | null>(null),
    [remote, setRemote] = useState<{
      project: Project;
      files: Record<string, string>;
    } | null>(null);
  const input = useRef<HTMLTextAreaElement>(null),
    tracking = useProjectOperations(pid);
  const canWrite = !!user?.access.allowed_actions.includes("write"),
    canExport = user?.access.allowed_actions.includes("export");
  const files = draft.data?.files ?? baseFiles ?? {},
    dirty =
      !!baseFiles &&
      (Object.keys(files).length !== Object.keys(baseFiles).length ||
        Object.keys(files).some((path) => files[path] !== baseFiles[path])),
    active = tracking.items.find(
      (o) => o.type === "generation" && inProgress(o),
    );
  const syncedOperations = useRef(new Set<string>());
  const syncFailedAt = useRef(0);
  const currentScope = useRef(pid);
  currentScope.current = pid;
  const latestBuild = tracking.items.find((o) => o.type === "generation");
  useEffect(() => {
    if (
      latestBuild?.state === "succeeded" &&
      latestBuild.result_ref.kind === "clarification"
    )
      setReplyTo(latestBuild.id);
  }, [latestBuild?.id, latestBuild?.state, latestBuild?.result_ref.kind]);
  useEffect(() => {
    if (
      !project ||
      !draft.data ||
      !baseFiles ||
      busy ||
      Date.now() - syncFailedAt.current < 3000
    )
      return;
    const applied = tracking.items.find(
      (o) =>
        o.type === "generation" &&
        o.state === "succeeded" &&
        o.result_ref.applied_revision_id,
    );
    if (!applied || syncedOperations.current.has(applied.id)) return;
    if (
      project.current_revision_id === applied.result_ref.applied_revision_id &&
      draft.data.base_revision_id === project.current_revision_id
    ) {
      syncedOperations.current.add(applied.id);
      return;
    }
    setBusy(true);
    void (async () => {
      const current = await api<Project>(`/projects/${pid}`);
      const incoming = await revisionFiles(pid, current.current_revision_id);
      if (currentScope.current !== pid) return;
      if (dirty) {
        if (current.current_revision_id !== draft.data!.base_revision_id)
          setRemote({ project: current, files: incoming });
      } else {
        await draft.reload();
        if (currentScope.current !== pid) return;
        setProject(current);
        setBaseFiles(incoming);
        setSelected((path) =>
          path in incoming ? path : (Object.keys(incoming)[0] ?? ""),
        );
        if (
          current.current_revision_id === applied.result_ref.applied_revision_id
        )
          setMessage("Alterações da IA aplicadas e salvas no projeto.");
      }
      syncedOperations.current.add(applied.id);
      setError(null);
    })()
      .catch((e) => {
        syncFailedAt.current = Date.now();
        if (currentScope.current === pid) setError(e);
      })
      .finally(() => setBusy(false));
  }, [tracking.items, project, !!draft.data, !!baseFiles, busy, dirty]);
  useEffect(() => {
    let alive = true;
    Promise.all([
      api<Project>(`/projects/${pid}`),
      api<Capabilities>("/capabilities"),
    ])
      .then(([p, c]) => {
        if (alive) {
          setProject(p);
          setCaps(c);
        }
      })
      .catch(setError);
    return () => {
      alive = false;
    };
  }, [pid]);
  useEffect(() => {
    if (!draft.data) return;
    let alive = true;
    revisionFiles(pid, draft.data.base_revision_id)
      .then((f) => {
        if (alive) {
          setBaseFiles(f);
          setSelected((s) => (s in f ? s : (Object.keys(f)[0] ?? "")));
        }
      })
      .catch(setError);
    return () => {
      alive = false;
    };
  }, [pid, draft.data?.base_revision_id]);
  const transferFocused = useRef(false);
  useEffect(() => {
    if (
      transferFocused.current ||
      !draft.data ||
      !project ||
      !caps ||
      !baseFiles ||
      !new URLSearchParams(location.search).has("prompt")
    )
      return;
    if (mobile !== "chat") {
      setMobile("chat");
      return;
    }
    input.current?.focus();
    transferFocused.current = true;
  }, [!!draft.data, !!project, !!caps, !!baseFiles, mobile]);
  // Flush draft before internal navigation; failed saves keep the editor and its buffers mounted.
  useEffect(() => {
    const guard = (event: MouseEvent) => {
      const anchor = (event.target as Element)?.closest(
        "a[href]",
      ) as HTMLAnchorElement | null;
      if (
        !anchor ||
        anchor.origin !== location.origin ||
        anchor.target ||
        anchor.pathname.includes("/api/") ||
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
  async function act(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(e);
      if (e instanceof ApiError && e.code === "VERSION_CONFLICT") {
        const p = await api<Project>(`/projects/${pid}`);
        setRemote({
          project: p,
          files: await revisionFiles(pid, p.current_revision_id),
        });
      }
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (!project || !draft.data) return;
    await draft.flush();
    const result = await command<{ project: Project; revision: Revision }>(
      `/projects/${pid}/revisions`,
      { base_revision_id: draft.data.base_revision_id, files },
      "POST",
      project.version,
    );
    setProject(result.project);
    setBaseFiles({ ...files });
    draft.setData((d) => ({
      ...d,
      base_revision_id: result.revision.id,
      files: null,
    }));
    await draft.flush();
    setMessage("Revisão salva.");
  }
  async function generate(event: FormEvent) {
    event.preventDefault();
    if (!draft.data || !caps || busy || active) return;
    const request = draft.data.prompt.trim();
    if (
      !canWrite ||
      !caps.generation.enabled ||
      request.length < 1 ||
      draft.data.prompt.length > caps.limits.max_prompt_chars ||
      tracking.loading ||
      tracking.error
    )
      return;
    setSending(request);
    await act(async () => {
      if (dirty) await save();
      await draft.flush();
      const current = await api<Project>(`/projects/${pid}`);
      draft.setData((d) => ({
        ...d,
        prompt: d.prompt.trim() === request ? "" : d.prompt,
      }));
      await draft.flush();
      try {
        const op = await command<Operation>(`/projects/${pid}/conversation`, {
          brief: request,
          base_revision_id: current.current_revision_id,
          policy_version: caps.policy_version,
          auto_apply: true,
          reply_to: replyTo,
        });
        setReplyTo(null);
        tracking.accept(op);
        tracking.refresh();
      } catch (failure) {
        draft.setData((d) => ({ ...d, prompt: d.prompt || request }));
        throw failure;
      }
    });
    setSending("");
  }
  async function inspect(op: Operation) {
    await act(async () => {
      setReview({
        op,
        files: await revisionFiles(pid, op.result_ref.revision_id!),
      });
      setMobile("code");
    });
  }
  async function accept() {
    if (!review || !project || dirty) return;
    await act(async () => {
      await draft.flush();
      await command(
        `/projects/${pid}/proposals/${review.op.id}/apply`,
        {},
        "POST",
        project.version,
      );
      const p = await api<Project>(`/projects/${pid}`);
      setProject(p);
      setBaseFiles(review.files);
      draft.setData((d) => ({
        ...d,
        base_revision_id: p.current_revision_id,
        files: null,
      }));
      await draft.flush();
      tracking.refresh();
      setReview(null);
      setMessage("Alterações aplicadas à revisão salva.");
    });
  }
  async function exportProject() {
    if (!project) return;
    await act(async () => {
      await draft.flush();
      const current = await api<Project>(`/projects/${pid}`);
      const op = await command<Operation>(`/projects/${pid}/exports`, {
        revision_id: current.current_revision_id,
      });
      tracking.accept(op);
      tracking.refresh();
      setExporting(false);
      setMessage("Preparando download da revisão salva.");
    });
  }
  const downloads = tracking.items.filter(
    (o) => o.type === "export" && o.state === "succeeded",
  );
  if (!project || !caps || !draft.data || !baseFiles)
    return (
      <>
        <ErrorNotice error={error || draft.error} />
        {!error && !draft.error && <Loading />}
      </>
    );
  const blocked = busy || !canWrite || !!draft.conflict;
  return (
    <div className="project-workspace">
      <div className="workspace-heading">
        <div>
          <Link className="back-link" to="/app/projetos">
            Meus projetos
          </Link>
          <span className="muted"> / </span>
          <strong>{project.name}</strong>
          <p className="small muted" role="status">
            {draft.status}
            {dirty
              ? " · Alterações ainda não aplicadas à revisão"
              : " · Revisão salva"}
          </p>
        </div>
        <div className="actions">
          <button
            className="button"
            onClick={() => setEditing(true)}
            disabled={!canWrite}
          >
            Editar projeto
          </button>
          <Link
            className="icon-button"
            aria-label="Histórico de revisões"
            to={`/app/projetos/${pid}/historico`}
          >
            <History size={18} />
          </Link>
          <button
            className="button"
            disabled={!canExport || busy}
            onClick={() => setExporting(true)}
          >
            <Download size={16} />
            Baixar projeto
          </button>
          <button
            className="button primary"
            disabled={!caps.publication.enabled}
            title={caps.publication.reason_code}
            onClick={() =>
              void act(async () => {
                if (dirty) await save();
                await draft.flush();
                navigate(`/app/projetos/${pid}/publicar`);
              })
            }
          >
            <Upload size={16} />
            Publicar
          </button>
        </div>
      </div>
      <ErrorNotice error={error || draft.error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {!!draft.error && !draft.conflict && (
        <button className="button" onClick={() => void act(draft.flush)}>
          Tentar salvar rascunho
        </button>
      )}
      {project.current_revision_id !== draft.data.base_revision_id && (
        <div className="notice">
          A revisão salva mudou. Seu rascunho foi preservado.{" "}
          <button
            className="button"
            onClick={() =>
              void act(async () => {
                const p = await api<Project>(`/projects/${pid}`);
                setRemote({
                  project: p,
                  files: await revisionFiles(pid, p.current_revision_id),
                });
              })
            }
          >
            Comparar versões
          </button>
        </div>
      )}
      <div
        className="mobile-workspace-tabs"
        role="tablist"
        aria-label="Área do projeto"
      >
        {[
          ["code", "Código"],
          ["preview", "Prévia"],
          ["chat", "Conversa"],
        ].map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={mobile === key}
            onClick={() => {
              setMobile(key);
              if (key !== "chat") setTab(key);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <div className={"ide-layout mobile-" + mobile}>
        <section className="panel ide-panel">
          <div className="ide-title">
            <h1>{project.name}</h1>
            <div
              className="filter-tabs"
              role="tablist"
              aria-label="Visualização do projeto"
            >
              <button
                role="tab"
                aria-selected={tab === "code"}
                className={tab === "code" ? "selected" : ""}
                onClick={() => setTab("code")}
              >
                <Code2 size={16} />
                Código
              </button>
              <button
                role="tab"
                aria-selected={tab === "preview"}
                className={tab === "preview" ? "selected" : ""}
                onClick={() => setTab("preview")}
              >
                <Eye size={16} />
                Prévia
              </button>
            </div>
          </div>
          <div hidden={tab !== "code"}>
            <ProjectEditor
              files={files}
              base={baseFiles}
              selected={selected}
              onSelect={setSelected}
              readOnly={blocked || !!active}
              onChange={(path, content) =>
                draft.setData((d) => ({
                  ...d,
                  files: { ...(d.files ?? baseFiles), [path]: content },
                }))
              }
              onAdd={() => setNewFile(true)}
              onRemove={() => {
                if (confirm("Remover " + selected + " do rascunho?")) {
                  const next = { ...files };
                  delete next[selected];
                  draft.setData((d) => ({ ...d, files: next }));
                  setSelected(Object.keys(next)[0] ?? "");
                }
              }}
            />
          </div>
          <div hidden={tab !== "preview"}>
            {tab === "preview" &&
              (caps.visual_preview?.enabled ? (
                <ProjectPreview files={files} dirty={dirty} />
              ) : (
                <p className="notice">
                  A prévia visual precisa da versão atualizada do backend.
                  Recarregue após a atualização.
                </p>
              ))}
          </div>
          <div className="editor-footer">
            <span className="small muted">
              {dirty
                ? "Rascunho recuperável · salve para criar uma revisão"
                : "Revisão " + project.current_revision_id?.slice(0, 8)}
            </span>
            <button
              className="button primary"
              disabled={blocked || !dirty}
              onClick={() => void act(save)}
            >
              <Save size={15} />
              Salvar revisão
            </button>
          </div>
          <div className="learning-nudge compact">
            <BookOpen size={23} />
            <div>
              <strong>Construa melhor, com método.</strong>
              <p>Transforme sua ideia em um pedido claro e verificável.</p>
            </div>
            <Link to={`/app/aprendizado/sdd?project=${pid}`}>
              Explorar guias →
            </Link>
          </div>
        </section>
        <section className="panel conversation-panel">
          <div className="card-title">
            <h2>
              <MessageSquare size={19} />
              Construir com IA
            </h2>
            <span className="small muted">Conversa do projeto</span>
          </div>
          {!!tracking.error && (
            <div className="notice" role="alert">
              A conexão foi interrompida. O último estado foi preservado.
              <button className="button" onClick={tracking.refresh}>
                Reconectar
              </button>
            </div>
          )}
          <BuildConversation
            operations={tracking.items}
            pid={pid}
            busy={busy}
            syncedAt={tracking.syncedAt}
            loadedRevision={project.current_revision_id}
            onCancel={(op) =>
              void act(async () => {
                tracking.accept(
                  await command<Operation>(`/operations/${op.id}/cancel`),
                );
                tracking.refresh();
              })
            }
            onRetry={(request) => {
              if (
                !draft.data!.prompt ||
                confirm("Substituir o texto do próximo pedido?")
              ) {
                draft.setData((d) => ({ ...d, prompt: request }));
                input.current?.focus();
              }
            }}
            onFile={(path) => {
              setSelected(path);
              setTab("code");
              setMobile("code");
            }}
            onReview={inspect}
            onAnswer={(op) => {
              setReplyTo(op.id);
              input.current?.focus();
            }}
          />
          {tracking.cursor && (
            <button
              className="text-button"
              disabled={tracking.loadingMore}
              onClick={tracking.loadMore}
            >
              Conversas anteriores
            </button>
          )}
          {sending && (
            <div className="build-request" role="status">
              <strong>Você</strong>
              <p>{sending}</p>
              <span className="small">Enviando…</span>
            </div>
          )}
          <form className="chat-composer" onSubmit={generate}>
            {replyTo && (
              <div className="notice small">
                Respondendo à pergunta da conversa.{" "}
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setReplyTo(null)}
                >
                  Iniciar outro pedido
                </button>
              </div>
            )}
            <label htmlFor="project-prompt">
              {replyTo ? "Sua resposta" : "O que vamos construir?"}
            </label>
            {!!draft.data.provenance?.length && (
              <span className="small muted">
                Rascunho preparado com um guia · revise antes de enviar
              </span>
            )}
            <textarea
              id="project-prompt"
              ref={input}
              aria-label="Pedido para a IA"
              value={draft.data.prompt}
              aria-invalid={
                draft.data.prompt.length > caps.limits.max_prompt_chars
              }
              rows={4}
              disabled={!canWrite}
              placeholder="Descreva o resultado esperado e como verificar…"
              onChange={(e) =>
                draft.setData((d) => ({ ...d, prompt: e.target.value }))
              }
              onKeyDown={(e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
            />
            {dirty && (
              <p className="small">
                Suas alterações serão salvas antes de enviar o pedido.
              </p>
            )}
            {draft.data.prompt.length > caps.limits.max_prompt_chars && (
              <p role="alert">
                O pedido excede {caps.limits.max_prompt_chars} caracteres.
                Reduza o texto para salvar e enviar. Seu conteúdo foi preservado
                nesta tela.
              </p>
            )}
            {!caps.generation.enabled && (
              <p className="small">
                A integração de IA precisa ser habilitada pelo operador.
              </p>
            )}
            <button
              className="button primary full"
              disabled={
                blocked ||
                !!active ||
                !caps.generation.enabled ||
                draft.data.prompt.trim().length < 1 ||
                draft.data.prompt.length > caps.limits.max_prompt_chars ||
                tracking.loading ||
                !!tracking.error
              }
            >
              <Send size={16} />
              {active
                ? "Construindo…"
                : sending
                  ? "Enviando…"
                  : "Enviar para IA"}
            </button>
            <p className="small muted">
              Ctrl + Enter para enviar. Somente o envio usa IA; rascunhos e
              guias não consomem créditos.
            </p>
          </form>
        </section>
      </div>
      {downloads.length > 0 && (
        <div className="actions">
          {downloads.map((op) => (
            <a
              className="button"
              key={op.id}
              href={`/api/v1/exports/${op.id}/download`}
            >
              Baixar ZIP · revisão {op.result_ref.revision_id?.slice(0, 8)}
            </a>
          ))}
        </div>
      )}
      {review && (
        <Modal
          title="Revise as alterações propostas"
          close={() => setReview(null)}
        >
          <p>
            Revise o conteúdo antes de aplicar. A revisão atual permanece salva
            até sua confirmação.
          </p>
          {review.op.result_ref.changed_files?.map((f) => (
            <details key={f.path} open>
              <summary>
                {f.path} · {f.change === "created" ? "Criado" : "Alterado"}
              </summary>
              <div className="compare-grid">
                <div>
                  <h3>Revisão atual</h3>
                  <pre className="source-preview">
                    {baseFiles[f.path] ?? "Arquivo novo"}
                  </pre>
                </div>
                <div>
                  <h3>Proposta</h3>
                  <pre className="source-preview">{review.files[f.path]}</pre>
                </div>
              </div>
            </details>
          ))}
          <p className="small">
            Estrutura conferida; testes e execução do projeto gerado não foram
            realizados.
          </p>
          {dirty && <p>Salve ou revise seu rascunho antes de aplicar.</p>}
          <ErrorNotice error={error} />
          <button
            className="button primary"
            disabled={
              blocked || dirty || !!review.op.result_ref.applied_revision_id
            }
            onClick={accept}
          >
            Aplicar alterações revisadas
          </button>
        </Modal>
      )}
      {remote && (
        <Modal
          title="Compare a revisão mais recente"
          close={() => setRemote(null)}
        >
          <p>
            Reaplicar transporta os arquivos que você alterou para a nova base.
            Revise colisões antes de salvar.
          </p>
          <div className="compare-grid">
            <div>
              <h3>Seu rascunho</h3>
              <pre className="source-preview">
                {JSON.stringify(files, null, 2)}
              </pre>
            </div>
            <div>
              <h3>Revisão salva</h3>
              <pre className="source-preview">
                {JSON.stringify(remote.files, null, 2)}
              </pre>
            </div>
          </div>
          <div className="actions">
            <button
              className="button"
              onClick={() => {
                setProject(remote.project);
                setBaseFiles(remote.files);
                draft.setData((d) => ({
                  ...d,
                  base_revision_id: remote.project.current_revision_id,
                  files: null,
                }));
                setRemote(null);
                setError(null);
              }}
            >
              Carregar revisão salva
            </button>
            <button
              className="button primary"
              onClick={() => {
                const merged = { ...remote.files };
                for (const path of new Set([
                  ...Object.keys(baseFiles),
                  ...Object.keys(files),
                ])) {
                  if (baseFiles[path] !== files[path]) {
                    if (path in files) merged[path] = files[path];
                    else delete merged[path];
                  }
                }
                setProject(remote.project);
                setBaseFiles(remote.files);
                draft.setData((d) => ({
                  ...d,
                  base_revision_id: remote.project.current_revision_id,
                  files: merged,
                }));
                setRemote(null);
                setError(null);
              }}
            >
              Reaplicar minhas alterações
            </button>
          </div>
        </Modal>
      )}
      {draft.conflict && (
        <DraftConflict
          local={draft.data}
          remote={draft.conflict.data}
          onResolve={draft.resolve}
        />
      )}
      {exporting && (
        <Modal title="Baixar seu projeto" close={() => setExporting(false)}>
          <p>
            O ZIP contém a revisão salva{" "}
            {project.current_revision_id?.slice(0, 8)}.{" "}
            {dirty
              ? "Seu rascunho tem alterações que ainda não fazem parte dessa revisão."
              : ""}
          </p>
          <div className="actions">
            {dirty && (
              <button
                className="button"
                disabled={blocked}
                onClick={() =>
                  void act(async () => {
                    await save();
                    setExporting(false);
                    setMessage(
                      "Revisão salva. Você já pode baixar o projeto atualizado.",
                    );
                  })
                }
              >
                Salvar primeiro
              </button>
            )}
            <button
              className="button primary"
              disabled={busy}
              onClick={exportProject}
            >
              Baixar revisão salva
            </button>
          </div>
        </Modal>
      )}
      {newFile && (
        <Modal title="Novo arquivo" close={() => setNewFile(false)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const path = String(new FormData(e.currentTarget).get("path"));
              if (
                path in files ||
                Object.keys(files).length >= caps.limits.max_files
              ) {
                setError(
                  new Error(
                    "Arquivo existente ou limite de arquivos atingido.",
                  ),
                );
                return;
              }
              draft.setData((d) => ({ ...d, files: { ...files, [path]: "" } }));
              setSelected(path);
              setNewFile(false);
            }}
          >
            <label>
              Caminho relativo
              <input
                name="path"
                required
                maxLength={caps.limits.max_path_length}
                pattern="(?!/)(?!.*\.\.)(?!.*\\\\).+"
                placeholder="public/novo-arquivo.js"
              />
            </label>
            <button className="button primary">Adicionar arquivo</button>
          </form>
        </Modal>
      )}
      {editing && (
        <Modal title="Editar projeto" close={() => setEditing(false)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              void act(async () => {
                setProject(
                  await command<Project>(
                    `/projects/${pid}`,
                    {
                      name: form.get("name"),
                      objective: form.get("objective"),
                    },
                    "PATCH",
                    project.version,
                  ),
                );
                setEditing(false);
              });
            }}
          >
            <label>
              Nome
              <input
                name="name"
                defaultValue={project.name}
                required
                maxLength={100}
              />
            </label>
            <label>
              Objetivo
              <textarea
                name="objective"
                defaultValue={project.objective}
                required
                minLength={10}
                maxLength={3000}
              />
            </label>
            <ErrorNotice error={error} />
            <button className="button primary" disabled={busy}>
              Salvar
            </button>
          </form>
        </Modal>
      )}
    </div>
  );
}
