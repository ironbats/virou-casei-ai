import { useId } from "react";
import { ArrowRight, LoaderCircle, ScanSearch, Wrench } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useSession } from "../app/session";
import { ErrorNotice } from "../shared/ui";
import type { Operation } from "../shared/types";
import { inProgress } from "./useProjectOperations";
import { useCodeAnalysis } from "./useCodeAnalysis";
import { correctionConversationPath } from "./codeCorrection";
import "./codeAnalysis.css";

type CodeAnalysisProps = {
  projectId: string;
  revisionId: string | null;
  source: "publication" | "preview" | "editor";
  publicationOperationId?: string;
  runtimeError?: string;
  dirty?: boolean;
  blocked?: boolean;
  onCorrectionAccepted?: (operation: Operation) => void;
};

export function CodeAnalysisPanel(props: CodeAnalysisProps) {
  const analysis = useCodeAnalysis(props);
  const navigate = useNavigate();
  return (
    <CodeAnalysisContent
      {...props}
      analysis={analysis}
      onCorrectionAccepted={
        props.onCorrectionAccepted ??
        ((operation) =>
          navigate(correctionConversationPath(props.projectId, operation.id)))
      }
    />
  );
}

export function CodeAnalysisContent({
  projectId,
  revisionId,
  dirty = false,
  blocked = false,
  onCorrectionAccepted,
  analysis,
}: CodeAnalysisProps & { analysis: ReturnType<typeof useCodeAnalysis> }) {
  const id = useId();
  const { user } = useSession();
  const operation = analysis.operation;
  const correction = analysis.correction;
  const active = !!operation && inProgress(operation);
  const correcting = !!correction && inProgress(correction);
  const writable = !!user?.access.allowed_actions.includes("write");
  const result =
    operation?.state === "succeeded" &&
    operation.result_ref.kind === "code_analysis" &&
    typeof operation.result_ref.summary === "string" &&
    Array.isArray(operation.result_ref.issues)
      ? operation.result_ref
      : null;
  const disabled =
    !writable ||
    !revisionId ||
    dirty ||
    blocked ||
    analysis.loading ||
    analysis.recovering ||
    !!analysis.sending ||
    active ||
    correcting;
  const reason = !writable
    ? "Seu acesso permite consultar o diagnóstico. Solicitar análise ou correção exige permissão de edição."
    : !revisionId
      ? "Salve uma revisão do projeto antes de pedir a análise."
      : dirty
        ? "Salve as alterações em uma revisão antes de pedir análise ou correção."
        : blocked
          ? "Aguarde a operação atual ou resolva o conflito do rascunho para continuar."
          : "A análise usa os arquivos da revisão salva e a cota de IA da sua conta.";
  const revisionMatches = result?.revision_id === revisionId;
  const canCorrect =
    !!result?.can_request_correction &&
    revisionMatches &&
    !disabled &&
    !correction &&
    !analysis.syncError;
  return (
    <section className="code-analysis" aria-labelledby={`${id}-title`}>
      <div className="code-analysis-heading">
        <div>
          <h3 id={`${id}-title`}>
            <ScanSearch size={19} aria-hidden="true" /> Análise de código com IA
          </h3>
          <p className="small muted">
            Entenda o problema e revise uma proposta de correção antes de
            aplicá-la.
          </p>
        </div>
        <button
          className="button"
          disabled={disabled || !!analysis.syncError}
          aria-describedby={`${id}-reason`}
          onClick={() => void analysis.analyze()}
        >
          {active || analysis.sending === "analysis" ? (
            <LoaderCircle size={16} aria-hidden="true" />
          ) : (
            <ScanSearch size={16} aria-hidden="true" />
          )}
          {analysis.sending === "analysis"
            ? "Solicitando análise…"
            : active
              ? "Analisando código…"
              : operation
                ? "Pedir nova análise IA"
                : "Pedir Análise IA"}
        </button>
      </div>
      <p id={`${id}-reason`} className="small muted">
        {reason}
      </p>
      <ErrorNotice error={analysis.error || analysis.syncError} />
      {!!analysis.syncError && (
        <button className="text-button" onClick={analysis.refresh}>
          Atualizar análise
        </button>
      )}
      {analysis.loading && (
        <p className="small" role="status">
          Consultando análises anteriores…
        </p>
      )}
      {analysis.recovering && (
        <p className="small" role="status">
          Conferindo automaticamente se a análise foi recebida…
        </p>
      )}
      {analysis.recoveringCorrection && (
        <p className="small" role="status">
          Conferindo se a correção foi recebida para abrir a conversa…
        </p>
      )}
      {active && (
        <p className="notice" role="status">
          {operation.progress_message ||
            "A IA está examinando a revisão salva. O código permanece inalterado."}
        </p>
      )}
      {operation &&
        ["failed", "cancelled", "reconciling"].includes(operation.state) && (
          <p
            className="notice"
            role={operation.state === "failed" ? "alert" : "status"}
          >
            {operation.progress_message ||
              (operation.state === "failed"
                ? "A análise não foi concluída. Consulte o status e tente novamente."
                : "Aguardando a confirmação do resultado da análise.")}
            {operation.error_code && (
              <span className="code-analysis-reference">
                Referência: {operation.error_code}
              </span>
            )}
          </p>
        )}
      {operation?.state === "succeeded" && !result && (
        <p className="notice error" role="alert">
          O resultado da análise não está disponível em um formato válido.
          Atualize o acompanhamento antes de solicitar uma correção.
        </p>
      )}
      {result && (
        <div className="code-analysis-result">
          <h4>Diagnóstico da IA</h4>
          <p className="code-analysis-text">{result.summary}</p>
          <p className="small muted">
            Revisão analisada: {result.revision_id?.slice(0, 8)}. As conclusões
            são hipóteses da IA; a validação exige compilar e testar a revisão
            corrigida.
          </p>
          {!revisionMatches && (
            <p className="notice">
              A revisão do projeto mudou. Peça uma nova análise antes de
              solicitar correções.
            </p>
          )}
          {!!result.issues?.length && (
            <ol className="code-analysis-issues">
              {result.issues.map((issue, index) => (
                <li
                  key={`${issue.path}:${index}`}
                  data-severity={issue.severity}
                >
                  <div className="code-analysis-issue-heading">
                    <strong>{issue.title}</strong>
                    <span>
                      {issue.severity === "error"
                        ? "Erro"
                        : issue.severity === "warning"
                          ? "Atenção"
                          : "Informação"}
                    </span>
                  </div>
                  {issue.path && (
                    <p className="code-analysis-location">
                      {issue.path}
                      {issue.line ? `:${issue.line}` : ""}
                    </p>
                  )}
                  <p className="code-analysis-text">{issue.explanation}</p>
                  {issue.suggestion && (
                    <p className="code-analysis-text">
                      <strong>Correção sugerida:</strong> {issue.suggestion}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
          {!!result.limitations?.length && (
            <div className="notice code-analysis-limitations">
              <strong>Limites da análise</strong>
              <ul>
                {result.limitations.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {!result.can_request_correction && (
            <p className="small muted">
              Esta análise não identificou uma correção de código que possa ser
              solicitada automaticamente. Confira o diagnóstico e as orientações
              acima.
            </p>
          )}
          {result.can_request_correction && !correction && (
            <button
              className="button primary"
              disabled={!canCorrect}
              onClick={() =>
                void analysis.requestCorrection(onCorrectionAccepted)
              }
            >
              <Wrench size={16} aria-hidden="true" />
              {analysis.sending === "correction"
                ? "Solicitando correção…"
                : "Solicitar correção IA"}
            </button>
          )}
        </div>
      )}
      {correction && (
        <div className="code-analysis-correction">
          <p>A correção está registrada na conversa do projeto.</p>
          <Link
            className="button primary"
            to={correctionConversationPath(projectId, correction.id)}
          >
            Acompanhar correção no chat{" "}
            <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
      )}
    </section>
  );
}
