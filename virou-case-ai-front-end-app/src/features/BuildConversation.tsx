import { useEffect, useState, type ReactNode } from "react";
import {
  ArrowDown,
  CheckCircle2,
  CircleAlert,
  Clock3,
  FileCode2,
  LoaderCircle,
  MessageSquare,
  Square,
  Wrench,
} from "lucide-react";
import { Link } from "react-router-dom";
import { date, states } from "../shared/api";
import type { Operation } from "../shared/types";
import { Modal } from "../shared/ui";
import { inProgress } from "./useProjectOperations";
import { useConversationScroll } from "./useConversationScroll";
import { MigrationFailureNotice } from "./MigrationFailureNotice";

export function operationMessage(operation: Operation) {
  if (operation.progress_message) return operation.progress_message;
  if (operation.state === "failed")
    return "Não foi possível concluir. Sua revisão foi preservada. Revise o pedido e tente novamente.";
  if (operation.state === "reconciling")
    return "O resultado está em apuração. Aguarde antes de tentar novamente.";
  if (operation.state === "cancelled") return "Operação cancelada.";
  if (operation.state === "succeeded") return "Nova revisão salva.";
  return operation.state === "queued"
    ? "Solicitação recebida. Preparando a construção."
    : "Construindo as alterações solicitadas.";
}

function duration(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return seconds < 60
    ? `${seconds}s`
    : `${Math.floor(seconds / 60)}min ${seconds % 60}s`;
}

export function BuildConversation({
  operations,
  pid,
  busy,
  syncedAt,
  loadedRevision,
  followRequest = 0,
  onCancel,
  onRetry,
  onFile,
  onReview,
  onAnswer,
  onDiscard,
  analysisContent,
  onRetryCorrection,
  correctionBlockedReason = "",
}: {
  operations: Operation[];
  pid: string;
  busy: boolean;
  syncedAt: number;
  loadedRevision: string | null;
  followRequest?: number;
  onCancel: (operation: Operation) => void;
  onRetry: (request: string) => void;
  onFile: (path: string) => void;
  onReview?: (operation: Operation) => void;
  onAnswer?: (operation: Operation) => void;
  onDiscard?: (operation: Operation) => void;
  analysisContent?: ReactNode;
  onRetryCorrection?: (operation: Operation) => void;
  correctionBlockedReason?: string;
}) {
  const [now, setNow] = useState(Date.now());
  const [snapshot, setSnapshot] = useState<{
    title: string;
    content: string;
  } | null>(null);
  const scroll = useConversationScroll(followRequest);
  const builds = operations
    .filter((operation) => operation.type === "generation")
    .slice()
    .reverse();
  const active = builds.some(inProgress);
  const answeredQuestions = new Set(
    builds
      .filter(
        (operation) =>
          operation.reply_to &&
          (operation.state === "succeeded" || inProgress(operation)),
      )
      .map((operation) => operation.reply_to),
  );
  const latestReplies = new Map<string, Operation>();
  for (const operation of builds)
    if (operation.reply_to) latestReplies.set(operation.reply_to, operation);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return (
    <div className="build-conversation-view">
      <div
        className="build-conversation"
        ref={scroll.viewport}
        role="region"
        aria-label="Conversa de construção"
        tabIndex={0}
        onScroll={scroll.onScroll}
      >
        <div className="build-conversation-content" ref={scroll.content}>
          {builds.length === 0 && !analysisContent && (
            <div className="build-welcome">
              <FileCode2 size={24} aria-hidden="true" />
              <h3>Da sua ideia aos arquivos</h3>
              <p>
                Descreva o resultado esperado. Você acompanha a construção aqui
                e recebe os arquivos em uma nova revisão.
              </p>
              <p className="small muted">
                Não inclua senhas ou chaves de acesso no pedido.
              </p>
            </div>
          )}
          {builds.map((operation) => {
            const working = inProgress(operation);
            const terminal = !working;
            const question = operation.result_ref.kind === "clarification";
            const answered = question && answeredQuestions.has(operation.id);
            const reply = question ? latestReplies.get(operation.id) : null;
            const resumed =
              !!operation.reply_to &&
              (latestReplies.get(operation.reply_to)?.id !== operation.id ||
                answeredQuestions.has(operation.reply_to));
            const progressMessage =
              question && reply
                ? answered
                  ? "Sua resposta foi enviada. A continuação está registrada nesta conversa."
                  : "Sua resposta foi recebida. A continuação não foi concluída; retome pela última tentativa abaixo."
                : operationMessage(operation);
            const proposal = !!operation.result_ref.candidate && !question;
            const explanation = operation.result_ref.message?.replace(
              /```[\s\S]*?```/g,
              "O código proposto está disponível em Revisar alterações.",
            );
            const failureCode =
              operation.state === "failed" &&
              typeof operation.error_code === "string" &&
              /^[A-Z][A-Z0-9_]{0,59}$/.test(operation.error_code)
                ? operation.error_code
                : null;
            const failureReference =
              operation.state === "failed" &&
              /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
                operation.id,
              )
                ? operation.id
                : null;
            const correction = !!operation.analysis_id && !operation.reply_to;
            const correctionStale =
              !!operation.analysis_revision_id &&
              operation.analysis_revision_id !== loadedRevision;
            const correctionRetryable =
              correction &&
              ["failed", "cancelled"].includes(operation.state) &&
              !operation.pending_reconciliation &&
              operation.billing_status !== "reconciliation_pending";
            const replyRetryable =
              !!operation.reply_to &&
              !resumed &&
              ["failed", "cancelled"].includes(operation.state);
            return (
              <article
                className="build-turn"
                key={operation.id}
                data-state={operation.state}
                data-kind={operation.result_ref.kind}
                data-operation-id={operation.id}
              >
                <div className="build-request">
                  <strong>Você</strong>
                  {operation.analysis_id && (
                    <span className="code-correction-label">
                      Correção a partir da análise IA
                    </span>
                  )}
                  <p>{operation.request_text || "Solicitação anterior"}</p>
                  {!!operation.attachment_snapshots?.length && (
                    <div className="sent-attachments">
                      {operation.attachment_snapshots.map((a) => (
                        <button
                          className="button"
                          key={a.id}
                          onClick={() => setSnapshot(a)}
                        >
                          Ver conteúdo enviado · {a.title} · v{a.version}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div className="build-response">
                  <div
                    className="build-status"
                    role="status"
                    aria-live="polite"
                    aria-atomic="true"
                  >
                    {question ? (
                      <MessageSquare size={19} />
                    ) : operation.state === "succeeded" ? (
                      <CheckCircle2 size={19} />
                    ) : operation.state === "failed" ? (
                      <CircleAlert size={19} />
                    ) : working ? (
                      <LoaderCircle className="build-spinner" size={19} />
                    ) : (
                      <Square size={17} />
                    )}
                    <strong>
                      {question
                        ? answered
                          ? "Resposta enviada"
                          : reply
                            ? "Resposta recebida"
                            : "Aguardando sua resposta"
                        : (states[operation.state] ?? operation.state)}
                    </strong>
                    <span className="sr-only">. {progressMessage}</span>
                  </div>
                  <p>{progressMessage}</p>
                  {operation.state === "failed" && (
                    <MigrationFailureNotice
                      failure={operation.result_ref.migration_failure}
                      messageShown={progressMessage}
                    />
                  )}
                  {(failureCode || failureReference) && (
                    <details className="build-activity">
                      <summary>Detalhes da falha</summary>
                      {failureCode && (
                        <p>
                          Código: <code>{failureCode}</code>
                        </p>
                      )}
                      {failureReference && (
                        <p>
                          Referência da operação:{" "}
                          <code>{failureReference}</code>
                        </p>
                      )}
                    </details>
                  )}
                  {explanation && proposal && explanation.length > 420 ? (
                    <details className="build-activity">
                      <summary>Ver explicação da proposta</summary>
                      <p className="assistant-message">{explanation}</p>
                    </details>
                  ) : explanation ? (
                    <p className="assistant-message">{explanation}</p>
                  ) : null}
                  {question && !reply && onAnswer && (
                    <button
                      className="button"
                      disabled={
                        busy ||
                        active ||
                        (!!operation.analysis_id &&
                          (correctionStale || !!correctionBlockedReason))
                      }
                      onClick={() => onAnswer(operation)}
                    >
                      Responder
                    </button>
                  )}
                  {working && !!operation.result_ref.working_files?.length && (
                    <div
                      className="build-files"
                      aria-label="Arquivos em construção"
                    >
                      {operation.result_ref.working_files.map((path) => (
                        <span className="build-file" key={path}>
                          <FileCode2 size={15} />
                          <span>{path}</span>
                          <small>Em construção</small>
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="small muted build-time">
                    <Clock3 size={13} aria-hidden="true" />
                    {date(operation.created_at)}
                    {working &&
                      ` · ${duration(now - new Date(operation.created_at).getTime())}`}
                  </p>
                  {working &&
                    operation.state !== "reconciling" &&
                    now - new Date(operation.created_at).getTime() > 60000 && (
                      <p className="small muted">
                        A construção está levando mais tempo. Você pode sair e
                        voltar: o acompanhamento será retomado.
                      </p>
                    )}
                  {working && syncedAt > 0 && now - syncedAt > 10000 && (
                    <p className="small build-warning">
                      A conexão está instável. Exibindo o último estado
                      confirmado; tentando reconectar.
                    </p>
                  )}
                  {!!operation.activity?.length && (
                    <details className="build-activity" open={working}>
                      <summary>Etapas da construção</summary>
                      <ol>
                        {operation.activity.map((event, index) => (
                          <li key={`${event.stage}-${index}`}>
                            <span aria-hidden="true">
                              {index < operation.activity!.length - 1 ||
                              operation.state === "succeeded"
                                ? "✓"
                                : "•"}
                            </span>
                            <span>{event.message}</span>
                          </li>
                        ))}
                      </ol>
                    </details>
                  )}
                  {operation.state === "succeeded" && !question && (
                    <>
                      {operation.result_ref.apply_conflict && (
                        <p className="notice">
                          O projeto ou rascunho mudou durante a geração. Seu
                          conteúdo foi preservado; compare a proposta antes de
                          aplicar.
                        </p>
                      )}
                      <p className="build-result">
                        {operation.result_ref.candidate
                          ? operation.result_ref.discarded_at
                            ? "Proposta descartada."
                            : operation.result_ref.applied_revision_id
                              ? "Alterações revisadas e aplicadas."
                              : "Alterações prontas. Revise e aplique ao seu projeto."
                          : `${operation.result_ref.changed_files?.length ?? ""} arquivo(s) criado(s) ou alterado(s).`}
                      </p>
                      {proposal &&
                        !!operation.result_ref.changed_files?.length && (
                          <p
                            className="small muted"
                            aria-label="Resumo dos arquivos da proposta"
                          >
                            {
                              operation.result_ref.changed_files.filter(
                                (file) => file.change === "modified",
                              ).length
                            }{" "}
                            alterado(s)
                            {" · "}
                            {
                              operation.result_ref.changed_files.filter(
                                (file) => file.change === "created",
                              ).length
                            }{" "}
                            criado(s)
                            {" · "}
                            {
                              operation.result_ref.changed_files.filter(
                                (file) => file.change === "deleted",
                              ).length
                            }{" "}
                            excluído(s)
                          </p>
                        )}
                      {operation.result_ref.validation ===
                        "structure_checked" && (
                        <p className="small muted">
                          Estrutura dos arquivos conferida. Execução e testes do
                          projeto gerado ainda precisam ser realizados.
                        </p>
                      )}
                      {!operation.result_ref.candidate &&
                        operation.result_ref.revision_id === loadedRevision && (
                          <div className="build-files">
                            {operation.result_ref.changed_files?.map((file) => (
                              <button
                                key={file.path}
                                className="build-file"
                                onClick={() => onFile(file.path)}
                              >
                                <FileCode2 size={15} aria-hidden="true" />
                                <span>{file.path}</span>
                                <small>
                                  {file.change === "created"
                                    ? "Criado"
                                    : "Alterado"}
                                </small>
                              </button>
                            ))}
                          </div>
                        )}
                      {operation.result_ref.discarded_at && (
                        <p>
                          Proposta descartada. A revisão anterior foi
                          preservada.
                        </p>
                      )}
                      {operation.result_ref.candidate &&
                        !operation.result_ref.applied_revision_id &&
                        !operation.result_ref.discarded_at &&
                        onDiscard && (
                          <button
                            className="button"
                            disabled={busy}
                            onClick={() => {
                              if (
                                confirm(
                                  "Descartar esta proposta sem alterar a revisão atual?",
                                )
                              )
                                onDiscard(operation);
                            }}
                          >
                            Descartar proposta
                          </button>
                        )}
                      <Link
                        className="text-button"
                        to={`/app/projetos/${pid}/historico`}
                      >
                        Ver revisão no histórico
                      </Link>
                      {operation.result_ref.candidate &&
                        !operation.result_ref.applied_revision_id &&
                        !operation.result_ref.discarded_at &&
                        onReview && (
                          <button
                            className="button primary"
                            onClick={() => onReview(operation)}
                            disabled={busy}
                          >
                            Revisar alterações
                          </button>
                        )}
                    </>
                  )}
                  <div className="actions">
                    {["queued", "running"].includes(operation.state) && (
                      <button
                        className="button"
                        disabled={busy || operation.cancel_requested}
                        onClick={() => onCancel(operation)}
                      >
                        <Square size={14} />
                        {operation.cancel_requested
                          ? "Cancelamento solicitado"
                          : "Cancelar construção"}
                      </button>
                    )}
                    {correctionRetryable && onRetryCorrection && (
                      <button
                        className="button"
                        disabled={
                          busy ||
                          active ||
                          correctionStale ||
                          !!correctionBlockedReason
                        }
                        onClick={() => onRetryCorrection(operation)}
                      >
                        <Wrench size={16} aria-hidden="true" /> Tentar correção
                        novamente
                      </button>
                    )}
                    {terminal &&
                      operation.state !== "succeeded" &&
                      !correction &&
                      (!operation.reply_to || replyRetryable) &&
                      operation.request_text && (
                        <button
                          className="button"
                          disabled={
                            busy ||
                            active ||
                            (!!operation.analysis_id &&
                              (correctionStale ||
                                !!correctionBlockedReason ||
                                operation.pending_reconciliation ||
                                operation.billing_status ===
                                  "reconciliation_pending"))
                          }
                          onClick={() =>
                            operation.reply_to && onAnswer
                              ? onAnswer(operation)
                              : onRetry(operation.request_text!)
                          }
                        >
                          {operation.reply_to && onAnswer
                            ? "Tentar responder novamente"
                            : "Reutilizar pedido"}
                        </button>
                      )}
                  </div>
                  {terminal && operation.state !== "succeeded" && resumed && (
                    <p className="small muted">
                      Esta pergunta já possui uma continuação nesta conversa.
                    </p>
                  )}
                  {(correctionRetryable ||
                    ((replyRetryable || (question && !reply)) &&
                      !!operation.analysis_id)) &&
                    (correctionStale || correctionBlockedReason) && (
                      <p className="small muted">
                        {correctionStale
                          ? "A revisão mudou. Peça uma nova análise do código salvo antes de solicitar outra correção."
                          : correctionBlockedReason}
                      </p>
                    )}
                  {operation.analysis_id &&
                    (operation.pending_reconciliation ||
                      operation.billing_status ===
                        "reconciliation_pending") && (
                      <p className="small muted">
                        Aguarde a conferência da tentativa anterior antes de
                        solicitar outra correção.
                      </p>
                    )}
                </div>
              </article>
            );
          })}
          {analysisContent}
        </div>
      </div>
      {scroll.awayFromLatest && (
        <button
          className="button build-follow-latest"
          onClick={() => {
            scroll.scrollToLatest();
            scroll.viewport.current?.focus({ preventScroll: true });
          }}
        >
          <ArrowDown size={16} aria-hidden="true" />
          Ir para a última mensagem
        </button>
      )}
      {snapshot && (
        <Modal
          title={`Conteúdo enviado · ${snapshot.title}`}
          close={() => setSnapshot(null)}
        >
          <pre className="source-preview">{snapshot.content}</pre>
        </Modal>
      )}
    </div>
  );
}
