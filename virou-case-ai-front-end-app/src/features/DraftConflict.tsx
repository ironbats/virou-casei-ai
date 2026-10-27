import { Modal } from "../shared/ui";
import { useState } from "react";
export function DraftConflict({
  local,
  remote,
  onResolve,
}: {
  local: unknown;
  remote: unknown;
  onResolve: (remote: boolean) => void;
}) {
  const [open, setOpen] = useState(true);
  if (!open)
    return (
      <button className="button" onClick={() => setOpen(true)}>
        Comparar conflito do rascunho
      </button>
    );
  return (
    <Modal
      title="O rascunho mudou em outra sessão"
      close={() => setOpen(false)}
    >
      <p>
        Compare os conteúdos. Reaplicar substitui o rascunho remoto pelo
        conteúdo desta tela; a revisão salva não muda.
      </p>
      <div className="compare-grid">
        <div>
          <h3>Nesta tela</h3>
          <pre className="source-preview">{JSON.stringify(local, null, 2)}</pre>
        </div>
        <div>
          <h3>Salvo no servidor</h3>
          <pre className="source-preview">
            {JSON.stringify(remote, null, 2)}
          </pre>
        </div>
      </div>
      <div className="actions">
        <button className="button" onClick={() => onResolve(true)}>
          Usar rascunho do servidor
        </button>
        <button className="button primary" onClick={() => onResolve(false)}>
          Reaplicar meu rascunho
        </button>
      </div>
    </Modal>
  );
}
