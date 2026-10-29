import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  Code2,
  Download,
  Eye,
  History,
  MessageSquare,
  Save,
} from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useSession } from "../app/session";
import { api, ApiError, command } from "../shared/api";
import { ErrorNotice, Loading } from "../shared/ui";
import type {
  Capabilities,
  Operation,
  Project,
  ProjectDraft,
  Revision,
} from "../shared/types";
import { GuidedBuildPanel } from "./GuidedBuildPanel";
import { PublicationControl } from "./PublicationControl";
import type { Proposal, ReviewSelection } from "./proposalTypes";
import { BuildConversation } from "./BuildConversation";
import { useProjectOperations, inProgress } from "./useProjectOperations";
import { useDraft } from "./useDraft";
import { mergeProjectDraft } from "./mergeProjectDraft";
import { WorkspaceDialogs } from "./WorkspaceDialogs";
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
    [reuseRequest, setReuseRequest] = useState<string | null>(null),
    [replyTo, setReplyTo] = useState<string | null>(null),
    [replyText, setReplyText] = useState(""),
    [replyFocusRequest, setReplyFocusRequest] = useState(0),
    [followRequest, setFollowRequest] = useState(0),
    [newFile, setNewFile] = useState(false),
    [editing, setEditing] = useState(false),
    [exporting, setExporting] = useState(false),
    [review, setReview] = useState<ReviewSelection>(null),
    [remote, setRemote] = useState<{
      project: Project;
      files: Record<string, string>;
    } | null>(null);
  const buildFlush = useRef<() => Promise<void>>(async () => {});
  const tracking = useProjectOperations(pid);
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
  const currentScope = useRef(pid),
    proposalAction = useRef(false),
    mounted = useRef(true);
  currentScope.current = pid;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const latestBuild = tracking.items.find((o) => o.type === "generation");
  useEffect(() => {
    setReplyText("");
    setReplyFocusRequest(0);
    setReplyTo(
      latestBuild?.state === "succeeded" &&
        latestBuild.result_ref.kind === "clarification"
        ? latestBuild.id
        : null,
    );
  }, [pid, latestBuild?.id, latestBuild?.state, latestBuild?.result_ref.kind]);
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
    document.getElementById("build-idea")?.focus();
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
        .then(() => buildFlush.current())
        .then(() => navigate(anchor.pathname + anchor.search))
        .catch(setError);
    };
    document.addEventListener("click", guard, true);
    return () => document.removeEventListener("click", guard, true);
  }, [draft.flush, navigate]);
  async function act(fn: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(e);
      if (e instanceof ApiError && e.code === "VERSION_CONFLICT" && !review) {
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
    return result.revision.id;
  }
  async function readProposal(oid: string) {
    const result = await api<Proposal>(`/projects/${pid}/proposals/${oid}`, {
      signal: AbortSignal.timeout(15_000),
    });
    if (result.proposal_id !== oid)
      throw new Error("A resposta não corresponde à proposta solicitada.");
    return result;
  }
  async function inspect(op: Operation) {
    await act(async () => {
      const proposal = await readProposal(op.id);
      if (!mounted.current || currentScope.current !== pid) return;
      setReview({ op, proposal });
      setMobile("code");
    });
  }
  async function refreshReview() {
    if (!review || proposalAction.current) return;
    await act(async () => {
      const proposal = await readProposal(review.op.id);
      if (!mounted.current || currentScope.current !== pid) return;
      setReview({ op: review.op, proposal });
      if (proposal.state === "applied") await syncApplied(proposal);
      tracking.refresh();
    });
  }
  async function compareCurrent() {
    if (!review || proposalAction.current) return;
    await act(async () => {
      const current = await api<Project>(`/projects/${pid}`, {
        signal: AbortSignal.timeout(15_000),
      });
      const incoming = await revisionFiles(pid, current.current_revision_id);
      if (!mounted.current || currentScope.current !== pid) return;
      setReview(null);
      setRemote({ project: current, files: incoming });
    });
  }
  async function syncApplied(proposal: Proposal) {
    if (!proposal.applied_revision_id || proposal.state !== "applied")
      throw new Error(
        "A aplicação ainda não foi confirmada pelo servidor. Consulte o estado antes de repetir.",
      );
    const p = await api<Project>(`/projects/${pid}`, {
      signal: AbortSignal.timeout(15_000),
    });
    const incoming = await revisionFiles(pid, p.current_revision_id);
    await draft.reload();
    if (!mounted.current || currentScope.current !== pid) return;
    setProject(p);
    setBaseFiles(incoming);
    setSelected((path) =>
      path in incoming ? path : (Object.keys(incoming)[0] ?? ""),
    );
    syncedOperations.current.add(proposal.proposal_id);
    tracking.refresh();
    setReview(null);
    setMessage(
      p.current_revision_id === proposal.applied_revision_id
        ? "Alterações aplicadas à revisão salva."
        : "Aplicação confirmada. A revisão mais recente do projeto foi carregada.",
    );
  }
  async function proposalCommand<T>(
    path: string,
    body: unknown,
    version?: number,
  ): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        command<T>(path, body, "POST", version),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(
                  "A confirmação demorou. Consultando o resultado antes de permitir repetir.",
                ),
              ),
            20_000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }
  async function accept() {
    if (!review || !project || dirty || busy || proposalAction.current) return;
    const selectedReview = review;
    proposalAction.current = true;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      await draft.flush();
      const latest = await readProposal(selectedReview.op.id);
      if (!mounted.current || currentScope.current !== pid) return;
      if (latest.proposal_version !== selectedReview.proposal.proposal_version)
        throw new Error(
          "A versão da proposta mudou. Consulte e revise a versão atual antes de aplicar.",
        );
      if (latest.state === "applied") {
        await syncApplied(latest);
        return;
      }
      if (latest.state !== "generated") {
        setReview({ ...selectedReview, proposal: latest });
        throw new Error(
          latest.state === "conflict"
            ? "O projeto ou rascunho mudou. A proposta e suas edições foram preservadas."
            : "Esta proposta já não está disponível para aplicação.",
        );
      }
      await proposalCommand<{ revision_id: string }>(
        `/projects/${pid}/proposals/${selectedReview.op.id}/apply`,
        {
          expected_proposal_version: selectedReview.proposal.proposal_version,
          expected_project_revision:
            selectedReview.proposal.base_project_revision,
        },
        selectedReview.proposal.expected_project_version,
      );
      const confirmed = await readProposal(selectedReview.op.id);
      if (!mounted.current || currentScope.current !== pid) return;
      setReview({ ...selectedReview, proposal: confirmed });
      await syncApplied(confirmed);
    } catch (failure) {
      if (!mounted.current || currentScope.current !== pid) return;
      try {
        const confirmed = await readProposal(selectedReview.op.id);
        if (!mounted.current || currentScope.current !== pid) return;
        if (
          confirmed.proposal_version ===
          selectedReview.proposal.proposal_version
        ) {
          setReview({ ...selectedReview, proposal: confirmed });
          if (confirmed.state === "applied") {
            await syncApplied(confirmed);
            return;
          }
        }
      } catch {}
      if (mounted.current && currentScope.current === pid)
        setError(
          failure instanceof TypeError
            ? new Error(
                "A conexão foi interrompida. A proposta foi preservada; consulte o estado antes de repetir.",
              )
            : failure,
        );
    } finally {
      proposalAction.current = false;
      if (mounted.current && currentScope.current === pid) setBusy(false);
    }
  }
  async function discard() {
    if (!review || busy || proposalAction.current) return;
    const selectedReview = review;
    proposalAction.current = true;
    setBusy(true);
    setError(null);
    try {
      await proposalCommand(
        `/projects/${pid}/proposals/${selectedReview.op.id}/discard`,
        {},
      );
      const confirmed = await readProposal(selectedReview.op.id);
      if (confirmed.state !== "discarded")
        throw new Error(
          "O descarte ainda não foi confirmado. Consulte o estado da proposta.",
        );
      if (!mounted.current || currentScope.current !== pid) return;
      setReview(null);
      tracking.refresh();
      setMessage("Proposta descartada. A revisão atual foi preservada.");
    } catch (failure) {
      if (!mounted.current || currentScope.current !== pid) return;
      try {
        const confirmed = await readProposal(selectedReview.op.id);
        if (confirmed.state === "discarded") {
          if (mounted.current && currentScope.current === pid) {
            setReview(null);
            tracking.refresh();
            setMessage("Proposta descartada. A revisão atual foi preservada.");
          }
          return;
        }
      } catch {}
      if (mounted.current && currentScope.current === pid) setError(failure);
    } finally {
      proposalAction.current = false;
      if (mounted.current && currentScope.current === pid) setBusy(false);
    }
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
          <PublicationControl
            pid={pid}
            revision={project.current_revision_id}
            dirty={dirty}
            busy={busy}
            operations={tracking.items}
            beforeNavigate={async () => {
              await draft.flush();
              await buildFlush.current();
            }}
          />
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
          ["chat", "Construção"],
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
            <span className="small muted">Pedido do projeto</span>
          </div>
          {!!tracking.error && (
            <div className="notice" role="alert">
              A conexão foi interrompida. O último estado foi preservado.
              <button className="button" onClick={tracking.refresh}>
                Reconectar
              </button>
            </div>
          )}
          <GuidedBuildPanel
            key={pid}
            onFlushReady={(fn) => {
              buildFlush.current = fn;
            }}
            pid={pid}
            caps={caps}
            canWrite={canWrite}
            blocked={blocked || tracking.loading || !!tracking.error}
            active={!!active}
            hasHistory={tracking.items.some((o) => o.type === "generation")}
            pendingProposal={tracking.items.some(
              (o) =>
                o.type === "generation" &&
                o.state === "succeeded" &&
                o.result_ref.candidate &&
                !o.result_ref.applied_revision_id &&
                !o.result_ref.discarded_at,
            )}
            reuseRequest={reuseRequest}
            onReused={() => setReuseRequest(null)}
            replyTo={replyTo}
            replyText={replyText}
            replyFocusRequest={replyFocusRequest}
            onNewRequest={() => setReplyTo(null)}
            contextRevision={project.current_revision_id}
            contextDirty={dirty}
            onSaveContext={async () => {
              await save();
            }}
            onRefreshContext={async () => {
              await draft.flush();
              const current = await api<Project>(`/projects/${pid}`);
              const incoming = await revisionFiles(
                pid,
                current.current_revision_id,
              );
              if (!mounted.current || currentScope.current !== pid) return;
              if (
                dirty &&
                current.current_revision_id !== draft.data!.base_revision_id
              ) {
                setRemote({ project: current, files: incoming });
                throw new Error(
                  "Há alterações locais e uma nova revisão salva. Compare as versões do projeto antes de atualizar a preparação.",
                );
              }
              await draft.reload();
              if (!mounted.current || currentScope.current !== pid) return;
              setProject(current);
              setBaseFiles(incoming);
              setSelected((path) =>
                path in incoming ? path : (Object.keys(incoming)[0] ?? ""),
              );
            }}
            beforeSend={async () => {
              setBusy(true);
              try {
                await draft.flush();
                return project.current_revision_id;
              } finally {
                setBusy(false);
              }
            }}
            onAccepted={(op) => {
              setReplyTo(null);
              setReplyText("");
              setReplyFocusRequest(0);
              setFollowRequest((request) => request + 1);
              tracking.accept(op);
              tracking.refresh();
            }}
          >
            <BuildConversation
              operations={tracking.items}
              pid={pid}
              busy={busy}
              syncedAt={tracking.syncedAt}
              loadedRevision={project.current_revision_id}
              followRequest={followRequest}
              onCancel={(op) =>
                void act(async () => {
                  tracking.accept(
                    await command<Operation>(`/operations/${op.id}/cancel`),
                  );
                  tracking.refresh();
                })
              }
              onRetry={(request) => {
                setReuseRequest(request);
              }}
              onFile={(path) => {
                setSelected(path);
                setTab("code");
                setMobile("code");
              }}
              onReview={inspect}
              onDiscard={(op) =>
                void act(async () => {
                  await command(`/projects/${pid}/proposals/${op.id}/discard`);
                  tracking.refresh();
                })
              }
              onAnswer={(op) => {
                setReplyTo(op.reply_to || op.id);
                setReplyText(op.reply_to ? op.request_text || "" : "");
                setReplyFocusRequest((request) => request + 1);
                setMobile("chat");
              }}
            />
          </GuidedBuildPanel>
          {tracking.cursor && (
            <button
              className="text-button"
              disabled={tracking.loadingMore}
              onClick={tracking.loadMore}
            >
              Conversas anteriores
            </button>
          )}
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
      <WorkspaceDialogs
        {...{
          review,
          setReview,
          remote,
          setRemote,
          exporting,
          setExporting,
          newFile,
          setNewFile,
          editing,
          setEditing,
          baseFiles,
          setBaseFiles,
          files,
          error,
          setError,
          blocked,
          dirty,
          busy,
          accept,
          discard,
          refreshReview,
          compareCurrent,
          project,
          setProject,
          draft,
          caps,
          act,
          save,
          setMessage,
          exportProject,
          setSelected,
          pid,
        }}
      />
    </div>
  );
}
