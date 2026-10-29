import { useEffect, useRef, useState } from "react";
import { command, date } from "./api";
import { Dialog } from "./admin-ui";
import type { Operation } from "./admin-types";

export function PublicationOperationStatus({
  operation,
}: {
  operation: Operation;
}) {
  const recovery = operation.result_ref?.publication_recovery;
  const failure = operation.result_ref?.publication_failure;
  const migration =
    failure?.migration?.format_version === 1 &&
    failure.migration.category === "migration" &&
    Array.isArray(failure.migration.issues)
      ? failure.migration
      : null;
  const labels: Record<string, string> = {
    queued: "Na fila",
    running: "Em andamento",
    reconciling:
      recovery?.state === "attention" ? "Aguardando revisão" : "Em conferência",
    succeeded: "Concluída",
    failed: "Não concluída",
    cancelled: "Cancelada",
  };
  const message =
    operation.state === "failed"
      ? failure?.message || operation.progress_message
      : recovery?.message || operation.progress_message;
  return (
    <>
      <span>{labels[operation.state] ?? operation.state}</span>
      {message && <small>{message}</small>}
      {operation.state === "reconciling" &&
        recovery?.state !== "attention" &&
        recovery?.next_attempt_at && (
          <small>
            Próxima conferência automática: {date(recovery.next_attempt_at)}
          </small>
        )}
      {operation.error_code && (
        <small>
          <code>{operation.error_code}</code>
        </small>
      )}
      {migration && (
        <details>
          <summary>Diagnóstico das migrations</summary>
          <ul>
            {migration.issues.map((issue, index) => (
              <li key={`${issue.code}:${issue.path || ""}:${index}`}>
                <strong>{issue.path || "Arquivo não identificado"}</strong>
                <small>{issue.message}</small>
                <small>{issue.hint}</small>
              </li>
            ))}
          </ul>
          {migration.issue_count > migration.issues.length && (
            <small>
              Exibindo {migration.issues.length} de {migration.issue_count}{" "}
              problemas.
            </small>
          )}
        </details>
      )}
    </>
  );
}

export function PublicationReconcileButton({
  operation,
  onDone,
}: {
  operation: Operation;
  onDone: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const requesting = useRef(false);
  const recovery = operation.result_ref?.publication_recovery;
  const automatic =
    recovery && ["scheduled", "running", "retry_wait"].includes(recovery.state);
  const unavailable =
    operation.state !== "reconciling" || recovery?.state === "running";
  useEffect(() => {
    if (operation.state !== "reconciling") setOpen(false);
  }, [operation.state]);
  return (
    <>
      <button
        className="button"
        disabled={unavailable}
        onClick={() => setOpen(true)}
      >
        {automatic ? "Conferir agora" : "Reconciliar publicação"}
      </button>
      {open && (
        <Dialog
          title="Conferir publicação remota"
          close={() => {
            if (!busy) setOpen(false);
          }}
        >
          {automatic && (
            <p>
              A conferência automática acompanha esta operação. Esta ação
              permite antecipar uma consulta sem criar outra publicação.
            </p>
          )}
          <p>
            O servidor consultará Worker, binding, banco e recibos de
            migrations. Somente um resultado comprovado poderá concluir a
            publicação ou recolocar o job na fila. Migrations sem recibo
            permanecem bloqueadas para inspeção.
          </p>
          <label>
            Motivo
            <textarea
              value={reason}
              minLength={8}
              maxLength={300}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {error && (
            <p role="alert" className="notice error">
              {error}
            </p>
          )}
          <div className="actions">
            <button
              className="button"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              Cancelar
            </button>
            <button
              className="button primary"
              disabled={busy || unavailable || reason.trim().length < 8}
              onClick={async () => {
                if (
                  requesting.current ||
                  unavailable ||
                  reason.trim().length < 8
                )
                  return;
                requesting.current = true;
                setBusy(true);
                setError("");
                try {
                  await command(`/publications/${operation.id}/reconcile`, {
                    reason,
                  });
                  await onDone();
                  setOpen(false);
                } catch (e) {
                  setError(e instanceof Error ? e.message : String(e));
                } finally {
                  requesting.current = false;
                  setBusy(false);
                }
              }}
            >
              Consultar e reconciliar
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}
