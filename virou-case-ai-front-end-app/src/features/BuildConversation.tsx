import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleAlert,
  Clock3,
  FileCode2,
  LoaderCircle,
  MessageSquare,
  Square,
} from "lucide-react";
import { Link } from "react-router-dom";
import { date, states } from "../shared/api";
import type { Operation } from "../shared/types";
import { inProgress } from "./useProjectOperations";

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
  onCancel,
  onRetry,
  onFile,
  onReview,
  onAnswer,
}: {
  operations: Operation[];
  pid: string;
  busy: boolean;
  syncedAt: number;
  loadedRevision: string | null;
  onCancel: (operation: Operation) => void;
  onRetry: (request: string) => void;
  onFile: (path: string) => void;
  onReview?: (operation: Operation) => void;
  onAnswer?: (operation: Operation) => void;
}) {
  const [now, setNow] = useState(Date.now());
  const log = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const builds = operations
    .filter((operation) => operation.type === "generation")
    .slice()
    .reverse();
  const active = builds.some(inProgress);
  const last = builds.at(-1);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  useEffect(() => {
    if (follow.current && log.current)
      log.current.scrollTop = log.current.scrollHeight;
  }, [
    last?.id,
    last?.state,
    last?.progress_stage,
    last?.activity?.length,
    loadedRevision,
  ]);
  return (
    <div
      className="build-conversation"
      ref={log}
      role="region"
      aria-label="Conversa de construção"
      tabIndex={0}
      onScroll={(event) => {
        const element = event.currentTarget;
        follow.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < 80;
      }}
    >
      {builds.length === 0 && (
        <div className="build-welcome">
          <FileCode2 size={24} aria-hidden="true" />
          <h3>Da sua ideia aos arquivos</h3>
          <p>
            Descreva o resultado esperado. Você acompanha a construção aqui e
            recebe os arquivos em uma nova revisão.
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
        return (
          <article
            className="build-turn"
            key={operation.id}
            data-state={operation.state}
            data-kind={operation.result_ref.kind}
          >
            <div className="build-request">
              <strong>Você</strong>
              <p>{operation.request_text || "Solicitação anterior"}</p>
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
                    ? "Aguardando sua resposta"
                    : (states[operation.state] ?? operation.state)}
                </strong>
                <span className="sr-only">. {operationMessage(operation)}</span>
              </div>
              <p>{operationMessage(operation)}</p>
              {operation.result_ref.message && (
                <p className="assistant-message">
                  {operation.result_ref.message}
                </p>
              )}
              {question && onAnswer && (
                <button
                  className="button"
                  disabled={busy || active}
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
                  A conexão está instável. Exibindo o último estado confirmado;
                  tentando reconectar.
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
                      ? operation.result_ref.applied_revision_id
                        ? "Alterações revisadas e aplicadas."
                        : "Alterações prontas. Revise e aplique ao seu projeto."
                      : `${operation.result_ref.changed_files?.length ?? ""} arquivo(s) criado(s) ou alterado(s).`}
                  </p>
                  {operation.result_ref.validation === "structure_checked" && (
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
                  <Link
                    className="text-button"
                    to={`/app/projetos/${pid}/historico`}
                  >
                    Ver revisão no histórico
                  </Link>
                  {operation.result_ref.candidate &&
                    !operation.result_ref.applied_revision_id &&
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
                {terminal &&
                  operation.state !== "succeeded" &&
                  operation.request_text && (
                    <button
                      className="button"
                      disabled={busy || active}
                      onClick={() => onRetry(operation.request_text!)}
                    >
                      Reutilizar pedido
                    </button>
                  )}
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}
