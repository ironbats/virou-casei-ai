import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  Circle,
  CircleCheck,
  Clock3,
  Copy,
  ExternalLink,
  Globe,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useSession } from "../app/session";
import { api, ApiError, command, date } from "../shared/api";
import type { Operation, Project } from "../shared/types";
import { ErrorNotice, Loading, useLoad } from "../shared/ui";
import { inProgress } from "./useProjectOperations";
import { PublicationBuildDiagnostics } from "./PublicationBuildDiagnostics";
import {
  publicationUrl,
  usePublicationOperation,
  usePublicationReadiness,
} from "./usePublication";
import "./publication.css";

const steps = [
  {
    title: "Solicitação recebida",
    description: "A publicação entra na fila de execução.",
    stages: ["queued"],
  },
  {
    title: "Preparar aplicação",
    description: "Os arquivos da revisão são preparados para publicação.",
    stages: ["publication_building"],
  },
  {
    title: "Preparar recursos",
    description: "A hospedagem e o banco do projeto são conferidos.",
    stages: ["publication_provisioning", "publication_migrating"],
  },
  {
    title: "Enviar à hospedagem",
    description: "A aplicação é enviada e seu endereço é configurado.",
    stages: ["publication_uploading", "publication_routing"],
  },
  {
    title: "Verificar publicação",
    description:
      "Frontend, API e banco são verificados antes da liberação do link.",
    stages: ["publication_verifying", "publication_verified"],
  },
];

function PublicationSteps({ operation }: { operation: Operation | null }) {
  const activity = operation?.activity ?? [];
  const observed = new Set(activity.map((event) => event.stage));
  if (operation) observed.add(operation.progress_stage);
  const current = steps.findIndex((step) =>
    step.stages.includes(operation?.progress_stage ?? ""),
  );
  const lastObserved = steps.reduce(
    (latest, step, index) =>
      step.stages.some((stage) => observed.has(stage)) ? index : latest,
    -1,
  );
  const activeIndex = current >= 0 ? current : lastObserved;
  const terminal =
    operation && ["succeeded", "failed", "cancelled"].includes(operation.state);
  return (
    <ol className="publication-steps" aria-label="Etapas da publicação">
      {steps.map((step, index) => {
        const seen = step.stages.some((stage) => observed.has(stage));
        const completed =
          seen && (operation?.state === "succeeded" || index < activeIndex);
        const active = !!operation && index === activeIndex && !completed;
        const interrupted =
          active &&
          (operation.state === "failed" || operation.state === "cancelled");
        const status = completed
          ? "Concluída"
          : interrupted
            ? "Interrompida"
            : active && operation.state === "reconciling"
              ? "Em conferência"
              : active
                ? operation.state === "queued"
                  ? "Na fila"
                  : "Em andamento"
                : terminal || index < activeIndex
                  ? "Sem registro"
                  : "Aguardando";
        return (
          <li
            key={step.title}
            data-state={
              completed
                ? "complete"
                : interrupted
                  ? "interrupted"
                  : active
                    ? "active"
                    : "waiting"
            }
            aria-current={active ? "step" : undefined}
          >
            <span className="publication-step-icon" aria-hidden="true">
              {completed ? (
                <Check size={18} />
              ) : interrupted ? (
                <AlertCircle size={18} />
              ) : active ? (
                <LoaderCircle size={18} />
              ) : (
                <Circle size={18} />
              )}
            </span>
            <div>
              <div className="publication-step-heading">
                <h3>{step.title}</h3>
                <span>{status}</span>
              </div>
              <p>{step.description}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function operationTitle(operation: Operation) {
  const titles: Record<string, string> = {
    queued: "Publicação na fila",
    running: "Publicação em andamento",
    reconciling: "Resultado em conferência",
    succeeded: "Publicação concluída",
    failed: "Publicação não concluída",
    cancelled: "Publicação cancelada",
  };
  return titles[operation.state] ?? "Acompanhando publicação";
}

export function PublicationPage() {
  const { projectId: pid = "" } = useParams();
  return <PublicationScreen key={pid} pid={pid} />;
}

function PublicationScreen({ pid }: { pid: string }) {
  const { user } = useSession();
  const [params, setParams] = useSearchParams();
  const operationId = params.get("operation");
  const project = useLoad(
    () => api<Project>(`/projects/${encodeURIComponent(pid)}`),
    [pid],
  );
  const tracking = usePublicationOperation(pid, operationId);
  const operation = tracking.operation;
  const readiness = usePublicationReadiness(
    pid,
    project.data?.current_revision_id ?? null,
    `${operation?.id ?? ""}:${operation?.state ?? ""}`,
    !!project.data,
  );
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const [copyStatus, setCopyStatus] = useState("");
  const sendingRef = useRef(false);
  const mounted = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);
  const statusHeading = useRef<HTMLHeadingElement>(null);
  const active = !!operation && inProgress(operation);
  const canPublish =
    !!project.data?.current_revision_id &&
    !!readiness.data?.can_publish &&
    readiness.data.revision_id === project.data?.current_revision_id &&
    !readiness.error &&
    !readiness.loading &&
    !tracking.loading &&
    !tracking.error &&
    !active &&
    !sending &&
    (!operation ||
      ["succeeded", "failed", "cancelled"].includes(operation.state));
  const publication = readiness.data?.publication?.url
    ? readiness.data.publication
    : operation?.state === "succeeded"
      ? {
          url: operation.result_ref.url,
          revision_id: operation.result_ref.revision_id,
          published_at: operation.result_ref.verified_at,
        }
      : null;
  const url = publicationUrl(publication?.url);
  useEffect(() => {
    mounted.current = true;
    heading.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => setCopyStatus(""), [url]);
  function refresh() {
    readiness.refresh();
    tracking.refresh();
    if (project.error) project.reload();
  }
  async function publish() {
    if (!canPublish || sendingRef.current || !project.data) return;
    sendingRef.current = true;
    setSending(true);
    setFailure(null);
    try {
      const accepted = await command<Operation>(
        `/projects/${encodeURIComponent(pid)}/publications`,
        {
          revision_id: project.data.current_revision_id,
        },
      );
      if (!mounted.current) return;
      tracking.accept(accepted);
      setParams({ operation: accepted.id }, { replace: true });
      readiness.refresh();
      statusHeading.current?.focus({ preventScroll: true });
    } catch (error) {
      if (mounted.current) {
        setFailure(
          error instanceof ApiError
            ? error
            : new Error(
                "O envio não pôde ser confirmado. Consulte o status acima antes de tentar novamente.",
              ),
        );
        setParams({}, { replace: true });
        refresh();
      }
    } finally {
      sendingRef.current = false;
      if (mounted.current) setSending(false);
    }
  }
  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopyStatus("Link copiado.");
    } catch {
      setCopyStatus(
        "Não foi possível copiar. Selecione o endereço exibido abaixo.",
      );
    }
  }
  const reason = readiness.error
    ? "Não foi possível verificar a publicação."
    : readiness.loading
      ? "Verificando publicação…"
      : readiness.data?.reasons.map((item) => item.message).join(" ") || "";
  const status =
    operation?.state ??
    (sending
      ? "sending"
      : tracking.error || readiness.error
        ? "unavailable"
        : readiness.loading || tracking.loading
          ? "loading"
          : canPublish
            ? "ready"
            : "blocked");
  const title = operation
    ? operationTitle(operation)
    : status === "sending"
      ? "Enviando solicitação"
      : status === "loading"
        ? "Consultando publicação"
        : status === "unavailable"
          ? "Não foi possível consultar o status"
          : status === "ready"
            ? "Pronto para publicar"
            : "Publicação aguardando liberação";
  const message = operation
    ? operation.state === "queued"
      ? "Sua solicitação foi recebida e aguarda o executor."
      : operation.state === "failed" &&
          operation.result_ref.publication_failure?.message
        ? operation.result_ref.publication_failure.message
        : operation.progress_message ||
          "O status será atualizado conforme a publicação avançar."
    : status === "ready"
      ? "Confira a revisão salva e confirme o envio para começar."
      : status === "sending"
        ? "Aguardando a confirmação do recebimento pela plataforma."
        : status === "loading"
          ? "Buscando a disponibilidade e o último resultado do projeto."
          : "Consulte as orientações abaixo para continuar.";
  const StatusIcon =
    status === "succeeded"
      ? CircleCheck
      : [
            "failed",
            "cancelled",
            "unavailable",
            "blocked",
            "reconciling",
          ].includes(status)
        ? AlertCircle
        : status === "queued"
          ? Clock3
          : active || status === "loading" || status === "sending"
            ? LoaderCircle
            : Upload;
  return (
    <div className="publication-page">
      <Link
        className="back-link"
        to={`/app/projetos/${encodeURIComponent(pid)}`}
      >
        <ArrowLeft size={17} />
        Voltar ao projeto
      </Link>
      <div className="page-heading publication-page-heading">
        <div>
          <h1 ref={heading} tabIndex={-1}>
            Publicação do projeto
          </h1>
          <p>{project.data?.name ?? "Acompanhe sua publicação"}</p>
        </div>
        <button className="button" disabled={sending} onClick={refresh}>
          <RefreshCw size={16} />
          Atualizar status
        </button>
      </div>
      <ErrorNotice error={project.error} />
      {!project.data && !project.error && <Loading />}
      {project.data && (
        <>
          <section
            className="panel publication-summary"
            data-state={status}
            aria-labelledby="publication-status-title"
          >
            <div className="publication-status-icon" aria-hidden="true">
              <StatusIcon size={28} />
            </div>
            <div className="publication-status-content">
              <div role="status" aria-live="polite" aria-atomic="true">
                <h2
                  id="publication-status-title"
                  ref={statusHeading}
                  tabIndex={-1}
                >
                  {title}
                </h2>
                <p>{message}</p>
              </div>
              {active && (
                <p className="small muted">
                  Você pode voltar ao projeto. A publicação continua e pode ser
                  acompanhada nesta tela.
                </p>
              )}
              {operation?.state === "reconciling" && (
                <p className="notice">
                  A plataforma está conferindo o resultado. Aguarde a conclusão
                  ou procure o suporte com o identificador da operação.
                </p>
              )}
              {operation?.state === "failed" && (
                <p className="small">
                  Revise a mensagem da tentativa. Um novo envio fica disponível
                  quando a plataforma liberar a publicação.
                </p>
              )}
              <span className="small muted">
                {tracking.error
                  ? "Atualização interrompida · último estado preservado"
                  : active
                    ? "Acompanhamento automático ativo"
                    : "Estado consultado na plataforma"}
                {tracking.syncedAt
                  ? ` · ${date(new Date(tracking.syncedAt).toISOString())}`
                  : ""}
              </span>
            </div>
          </section>
          {!!tracking.error && (
            <div className="publication-connection-error">
              <ErrorNotice error={tracking.error} />
              <button className="button" onClick={tracking.refresh}>
                Reconectar acompanhamento
              </button>
              {operationId && (
                <button
                  className="text-button"
                  onClick={() => setParams({}, { replace: true })}
                >
                  Ver publicação mais recente
                </button>
              )}
            </div>
          )}
          {operation && (
            <PublicationBuildDiagnostics
              key={operation.id}
              operation={operation}
              projectId={pid}
              currentRevisionId={project.data.current_revision_id}
            />
          )}
          <div className="publication-layout" data-state={status}>
            <section
              className="panel publication-progress"
              aria-labelledby="publication-stages-title"
            >
              <h2 id="publication-stages-title">Etapas da publicação</h2>
              <p className="muted small">
                O acompanhamento reflete as etapas informadas pela plataforma.
              </p>
              <PublicationSteps operation={operation} />
            </section>
            <div className="publication-side">
              <section
                className="panel publication-confirmation"
                aria-labelledby="publication-revision-title"
              >
                <h2 id="publication-revision-title">Revisão para publicar</h2>
                <dl className="publication-facts">
                  <div>
                    <dt>Revisão salva</dt>
                    <dd>
                      {project.data.current_revision_id?.slice(0, 8) ??
                        "Nenhuma revisão salva"}
                    </dd>
                  </div>
                  <div>
                    <dt>Projeto</dt>
                    <dd>{project.data.name}</dd>
                  </div>
                </dl>
                <p className="small muted">
                  A publicação usa os arquivos desta revisão salva. O link é
                  liberado após a verificação externa de frontend, API e banco.
                </p>
                <div
                  id="publication-reason"
                  className="publication-reason"
                  role="status"
                >
                  {reason}
                </div>
                <ErrorNotice error={readiness.error || failure} />
                {(!!reason || !!readiness.error) && (
                  <button
                    className="text-button small"
                    disabled={sending}
                    onClick={readiness.refresh}
                  >
                    Verificar novamente
                  </button>
                )}
                <button
                  className="button primary"
                  disabled={!canPublish}
                  aria-describedby="publication-reason"
                  onClick={() => void publish()}
                >
                  <Upload size={16} />
                  {sending ? "Enviando publicação…" : "Publicar esta revisão"}
                </button>
                {active && (
                  <p className="small muted">
                    Já existe uma publicação em andamento. Aguarde o resultado
                    para iniciar outro envio.
                  </p>
                )}
              </section>
              <section
                className="panel publication-link"
                aria-labelledby="publication-link-title"
              >
                <h2 id="publication-link-title">
                  <Globe size={19} />
                  Última publicação verificada
                </h2>
                {url ? (
                  <>
                    <p className="publication-address">{url}</p>
                    <div className="actions">
                      <a
                        className="button primary"
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <ExternalLink size={16} />
                        Abrir publicação
                      </a>
                      <button className="button" onClick={() => void copy()}>
                        <Copy size={16} />
                        Copiar link
                      </button>
                    </div>
                    <p className="small muted">
                      Revisão{" "}
                      {publication?.revision_id?.slice(0, 8) ?? "não informada"}{" "}
                      · {date(publication?.published_at)}
                    </p>
                    {active && (
                      <p className="small muted">
                        Este link corresponde à última publicação verificada. A
                        tentativa em andamento aparece nas etapas ao lado.
                      </p>
                    )}
                    <p className="small" role="status">
                      {copyStatus}
                    </p>
                  </>
                ) : (
                  <p className="small muted">
                    O endereço aparecerá aqui quando a publicação for concluída
                    e verificada.
                  </p>
                )}
              </section>
            </div>
          </div>
          {operation && (
            <details className="panel publication-details">
              <summary>Detalhes da publicação</summary>
              <dl className="publication-facts">
                <div>
                  <dt>Operação</dt>
                  <dd>{operation.id}</dd>
                </div>
                <div>
                  <dt>Solicitada em</dt>
                  <dd>{date(operation.created_at)}</dd>
                </div>
                {operation.result_ref.revision_id && (
                  <div>
                    <dt>Revisão da operação</dt>
                    <dd>{operation.result_ref.revision_id}</dd>
                  </div>
                )}
                {operation.error_code && (
                  <div>
                    <dt>Código para suporte</dt>
                    <dd>{operation.error_code}</dd>
                  </div>
                )}
              </dl>
              {!!operation.activity?.length && (
                <ol className="publication-activity">
                  {operation.activity.map((event, index) => (
                    <li key={`${event.stage}:${event.at}:${index}`}>
                      <time dateTime={event.at}>{date(event.at)}</time>
                      <span>
                        {event.stage === "queued"
                          ? "Solicitação de publicação recebida."
                          : event.message}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
              {operationId && (
                <button
                  className="text-button"
                  onClick={() => setParams({}, { replace: true })}
                >
                  Ver publicação mais recente
                </button>
              )}
            </details>
          )}
          <div className="learning-nudge publication-retention">
            <ShieldCheck size={22} />
            <div>
              <strong>Hospedagem e arquivos até o dia 32 do ciclo</strong>
              <p>
                {user?.access.purge_at
                  ? `Baixe antes de ${date(user.access.purge_at)}.`
                  : "Consulte Uso e acesso para conferir o prazo de download."}{" "}
                Sem renovação, o acesso aos arquivos termina e o expurgo é
                processado.
              </p>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
