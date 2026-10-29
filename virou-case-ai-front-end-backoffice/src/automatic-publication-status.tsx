import { date } from "./api";

export type AutomaticVerification = {
  enabled: boolean;
  state:
    | "disabled"
    | "waiting_configuration"
    | "waiting_executor"
    | "waiting_lifecycle"
    | "scheduled"
    | "queued"
    | "running"
    | "retry_wait"
    | "attention";
  message: string;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  last_succeeded_at: string | null;
  last_error_code: string | null;
  consecutive_failures: number;
  operation_id?: string | null;
};

const labels: Record<AutomaticVerification["state"], string> = {
  disabled: "Verificação automática desativada",
  waiting_configuration: "Verificação automática aguarda a configuração",
  waiting_executor: "Verificação automática aguarda o executor",
  waiting_lifecycle: "Verificação automática aguarda a liberação operacional",
  scheduled: "Verificação automática programada",
  queued: "Verificação automática na fila",
  running: "Verificação automática em andamento",
  retry_wait: "Nova tentativa automática programada",
  attention: "Verificação automática precisa de atenção",
};

export function AutomaticPublicationStatus({
  verification,
  canPublish,
}: {
  verification?: AutomaticVerification;
  canPublish: boolean;
}) {
  if (!verification) return null;
  const pending = ["queued", "running"].includes(verification.state);
  return (
    <section
      className="cloudflare-automatic-verification"
      aria-label="Renovação automática da publicação"
    >
      <h3>{labels[verification.state] || "Verificação automática"}</h3>
      <p role="status">{verification.message}</p>
      {pending && canPublish && (
        <p className="field-help">
          A publicação continua liberada pela verificação anterior enquanto ela
          permanecer válida.
        </p>
      )}
      {(verification.last_succeeded_at || verification.next_attempt_at) && (
        <dl className="integration-summary">
          {verification.last_succeeded_at && (
            <div>
              <dt>Última verificação aprovada</dt>
              <dd>{date(verification.last_succeeded_at)}</dd>
            </div>
          )}
          {verification.next_attempt_at && (
            <div>
              <dt>
                {verification.state === "retry_wait"
                  ? "Próxima tentativa"
                  : "Próxima verificação"}
              </dt>
              <dd>{date(verification.next_attempt_at)}</dd>
            </div>
          )}
        </dl>
      )}
      {verification.last_error_code && (
        <p className="field-help">
          Última falha: <code>{verification.last_error_code}</code>
          {verification.consecutive_failures > 0 && (
            <> · Tentativas sem sucesso: {verification.consecutive_failures}</>
          )}
        </p>
      )}
    </section>
  );
}
