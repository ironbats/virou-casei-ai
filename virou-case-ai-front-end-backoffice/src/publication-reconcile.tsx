import { useState } from "react";
import { command } from "./api";
import { Dialog } from "./admin-ui";

export function PublicationReconcileButton({
  operation,
  onDone,
}: {
  operation: { id: string; state: string };
  onDone: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <>
      <button
        className="button"
        disabled={operation.state !== "reconciling"}
        onClick={() => setOpen(true)}
      >
        Reconciliar publicação
      </button>
      {open && (
        <Dialog
          title="Conferir publicação remota"
          close={() => {
            if (!busy) setOpen(false);
          }}
        >
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
              disabled={busy || reason.trim().length < 8}
              onClick={async () => {
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
