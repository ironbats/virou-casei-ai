import { MigrationFailureNotice } from "./MigrationFailureNotice";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  BookOpen,
  Code2,
  Download,
  Eye,
  History,
  MessageSquare,
  Save,
  ScanSearch,
  Pencil,
} from "lucide-react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { WorkspaceTopbar } from "../app/WorkspaceChrome";
import { WorkspaceMenu } from "./WorkspaceMenu";
import { useWorkbenchSplit } from "./useWorkbenchSplit";
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
import { CodeAnalysisContent } from "./CodeAnalysisPanel";
import { useCodeAnalysis } from "./useCodeAnalysis";
import { requestCodeCorrection } from "./codeCorrection";
import { protectedMigration } from "./migrationFiles";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedProposal = searchParams.get("proposal");
  const requestedConversation = searchParams.get("conversation");
  const showAnalysis = searchParams.get("analysis") === "1";
  const openedProposal = useRef<string | null>(null);
  const openedConversation = useRef<string | null>(null);
  const openedAnalysis = useRef(false);
  const conversationPanel = useRef<HTMLElement>(null);
  const split = useWorkbenchSplit();
  const focusedPanel = useRef<"code" | "chat" | null>(null);
  const [previewIssues, setPreviewIssues] = useState<string[]>([]);
  const [newRequestRequest, setNewRequestRequest] = useState(0);
  const draft = useDraft<ProjectDraft>(
    `/projects/${pid}/draft`,
    draftPayload,
    mergeProjectDraft,
  );
  const [project, setProject] = useState<Project | null>(null),
    [caps, setCaps] = useState<Capabilities | null>(null),
    [baseFiles, setBaseFiles] = useState<Record<string, string> | null>(null),
    [sourceError, setSourceError] = useState<unknown>(null),
    [sourceRetry, setSourceRetry] = useState(0),
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
    [conversationRequest, setConversationRequest] = useState(0),
    [conversationLookup, setConversationLookup] = useState(0),
    [openingConversation, setOpeningConversation] = useState(false),
    [conversationError, setConversationError] = useState<unknown>(null),
    [newFile, setNewFile] = useState<false | "file" | "migration">(false),
    [editing, setEditing] = useState(false),
    [exporting, setExporting] = useState(false),
    [requestedExport, setRequestedExport] = useState<{
      operation: Operation;
      revisionId: string | null;
    } | null>(null),
    [review, setReview] = useState<ReviewSelection>(null),
    [remote, setRemote] = useState<{
      project: Project;
      files: Record<string, string>;
    } | null>(null);
  useEffect(() => {
    const update = (event: FocusEvent) => {
      const element = event.target as Element;
      focusedPanel.current = element.closest(".conversation-panel")
        ? "chat"
        : element.closest(".ide-panel")
          ? "code"
          : null;
    };
    document.addEventListener("focusin", update);
    return () => document.removeEventListener("focusin", update);
  }, []);
  useLayoutEffect(() => {
    if (split.narrow && focusedPanel.current) setMobile(focusedPanel.current);
  }, [split.narrow]);
  const buildFlush = useRef<() => Promise<void>>(async () => {});
  const correctionRetry = useRef(false);
  const tracking = useProjectOperations(pid);
  const codeAnalysis = useCodeAnalysis({
    projectId: pid,
    revisionId: project?.current_revision_id ?? null,
    source: previewIssues.length ? "preview" : "editor",
    runtimeError: previewIssues.join("\n"),
  });
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
  const pendingProposal = tracking.items.some(
    (operation) =>
      operation.type === "generation" &&
      operation.state === "succeeded" &&
      operation.result_ref.candidate &&
      !operation.result_ref.applied_revision_id &&
      !operation.result_ref.discarded_at,
  );
  const conversationOperations = tracking.items.map((operation) => {
    if (operation.type !== "generation" || operation.analysis_id)
      return operation;
    const analysis = [codeAnalysis.operation, ...tracking.items].find(
      (item) =>
        item?.type === "code_analysis" &&
        item.result_ref.correction_operation_id === operation.id,
    );
    return analysis
      ? {
          ...operation,
          analysis_id: analysis.id,
          analysis_revision_id: analysis.result_ref.revision_id,
        }
      : operation;
  });
  const syncedOperations = useRef(new Set<string>());
  const syncFailedAt = useRef(0);
  const currentScope = useRef(pid),
    proposalAction = useRef(false),
    mounted = useRef(true);
  currentScope.current = pid;
  function openConversation(operation: Operation) {
    tracking.accept(operation);
    tracking.refresh();
    openedConversation.current = operation.id;
    openedAnalysis.current = false;
    setConversationError(null);
    setOpeningConversation(false);
    setSearchParams((previous) => {
      const next = new URLSearchParams(previous);
      next.delete("analysis");
      next.delete("proposal");
      next.set("conversation", operation.id);
      return next;
    });
    setMobile("chat");
    setFollowRequest((request) => request + 1);
    setConversationRequest((request) => request + 1);
  }
  function focusConversation() {
    const viewport = conversationPanel.current?.querySelector<HTMLElement>(
      ".build-conversation",
    );
    if (!viewport) return;
    viewport.focus({ preventScroll: true });
    const target = requestedConversation
      ? viewport.querySelector<HTMLElement>(
          `[data-operation-id="${CSS.escape(requestedConversation)}"]`,
        )
      : viewport.querySelector<HTMLElement>(".code-analysis");
    if (target)
      viewport.scrollTop +=
        target.getBoundingClientRect().top -
        viewport.getBoundingClientRect().top;
    conversationPanel.current?.scrollIntoView({ block: "nearest" });
  }
  useEffect(() => {
    if (!requestedConversation) {
      openedConversation.current = null;
      setConversationError(null);
      setOpeningConversation(false);
      return;
    }
    if (
      openedConversation.current === requestedConversation ||
      !project ||
      !caps ||
      !draft.data ||
      !baseFiles
    )
      return;
    setConversationError(null);
    setOpeningConversation(true);
    const controller = new AbortController();
    void api<Operation>(
      `/operations/${encodeURIComponent(requestedConversation)}`,
      {
        signal: AbortSignal.any([
          controller.signal,
          AbortSignal.timeout(12000),
        ]),
      },
    )
      .then((operation) => {
        if (
          operation.id !== requestedConversation ||
          operation.project_id !== pid ||
          operation.type !== "generation"
        )
          throw new Error("A conversa solicitada não pertence a este projeto.");
        if (controller.signal.aborted || currentScope.current !== pid) return;
        openedConversation.current = requestedConversation;
        tracking.accept(operation);
        setMobile("chat");
        setFollowRequest((request) => request + 1);
        setConversationRequest((request) => request + 1);
      })
      .catch((failure) => {
        if (!controller.signal.aborted && currentScope.current === pid)
          setConversationError(failure);
      })
      .finally(() => {
        if (!controller.signal.aborted && currentScope.current === pid)
          setOpeningConversation(false);
      });
    return () => controller.abort();
  }, [
    requestedConversation,
    conversationLookup,
    pid,
    !!project,
    !!caps,
    !!draft.data,
    !!baseFiles,
  ]);
  useEffect(() => {
    if (!showAnalysis) {
      openedAnalysis.current = false;
      return;
    }
    if (
      openedAnalysis.current ||
      !project ||
      !caps ||
      !draft.data ||
      !baseFiles
    )
      return;
    openedAnalysis.current = true;
    setMobile("chat");
    setFollowRequest((request) => request + 1);
    setConversationRequest((request) => request + 1);
  }, [showAnalysis, !!project, !!caps, !!draft.data, !!baseFiles]);
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
    setSourceError(null);
    setBaseFiles(null);
    revisionFiles(pid, draft.data.base_revision_id)
      .then((files) => {
        if (alive) {
          setBaseFiles(files);
          setSourceError(null);
          setSelected((current) => current in files ? current : (Object.keys(files)[0] ?? ""));
        }
      })
      .catch((failure) => {
        if (alive) setSourceError(failure);
      });
    return () => {
      alive = false;
    };
  }, [pid, draft.data?.base_revision_id, sourceRetry]);
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
  useEffect(() => {
    if (
      !requestedProposal ||
      openedProposal.current === requestedProposal ||
      !project ||
      !draft.data ||
      !caps ||
      !baseFiles ||
      busy
    )
      return;
    openedProposal.current = requestedProposal;
    void act(async () => {
      const op = await api<Operation>(
        `/operations/${encodeURIComponent(requestedProposal)}`,
        {
          signal: AbortSignal.timeout(12000),
        },
      );
      if (
        op.project_id !== pid ||
        op.type !== "generation" ||
        op.state !== "succeeded" ||
        op.result_ref.kind !== "implementation"
      )
        throw new Error(
          "A proposta de correção não está disponível para este projeto.",
        );
      const proposal = await readProposal(op.id);
      if (!mounted.current || currentScope.current !== pid) return;
      tracking.accept(op);
      setReview({ op, proposal });
      setMobile("code");
    });
  }, [
    requestedProposal,
    pid,
    !!project,
    !!draft.data,
    !!caps,
    !!baseFiles,
    busy,
  ]);
  useEffect(() => setPreviewIssues([]), [project?.current_revision_id]);
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
  async function retryCorrection(operation: Operation) {
    if (correctionRetry.current || busy) return;
    correctionRetry.current = true;
    try {
      await act(async () => {
        if (
          !operation.analysis_id ||
          !project?.current_revision_id ||
          !canWrite ||
          dirty ||
          pendingProposal
        )
          throw new Error(
            "Confira a revisão salva e o diagnóstico antes de solicitar outra correção.",
          );
        await draft.flush();
        await buildFlush.current();
        const accepted = await requestCodeCorrection({
          projectId: pid,
          analysisId: operation.analysis_id,
          revisionId: project.current_revision_id,
          previousId: operation.id,
          onRecovering: () => {
            if (mounted.current && currentScope.current === pid)
              setMessage(
                "Conferindo se a correção foi recebida. O acompanhamento será retomado nesta conversa.",
              );
          },
        });
        if (!mounted.current || currentScope.current !== pid) return;
        setMessage("");
        openConversation(accepted);
      });
    } finally {
      correctionRetry.current = false;
    }
  }
  async function exportProject() {
    if (!project) return;
    await act(async () => {
      const projectId = pid;
      setRequestedExport(null);
      try {
        await draft.flush();
        const current = await api<Project>(`/projects/${projectId}`);
        const operation = await command<Operation>(
          `/projects/${projectId}/exports`,
          {
            revision_id: current.current_revision_id,
          },
        );
        if (!mounted.current || currentScope.current !== projectId) return;
        setRequestedExport({
          operation,
          revisionId: current.current_revision_id,
        });
        tracking.accept(operation);
        tracking.refresh();
      } finally {
        if (mounted.current && currentScope.current === projectId)
          setExporting(false);
      }
    });
  }
  const exportOperation =
    requestedExport?.operation.project_id === pid
      ? tracking.items.find(
          (operation) =>
            operation.id === requestedExport.operation.id &&
            operation.project_id === pid &&
            operation.type === "export",
        ) || requestedExport.operation
      : null;
  const exportRevision =
    exportOperation?.result_ref.revision_id || requestedExport?.revisionId;
  const downloads = tracking.items.filter(
    (o) => o.type === "export" && o.state === "succeeded",
  );
  if (!project || !caps || !draft.data)
    return (
      <>
        <ErrorNotice error={error || draft.error} />
        <MigrationFailureNotice error={error || draft.error} />
        {!error && !draft.error && <Loading />}
      </>
    );
  if (!baseFiles)
    return (
      <div className="project-workspace">
        <WorkspaceTopbar>
          <div className="workspace-heading">
            <div className="workspace-identity">
              <Link className="back-link" to="/app/projetos">Meus projetos</Link>
              <span className="muted"> / </span>
              <h1 title={project.name}>{project.name}</h1>
            </div>
          </div>
        </WorkspaceTopbar>
        <main className="workspace-source-fallback">
          {sourceError ? (
            <section className="workspace-source-fallback-card" role="status">
              <h2>Projeto disponível, mas os arquivos da revisão não carregaram</h2>
              <p>O cadastro e o histórico do projeto continuam disponíveis. A edição e a publicação estão bloqueadas até que os arquivos sejam recuperados com integridade.</p>
              <ErrorNotice error={sourceError} />
              {sourceError instanceof ApiError && sourceError.requestId && (
                <p className="small muted">Identificação do erro: {sourceError.requestId}</p>
              )}
              <div className="actions">
                <button className="button primary" onClick={() => setSourceRetry((attempt) => attempt + 1)}>Tentar carregar novamente</button>
                <Link className="button" to={`/app/projetos/${pid}/historico`}>Histórico de revisões</Link>
                <Link className="button" to="/app/projetos">Voltar aos projetos</Link>
              </div>
            </section>
          ) : <Loading />}
        </main>
      </div>
    );
  const blocked = busy || !canWrite || !!draft.conflict;
  const analysisBlocked =
    blocked ||
    !!active ||
    project.current_revision_id !== draft.data.base_revision_id;
  const analysisReason = !canWrite
    ? "Seu acesso permite consultar o diagnóstico. Solicitar análise exige permissão de edição."
    : !project.current_revision_id
      ? "Salve uma revisão do projeto antes de pedir a análise."
      : dirty
        ? "Salve as alterações em uma revisão antes de pedir análise ou correção."
        : analysisBlocked
          ? "Aguarde a operação atual ou resolva o conflito do rascunho para continuar."
          : "O diagnóstico usa a revisão salva e aparece na conversa do projeto.";
  const analysisAction = (
    <button
      className="button workspace-analysis-button"
      title={analysisReason}
      aria-label={
        codeAnalysis.operation ? "Ver análise IA no chat" : "Pedir Análise IA"
      }
      aria-describedby="code-analysis-entry-reason"
      disabled={
        codeAnalysis.loading ||
        (!codeAnalysis.operation &&
          (analysisBlocked ||
            dirty ||
            !project.current_revision_id ||
            !!codeAnalysis.sending ||
            codeAnalysis.recovering))
      }
      onClick={() => {
        openedAnalysis.current = true;
        setSearchParams((previous) => {
          const next = new URLSearchParams(previous);
          next.delete("conversation");
          next.delete("proposal");
          next.set("analysis", "1");
          return next;
        });
        setMobile("chat");
        setFollowRequest((request) => request + 1);
        setConversationRequest((request) => request + 1);
        if (!codeAnalysis.operation) void codeAnalysis.analyze();
      }}
    >
      <ScanSearch size={16} />
      <span>Análise IA</span>
    </button>
  );
  return (
    <div className="project-workspace">
      <WorkspaceTopbar>
        <div className="workspace-heading">
          <div className="workspace-identity">
            <Link className="back-link" to="/app/projetos">
              Meus projetos
            </Link>
            <span className="muted"> / </span>
            <h1 tabIndex={0} title={project.name}>
              {project.name}
            </h1>
            <p
              className="small muted workspace-draft-status"
              role="status"
              title={draft.status}
            >
              {dirty ? "Rascunho alterado" : draft.status}
            </p>
          </div>
          <div className="actions">
            <button
              className="button"
              aria-label="Baixar projeto"
              title="Baixar projeto"
              disabled={!canExport || busy}
              onClick={() => setExporting(true)}
            >
              <Download size={16} />
              <span className="workspace-action-label">Baixar projeto</span>
            </button>
            <PublicationControl
              compact
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
            <WorkspaceMenu label="Mais ações do projeto">
              <button
                className="button workspace-menu-download"
                disabled={!canExport || busy}
                onClick={() => setExporting(true)}
              >
                <Download size={16} />
                Baixar projeto
              </button>
              <button
                className="button"
                onClick={() => setEditing(true)}
                disabled={!canWrite}
              >
                <Pencil size={16} />
                Editar projeto
              </button>
              <Link className="button" to={`/app/projetos/${pid}/historico`}>
                <History size={16} />
                Histórico de revisões
              </Link>
              <Link
                className="button"
                to={`/app/aprendizado/sdd?project=${pid}`}
              >
                <BookOpen size={16} />
                Explorar guias
              </Link>
              {downloads.map((op) => (
                <a
                  className="button"
                  key={op.id}
                  href={`/api/v1/exports/${op.id}/download`}
                >
                  <Download size={16} />
                  Baixar ZIP · revisão {op.result_ref.revision_id?.slice(0, 8)}
                </a>
              ))}
              <p className="small muted">
                {draft.status}
                {dirty
                  ? " · Alterações ainda não aplicadas à revisão"
                  : " · Revisão salva"}
              </p>
            </WorkspaceMenu>
          </div>
        </div>
      </WorkspaceTopbar>
      <div className="workspace-alerts">
        <ErrorNotice error={error || draft.error} />
        <MigrationFailureNotice error={error || draft.error} />
        <ErrorNotice error={conversationError} />
        {openingConversation && (
          <p className="small" role="status">
            Abrindo a conversa…
          </p>
        )}
        {!!conversationError && requestedConversation && (
          <button
            className="button"
            onClick={() => setConversationLookup((attempt) => attempt + 1)}
          >
            Tentar abrir conversa novamente
          </button>
        )}
        {message && (
          <p className="notice" role="status">
            {message}
          </p>
        )}
        {exportOperation && (
          <div
            className={
              "notice workspace-export-status" +
              (exportOperation.state === "failed" ? " error" : "")
            }
            role={exportOperation.state === "failed" ? "alert" : "status"}
            data-operation-id={exportOperation.id}
          >
            <div>
              <strong>
                {exportOperation.state === "succeeded"
                  ? "Download pronto."
                  : exportOperation.state === "failed"
                    ? "Não foi possível preparar o download."
                    : exportOperation.state === "cancelled"
                      ? "A preparação do download foi cancelada."
                      : exportOperation.state === "reconciling"
                        ? "O download está em apuração. Aguarde a confirmação."
                        : "Preparando download da revisão salva."}
              </strong>
              {exportRevision && (
                <span className="small muted">
                  Revisão {exportRevision.slice(0, 8)}
                </span>
              )}
              {exportOperation.state === "failed" && (
                <p>
                  {exportOperation.progress_message ||
                    "Tente novamente pelo botão Baixar projeto."}
                </p>
              )}
              {!!tracking.error && inProgress(exportOperation) && (
                <p>
                  Não foi possível atualizar o download. A solicitação foi
                  preservada.{" "}
                  <button className="text-button" onClick={tracking.refresh}>
                    Atualizar estado do download
                  </button>
                </p>
              )}
            </div>
            {exportOperation.state === "succeeded" && (
              <a
                className="button"
                href={`/api/v1/exports/${encodeURIComponent(exportOperation.id)}/download`}
              >
                <Download size={16} aria-hidden="true" />
                Baixar ZIP · revisão {exportRevision?.slice(0, 8)}
              </a>
            )}
          </div>
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
      </div>
      <div
        className="mobile-workspace-tabs"
        hidden={!split.narrow}
        role="tablist"
        aria-label="Área do projeto"
      >
        {[
          ["code", "Código"],
          ["chat", "Chat"],
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
      <div
        ref={split.element}
        style={split.style}
        className={
          "ide-layout mobile-" +
          mobile +
          (split.narrow ? " is-narrow" : "") +
          (split.dragging ? " is-resizing" : "")
        }
      >
        <section
          className="panel ide-panel"
          aria-label="Código e prévia do projeto"
          hidden={split.narrow && mobile === "chat"}
        >
          <ProjectEditor
            mode={tab}
            menuActions={analysisAction}
            toolbar={
              <>
                <div
                  className="filter-tabs"
                  role="tablist"
                  aria-label="Visualização do projeto"
                >
                  <button
                    role="tab"
                    aria-label="Código"
                    title="Código"
                    aria-selected={tab === "code"}
                    className={tab === "code" ? "selected" : ""}
                    onClick={() => setTab("code")}
                  >
                    <Code2 size={16} />
                    <span>Código</span>
                  </button>
                  <button
                    role="tab"
                    aria-label="Prévia"
                    title="Prévia"
                    aria-selected={tab === "preview"}
                    className={tab === "preview" ? "selected" : ""}
                    onClick={() => setTab("preview")}
                  >
                    <Eye size={16} />
                    <span>Prévia</span>
                  </button>
                </div>
                {analysisAction}
                <button
                  className="button workspace-save-button"
                  title="Salvar revisão"
                  aria-label="Salvar revisão"
                  disabled={blocked || !dirty}
                  onClick={() => void act(save)}
                >
                  <Save size={16} />
                  <span>Salvar revisão</span>
                </button>
              </>
            }
            preview={
              tab === "preview" ? (
                caps.visual_preview?.enabled ? (
                  <ProjectPreview
                    files={files}
                    dirty={dirty}
                    onIssues={setPreviewIssues}
                  />
                ) : (
                  <p className="notice">
                    A prévia visual precisa da versão atualizada do backend.
                    Recarregue após a atualização.
                  </p>
                )
              ) : null
            }
            files={files}
            base={baseFiles}
            selected={selected}
            onSelect={(path) => {
              setSelected(path);
              setTab("code");
            }}
            readOnly={blocked || !!active}
            onChange={(path, content) => {
              if (protectedMigration(path, baseFiles)) return;
              draft.setData((d) => ({
                ...d,
                files: { ...(d.files ?? baseFiles), [path]: content },
              }));
            }}
            onAdd={() => setNewFile("file")}
            onAddMigration={() => setNewFile("migration")}
            onRestoreMigration={() => {
              if (
                !(selected in baseFiles) ||
                !protectedMigration(selected, baseFiles)
              )
                return;
              draft.setData((d) => ({
                ...d,
                files: {
                  ...(d.files ?? baseFiles),
                  [selected]: baseFiles[selected],
                },
              }));
            }}
            onRemove={() => {
              if (protectedMigration(selected, baseFiles)) return;
              if (confirm("Remover " + selected + " do rascunho?")) {
                const next = { ...files };
                delete next[selected];
                draft.setData((d) => ({ ...d, files: next }));
                setSelected(Object.keys(next)[0] ?? "");
              }
            }}
          />
          <div className="workspace-ide-status">
            <span>
              {selected.split(".").at(-1)?.toUpperCase() || "Texto"} · UTF-8
            </span>
            <span title={project.current_revision_id || "Sem revisão salva"}>
              {dirty
                ? "Rascunho alterado"
                : project.current_revision_id
                  ? "Revisão salva"
                  : "Sem revisão salva"}
            </span>
          </div>
          <p
            id="code-analysis-entry-reason"
            className={
              analysisBlocked || dirty || !project.current_revision_id
                ? "workspace-analysis-reason small"
                : "sr-only"
            }
          >
            {analysisReason}
          </p>
        </section>
        <div
          className="workbench-separator"
          hidden={split.narrow}
          {...split.separator}
        >
          <span />
        </div>
        <section
          className="panel conversation-panel"
          ref={conversationPanel}
          aria-label="Construir com IA"
          hidden={split.narrow && mobile !== "chat"}
        >
          <div className="card-title">
            <h2>
              <MessageSquare size={19} />
              Construir com IA
            </h2>
            <WorkspaceMenu
              label="Histórico e ações da conversa"
              icon={<History size={19} aria-hidden="true" />}
            >
              <button
                className="button"
                onClick={() => {
                  setMobile("chat");
                  setConversationRequest((request) => request + 1);
                }}
              >
                Histórico de construções e propostas
              </button>
              {tracking.cursor && (
                <button
                  className="button"
                  disabled={tracking.loadingMore}
                  onClick={tracking.loadMore}
                >
                  Conversas anteriores
                </button>
              )}
              <button
                className="button"
                disabled={blocked || tracking.loading || !!tracking.error}
                onClick={() => setNewRequestRequest((request) => request + 1)}
              >
                Pedido do projeto
              </button>
            </WorkspaceMenu>
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
            conversationRequest={conversationRequest}
            newRequestRequest={newRequestRequest}
            onConversationReady={focusConversation}
            hasHistory={tracking.items.some((o) => o.type === "generation")}
            pendingProposal={pendingProposal}
            reuseRequest={reuseRequest}
            onReused={() => setReuseRequest(null)}
            replyTo={replyTo}
            replyText={replyText}
            replyFocusRequest={replyFocusRequest}
            onNewRequest={() => {
              setReplyTo(null);
              if (requestedConversation || showAnalysis)
                setSearchParams(
                  (previous) => {
                    const next = new URLSearchParams(previous);
                    next.delete("conversation");
                    next.delete("analysis");
                    return next;
                  },
                  { replace: true },
                );
            }}
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
              operations={conversationOperations}
              pid={pid}
              busy={busy}
              syncedAt={tracking.syncedAt}
              loadedRevision={project.current_revision_id}
              followRequest={followRequest}
              analysisContent={
                showAnalysis ? (
                  <CodeAnalysisContent
                    projectId={pid}
                    revisionId={project.current_revision_id}
                    source={previewIssues.length ? "preview" : "editor"}
                    analysis={codeAnalysis}
                    dirty={dirty}
                    blocked={analysisBlocked || pendingProposal}
                    onCorrectionAccepted={openConversation}
                  />
                ) : undefined
              }
              correctionBlockedReason={
                !canWrite
                  ? "Seu acesso não permite solicitar uma correção."
                  : dirty
                    ? "Salve as alterações da revisão antes de solicitar outra correção."
                    : blocked ||
                        pendingProposal ||
                        project.current_revision_id !==
                          draft.data.base_revision_id
                      ? "Conclua a operação atual ou resolva o conflito do rascunho antes de tentar novamente."
                      : ""
              }
              onRetryCorrection={(operation) => void retryCorrection(operation)}
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
        </section>
      </div>
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
