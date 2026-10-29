import { Code2, FileText, Info } from "lucide-react";
import type { RecoverySnapshot } from "./publicationRecovery";
import { Link } from "react-router-dom";
import type { Operation } from "../shared/types";
import {
  publicationDiagnostic,
  publicationPhase,
  publicationRecoveryAction,
} from "./publicationDiagnostic";

const owners = {
  user: "Você, responsável pelo código",
  operator: "Responsável pela operação da plataforma",
  platform: "Recuperação automática da plataforma",
};
const effects: Record<string, string> = {
  not_sent: "Envio do SQL não iniciado",
  none: "Ausência de efeitos da migration confirmada",
  unknown: "Efeitos no banco ainda não confirmados",
  confirmed: "Execução do SQL confirmada; recibo verificado separadamente",
};

export function PublicationOperationDiagnostics({
  operation,
  projectId,
  recoverySnapshot,
}: {
  operation: Operation;
  projectId: string;
  recoverySnapshot?: RecoverySnapshot | null;
}) {
  const diagnostic = publicationDiagnostic(operation);
  const recovery = operation.result_ref.publication_recovery;
  const failure = operation.result_ref.publication_failure;
  if (
    !diagnostic &&
    !recovery &&
    !recoverySnapshot &&
    (failure?.build || failure?.migration)
  )
    return null;
  const recoveryDiagnostic = recoverySnapshot?.diagnostic;
  const initial =
    diagnostic?.initial_error ?? recoveryDiagnostic?.initial_error;
  const migration = diagnostic?.migration ?? recoverySnapshot?.migration;
  const effectStatus =
    diagnostic?.effect_status ?? recoveryDiagnostic?.effect_status;
  const currentError = recoveryDiagnostic?.current_error;
  const initialCode = initial?.code || failure?.code || operation.error_code;
  const provider =
    initial?.provider ?? recoveryDiagnostic?.initial_error?.provider;
  if (
    !["failed", "reconciling"].includes(operation.state) ||
    (!initialCode && !recovery && !diagnostic)
  )
    return null;
  const developer = !!recoverySnapshot?.resolution;
  const resolved = recoverySnapshot?.classification === "resolved";
  const nextAction = developer
    ? resolved
      ? "Pendência encerrada. Confira a prontidão da revisão atual antes de um novo envio."
      : recoverySnapshot.resolution?.eligibility === "ineligible"
        ? recoverySnapshot.resolution.next_action.label
        : "Conferir e resolver neste projeto"
    : publicationRecoveryAction(operation);
  const missingEvidence =
    recoverySnapshot?.resolution?.missing_evidence ??
    recovery?.missing_evidence ??
    [];
  const hasRecoveryError = !!currentError?.code || !!recovery?.last_error_code;
  const detailed =
    !!migration ||
    !!provider ||
    (diagnostic?.detail_available ??
      (!!diagnostic || !!failure?.message || !!recovery?.last_error_message));
  return (
    <section
      className="panel publication-operation-diagnostics"
      aria-labelledby="publication-operation-diagnostic-title"
    >
      <h2 id="publication-operation-diagnostic-title">
        <FileText size={22} aria-hidden="true" />
        Diagnóstico da tentativa
      </h2>
      <div className="publication-diagnostic-errors">
        <div>
          <h3>Erro inicial</h3>
          <p>
            <code>{initialCode || "Não registrado"}</code>
          </p>
          <p>
            {initial?.message ||
              failure?.message ||
              "Diagnóstico detalhado indisponível nesta tentativa."}
          </p>
          {provider?.message && provider.message !== initial?.message && (
            <p>{provider.message}</p>
          )}
        </div>
        <div>
          <h3>
            {operation.state === "reconciling"
              ? "Impedimento atual"
              : "Última conferência de recuperação"}
          </h3>
          {hasRecoveryError ? (
            <>
              <p>
                <code>{currentError?.code || recovery?.last_error_code}</code>
              </p>
              <p>
                {currentError?.message ||
                  recovery?.last_error_message ||
                  recovery?.message ||
                  "O motivo detalhado da última conferência não foi registrado."}
              </p>
            </>
          ) : (
            <p>
              {operation.state === "reconciling"
                ? "O resultado externo ainda está em conferência. Nenhum erro adicional foi registrado."
                : recovery?.message ||
                  "A operação foi encerrada. Não há recuperação pendente nesta tentativa."}
            </p>
          )}
        </div>
      </div>
      <dl className="publication-facts publication-build-facts">
        <div>
          <dt>Fase interrompida</dt>
          <dd>{publicationPhase(operation, recoveryDiagnostic?.phase)}</dd>
        </div>
        <div>
          <dt>Revisão da tentativa</dt>
          <dd>{operation.result_ref.revision_id || "Não informada"}</dd>
        </div>
        {migration && (
          <div>
            <dt>Migration</dt>
            <dd>{migration.name}</dd>
          </div>
        )}
        {effectStatus && (
          <div>
            <dt>
              {resolved
                ? "Resultado registrado na tentativa"
                : "Resultado do SQL"}
            </dt>
            <dd>{effects[effectStatus] || "Efeito ainda não identificado"}</dd>
          </div>
        )}
        {resolved && recoverySnapshot?.request?.state === "resolved" && (
          <div>
            <dt>Resultado confirmado pela recuperação</dt>
            <dd>
              Ausência de efeitos comprovada. Nenhuma migration foi reaplicada.
            </dd>
          </div>
        )}
        {provider?.statement_index !== undefined && (
          <div>
            <dt>Instrução</dt>
            <dd>
              {provider.statement_index}
              {provider.line ? ` · linha ${provider.line}` : ""}
            </dd>
          </div>
        )}
        {recovery?.owner && !developer && (
          <div>
            <dt>Responsável pelo próximo passo</dt>
            <dd>{owners[recovery.owner]}</dd>
          </div>
        )}
      </dl>
      <div className="notice publication-next-step">
        <h3>Próximo passo</h3>
        <p>{nextAction}</p>
        {!!missingEvidence.length && (
          <>
            <p>Evidência necessária para continuar:</p>
            <ul>
              {missingEvidence.map((evidence, index) => (
                <li key={`${index}:${evidence}`}>{evidence}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      {provider?.status === 400 &&
        provider.provider_error_codes?.includes(7500) && (
          <p className="notice publication-unknown-cause">
            <Info size={20} aria-hidden="true" />
            <span>
              <strong>A causa exata da recusa não está confirmada.</strong>{" "}
              Consulte os detalhes técnicos e as evidências para entender o
              problema.
            </span>
          </p>
        )}
      {!detailed && (
        <p className="small muted">
          {(initial?.message || failure?.message) &&
            "Diagnóstico detalhado indisponível nesta tentativa. "}
          Os registros antigos não permitem identificar a causa remota com
          precisão. Nenhuma causa adicional foi presumida.
        </p>
      )}
      <p className="small muted">
        Consultar este diagnóstico não usa cota de IA. A Análise IA é opcional e
        não confirma execução no banco nem libera a publicação.
      </p>
      <div className="actions">
        <Link
          className="button"
          to={`/app/projetos/${encodeURIComponent(projectId)}?publication_operation=${encodeURIComponent(operation.id)}`}
        >
          <Code2 size={18} aria-hidden="true" />
          Abrir código do projeto
        </Link>
      </div>
      {provider && (
        <details className="publication-build-execution">
          <summary>Dados técnicos da recusa</summary>
          <dl className="publication-facts publication-build-facts">
            {provider.status !== undefined && (
              <div>
                <dt>HTTP</dt>
                <dd>{provider.status}</dd>
              </div>
            )}
            {provider.provider_error_codes?.length ? (
              <div>
                <dt>Códigos do provedor</dt>
                <dd>{provider.provider_error_codes.join(", ")}</dd>
              </div>
            ) : null}
            {provider.cf_ray && (
              <div>
                <dt>Correlação do provedor</dt>
                <dd>{provider.cf_ray}</dd>
              </div>
            )}
            {provider.method && (
              <div>
                <dt>Método</dt>
                <dd>{provider.method}</dd>
              </div>
            )}
            {provider.route && (
              <div>
                <dt>Rota sanitizada</dt>
                <dd>{provider.route}</dd>
              </div>
            )}
            {migration?.sha256 && (
              <div>
                <dt>SHA-256 da migration</dt>
                <dd>{migration.sha256}</dd>
              </div>
            )}
          </dl>
        </details>
      )}
    </section>
  );
}
