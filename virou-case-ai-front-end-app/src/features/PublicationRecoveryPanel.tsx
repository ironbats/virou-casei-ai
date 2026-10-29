import { useEffect, useRef, useState } from "react";
import {
  Check,
  Download,
  FileCheck2,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { ApiError, date } from "../shared/api";
import type { Operation } from "../shared/types";
import { ErrorNotice } from "../shared/ui";
import {
  recoveryExport,
  recoveryRequestActive,
  type RecoverySnapshot,
} from "./publicationRecovery";
import { usePublicationRecovery } from "./usePublicationRecovery";

const classifications: Record<RecoverySnapshot["classification"], string> = {
  unassessed: "Aguardando conferência",
  confirmed: "Aplicação confirmada",
  no_effect: "Ausência de efeito confirmada",
  schema_applied: "Estrutura compatível com a migration",
  schema_unchanged: "Estrutura compatível com o estado anterior",
  unknown: "Resultado ainda não confirmado",
  conflict: "Evidências divergentes",
  resolved: "Pendência resolvida",
};
const checkNames: Record<string, string> = {
  identity: "Identificação da migration",
  source: "Arquivo da revisão",
  history: "Histórico de migrations",
  receipt: "Recibo remoto",
  execution: "Registro da execução",
  schema: "Estrutura do banco",
};
const checkResults: Record<string, string> = {
  matched: "Correspondência confirmada",
  confirmed: "Confirmado",
  absent: "Não encontrado",
  durable_no_effect: "Ausência de efeito confirmada em registro durável",
  durable_confirmed: "Aplicação confirmada em registro durável",
  unconfirmed: "Sem confirmação durável",
  matches_expected: "Estrutura corresponde ao modelo esperado",
  matches_baseline: "Estrutura corresponde ao estado anterior",
  mismatch: "Estrutura divergente",
};
const resolutionReasons: Record<string, string> = {
  PROOF_INCOMPLETE:
    "As evidências disponíveis ainda não comprovam a ausência de efeitos. Consulte abaixo a prova que falta.",
  REMOTE_UNAVAILABLE:
    "A consulta ao provedor não foi concluída. Tente novamente após o intervalo informado.",
  PLAN_EXPIRED:
    "A validade da análise terminou. Uma nova conferência precisa verificar as evidências atuais.",
  EVIDENCE_CHANGED:
    "As evidências mudaram durante a análise. É necessário preparar uma nova conferência.",
  OPERATION_BUSY:
    "Outra execução está verificando esta tentativa. O acompanhamento será atualizado automaticamente.",
  UNSUPPORTED_EFFECTS:
    "Os comandos desta tentativa não pertencem ao conjunto que pode ser encerrado automaticamente com segurança.",
  DML_UNCERTAIN:
    "A tentativa pode ter alterado dados. A estrutura do banco, sozinha, não comprova ausência de efeitos.",
  SCHEMA_APPLIED:
    "A estrutura indica aplicação da migration. Esta ação não pode encerrar a tentativa como sem efeito.",
  CONFIGURATION_CHANGED:
    "A configuração mudou. A conexão precisa passar pela validação existente antes de uma nova recuperação.",
  ACCESS_DENIED:
    "Seu acesso atual não permite recuperar esta publicação. Confira a vigência e as permissões do seu acesso.",
};

export function PublicationRecoveryPanel({
  operation,
  projectId,
  refreshKey,
  canPublish,
  trackingUnavailable,
  onUpdated,
  onSnapshot,
}: {
  operation: Operation;
  projectId: string;
  refreshKey: number;
  canPublish: boolean;
  trackingUnavailable: boolean;
  onUpdated: () => void;
  onSnapshot?: (snapshot: RecoverySnapshot | null) => void;
}) {
  const recovery = usePublicationRecovery(
    operation,
    projectId,
    refreshKey,
    onUpdated,
  );
  const [exportStatus, setExportStatus] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceDetails = useRef<HTMLDetailsElement>(null);
  const evidenceContent = useRef<HTMLDivElement>(null);
  const { data, loading, loadError, actionError, sending } = recovery;
  const developer = !!data?.resolution;
  const resolved =
    data?.classification === "resolved" && data.recovery_state === "resolved";
  const processing = recoveryRequestActive(data?.request);
  const resolving = data?.request?.state === "resolving";
  const permitted = developer
    ? !!data.capabilities?.can_check_and_resolve
    : !!data?.can_check && canPublish;
  const cooldown =
    (developer ? data?.resolution?.blocked_code : data?.blocked_code) ===
      "RECOVERY_COOLDOWN" ||
    (developer && !permitted && !processing && !resolved)
      ? developer
        ? data?.resolution?.cooldown_until
        : data?.cooldown_until
      : null;
  const requestedAt = data?.request?.requested_at ?? data?.requested_at;
  const feedback = resolved
    ? developer
      ? "Pendência encerrada. Nenhuma migration foi reaplicada."
      : "A pendência foi resolvida. Confira a disponibilidade da publicação."
    : !recovery.feedback
      ? ""
      : data?.request?.state === "attention" ||
          (requestedAt &&
            data?.last_checked_at &&
            Date.parse(data.last_checked_at) >= Date.parse(requestedAt))
        ? "Conferência concluída. Consulte o resultado e o próximo passo acima."
        : recovery.feedback;
  const unavailable = loadError instanceof ApiError && loadError.status === 404;
  const canCheck =
    permitted &&
    data?.operation_state === "reconciling" &&
    operation.state === "reconciling" &&
    !trackingUnavailable &&
    !loading &&
    !loadError &&
    !sending &&
    !processing;
  const blockedReason =
    !developer && !canPublish
      ? "Seu acesso atual permite consultar o diagnóstico, mas não solicitar a conferência."
      : trackingUnavailable
        ? "Atualize o acompanhamento da publicação antes de solicitar a conferência."
        : developer
          ? data?.resolution?.blocked_reason
          : data?.blocked_reason;
  const missing =
    data?.resolution?.missing_evidence ?? data?.missing_evidence ?? [];
  const decisionReason = data?.resolution
    ? data.resolution.message || resolutionReasons[data.resolution.reason_code]
    : null;
  const busyLabel = resolving
    ? "Recuperando tentativa…"
    : "Verificando evidências…";
  const badge = resolved
    ? "Pendência resolvida"
    : processing
      ? busyLabel
      : developer
        ? data?.resolution?.eligibility === "ineligible"
          ? "Pendência mantida"
          : "Conferência necessária"
        : data
          ? classifications[data.classification]
          : null;
  useEffect(() => onSnapshot?.(data), [data, onSnapshot]);
  function showEvidence() {
    if (!evidenceDetails.current) return;
    evidenceDetails.current.open = true;
    setEvidenceOpen(true);
    evidenceContent.current?.focus({ preventScroll: true });
    evidenceContent.current?.scrollIntoView({ block: "nearest" });
  }
  function download() {
    if (!data) return;
    let url: string | null = null;
    try {
      const blob = new Blob([recoveryExport(data)], {
        type: "application/json;charset=utf-8",
      });
      url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `diagnostico-publicacao-${operation.id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80)}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setExportStatus("Diagnóstico preparado para download.");
    } catch {
      setExportStatus(
        "Não foi possível exportar o diagnóstico. Tente novamente.",
      );
    } finally {
      if (url) {
        const exportedUrl = url;
        setTimeout(() => URL.revokeObjectURL(exportedUrl), 1000);
      }
    }
  }
  return (
    <section
      className="panel publication-recovery"
      aria-labelledby="publication-recovery-title"
      aria-busy={sending || processing}
    >
      <div className="publication-recovery-heading">
        <h2 id="publication-recovery-title">
          <ShieldCheck size={22} aria-hidden="true" />
          Recuperar publicação
        </h2>
        {badge && (
          <span className="publication-recovery-state" data-resolved={resolved}>
            {badge}
          </span>
        )}
      </div>
      <p className="muted publication-recovery-intro">
        {resolved
          ? "A tentativa anterior foi encerrada como falha. Confira a prontidão da revisão antes de publicar novamente."
          : developer
            ? "O resultado da tentativa anterior ainda precisa de confirmação."
            : "Confira as evidências da tentativa atual. A conferência usa a mesma operação e não consome créditos de IA."}
      </p>
      {loading && (
        <p className="small muted" role="status">
          Consultando recuperação…
        </p>
      )}
      {unavailable ? (
        <p className="notice" role="status">
          A recuperação assistida ainda não está disponível para esta tentativa.
          O acompanhamento e os diagnósticos da publicação continuam disponíveis
          nesta página.
        </p>
      ) : (
        <ErrorNotice error={loadError} />
      )}
      {!!loadError && data && (
        <p className="small muted">
          Último diagnóstico preservado. Consulte novamente para obter o estado
          atual.
        </p>
      )}
      {data && (
        <>
          {developer && (
            <ol
              className="publication-recovery-progress"
              aria-label="Progresso da recuperação"
            >
              {[
                [
                  "Verificar evidências",
                  "Analisar o diagnóstico da tentativa anterior.",
                ],
                ["Recuperar tentativa", "Resolver a pendência com segurança."],
                [
                  "Liberar novo envio",
                  "Após a confirmação técnica, conferir a prontidão da revisão.",
                ],
              ].map(([title, description], index) => {
                const complete = resolved
                  ? index < 2
                  : resolving && index === 0;
                const current = resolved
                  ? index === 2
                  : resolving
                    ? index === 1
                    : index === 0;
                return (
                  <li
                    key={title}
                    data-state={
                      complete ? "complete" : current ? "current" : "waiting"
                    }
                    aria-current={current ? "step" : undefined}
                  >
                    <span
                      className="publication-recovery-progress-icon"
                      aria-hidden="true"
                    >
                      {complete ? <Check size={18} /> : index + 1}
                    </span>
                    <strong>{title}</strong>
                    <p>{description}</p>
                  </li>
                );
              })}
            </ol>
          )}
          {(!developer || data.resolution?.eligibility === "ineligible") && (
            <div className="publication-recovery-next">
              {!developer && <p>{data.summary}</p>}
              <h3>Próximo passo</h3>
              <p>
                {developer
                  ? decisionReason || data.summary
                  : data.required_action}
              </p>
              {developer && <p>{data.resolution?.next_action.label}</p>}
              {!developer && data.operator_required && (
                <p>
                  Exporte o diagnóstico para o responsável pela operação da
                  plataforma. A liberação depende da confirmação técnica do
                  resultado desta tentativa.
                </p>
              )}
              {missing.length > 0 && (
                <div>
                  <h3>Evidências ainda necessárias</h3>
                  <ul>
                    {missing.map((item, index) => (
                      <li key={`${index}:${item}`}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          {developer && !resolved && (
            <div className="publication-recovery-consent">
              <FileCheck2 size={24} aria-hidden="true" />
              <p>
                Ao continuar, verificamos as evidências e resolvemos a pendência
                quando for seguro. Nenhum comando de banco será repetido.
              </p>
            </div>
          )}
          <div className="publication-recovery-last-check">
            <span>
              Última conferência:{" "}
              {data.last_checked_at ? (
                <time dateTime={data.last_checked_at}>
                  {date(data.last_checked_at)}
                </time>
              ) : (
                "Ainda não concluída"
              )}
            </span>
            <button
              className="text-button"
              onClick={showEvidence}
              aria-expanded={evidenceOpen}
              aria-controls="publication-recovery-evidence"
            >
              Ver evidências
            </button>
          </div>
        </>
      )}
      <ErrorNotice error={actionError} />
      <div className="actions publication-recovery-actions">
        <button
          className="button primary"
          onClick={
            canCheck
              ? developer
                ? recovery.resolve
                : recovery.check
              : undefined
          }
          disabled={!canCheck}
          aria-describedby={
            blockedReason && !resolved
              ? "publication-recovery-blocked"
              : undefined
          }
        >
          {sending || processing ? (
            <LoaderCircle size={17} aria-hidden="true" />
          ) : (
            <RefreshCw size={17} aria-hidden="true" />
          )}
          {developer
            ? sending || processing
              ? busyLabel
              : "Conferir e resolver"
            : sending
              ? "Solicitando conferência…"
              : "Conferir pendência"}
        </button>
        <button className="button" onClick={download} disabled={!data}>
          <Download size={17} aria-hidden="true" />
          Exportar diagnóstico
        </button>
        <span className="small muted">Sem uso de créditos de IA.</span>
        {!!loadError && (
          <button
            className="text-button"
            onClick={recovery.refresh}
            disabled={loading || sending}
          >
            Consultar recuperação
          </button>
        )}
      </div>
      {blockedReason && !resolved && (
        <p className="small muted" id="publication-recovery-blocked">
          {blockedReason}
        </p>
      )}
      {cooldown && (
        <p className="small muted">
          Próxima solicitação após{" "}
          <time dateTime={cooldown}>{date(cooldown)}</time>.
        </p>
      )}
      <div
        className="publication-recovery-feedback"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {processing && (
          <p>
            {busyLabel} Você pode sair desta página; o processamento continua e
            será retomado no acompanhamento ao voltar.
          </p>
        )}
        {feedback && <p>{feedback}</p>}
        {exportStatus && <p>{exportStatus}</p>}
      </div>
      {data && (
        <details
          className="publication-recovery-details"
          ref={evidenceDetails}
          onToggle={(event) => setEvidenceOpen(event.currentTarget.open)}
        >
          <summary
            aria-expanded={evidenceOpen}
            aria-controls="publication-recovery-evidence"
          >
            Dados do diagnóstico
          </summary>
          <div
            id="publication-recovery-evidence"
            ref={evidenceContent}
            tabIndex={-1}
          >
            <dl className="publication-facts">
              <div>
                <dt>Tentativa</dt>
                <dd>
                  <code>{data.operation_id}</code>
                </dd>
              </div>
              <div>
                <dt>Revisão desta tentativa</dt>
                <dd>
                  <code>{data.revision_id ?? "Não informada"}</code>
                </dd>
              </div>
              <div>
                <dt>Classificação técnica</dt>
                <dd>{classifications[data.classification]}</dd>
              </div>
              {data.resolution && (
                <div>
                  <dt>Decisão da recuperação</dt>
                  <dd>
                    <code>{data.resolution.reason_code}</code>
                  </dd>
                </div>
              )}
              {data.request && (
                <div>
                  <dt>Solicitação de recuperação</dt>
                  <dd>
                    <code>{data.request.id}</code>
                  </dd>
                </div>
              )}
              {data.migration && (
                <>
                  <div>
                    <dt>Migration em conferência</dt>
                    <dd>
                      <code>{data.migration.name}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Checksum SHA-256</dt>
                    <dd>
                      <code>{data.migration.sha256}</code>
                    </dd>
                  </div>
                </>
              )}
              {Object.entries(data.checks).map(([key, value]) => (
                <div key={key}>
                  <dt>{checkNames[key] ?? key}</dt>
                  <dd>{checkResults[value] ?? value}</dd>
                </div>
              ))}
            </dl>
            <dl className="publication-recovery-times">
              {requestedAt && (
                <div>
                  <dt>Conferência solicitada</dt>
                  <dd>
                    <time dateTime={requestedAt}>{date(requestedAt)}</time>
                  </dd>
                </div>
              )}
              {data.last_checked_at && (
                <div>
                  <dt>Última conferência concluída</dt>
                  <dd>
                    <time dateTime={data.last_checked_at}>
                      {date(data.last_checked_at)}
                    </time>
                  </dd>
                </div>
              )}
              {data.request?.completed_at && (
                <div>
                  <dt>Solicitação concluída</dt>
                  <dd>
                    <time dateTime={data.request.completed_at}>
                      {date(data.request.completed_at)}
                    </time>
                  </dd>
                </div>
              )}
            </dl>
            <p className="small muted">
              O arquivo reúne a identificação da tentativa, o diagnóstico e as
              evidências disponíveis para a análise operacional.
            </p>
          </div>
        </details>
      )}
    </section>
  );
}
