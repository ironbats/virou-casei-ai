import { useEffect, useId, useRef, useState } from "react";
import { Info, LoaderCircle, RotateCw } from "lucide-react";
import { ApiError } from "../shared/api";
import { ErrorNotice, Modal } from "../shared/ui";

export function uncertainRetry(error: unknown) {
  if (error instanceof ApiError)
    return error.status >= 500 || error.status === 408;
  return (
    !(error instanceof Error) ||
    error instanceof TypeError ||
    error instanceof DOMException
  );
}

export function TemplateFillRetryDialog({
  title,
  blockedReason,
  initialError,
  confirm,
  close,
}: {
  title: string;
  blockedReason: string;
  initialError?: unknown;
  confirm: () => Promise<void>;
  close: () => void;
}) {
  const descriptionId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(initialError || null);
  const submitting = useRef(false);
  const alive = useRef(true);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const errorNotice = useRef<HTMLDivElement>(null);
  const uncertain = !!error && uncertainRetry(error);
  const requestId =
    error instanceof ApiError && /^[\w.-]{1,80}$/.test(error.requestId)
      ? error.requestId
      : "";

  useEffect(() => {
    alive.current = true;
    const dialog = cancelButton.current?.closest("dialog");
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !dialog) return;
      const controls = Array.from(
        dialog.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
      );
      const first = controls[0];
      const last = controls.at(-1);
      if (!first) {
        event.preventDefault();
        dialog.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog?.addEventListener("keydown", trapFocus);
    cancelButton.current?.focus();
    return () => {
      alive.current = false;
      dialog?.removeEventListener("keydown", trapFocus);
    };
  }, []);
  useEffect(() => {
    if (error && !busy) errorNotice.current?.focus();
  }, [error, busy]);

  async function submit() {
    if (submitting.current || blockedReason) return;
    submitting.current = true;
    setBusy(true);
    try {
      await confirm();
    } catch (cause) {
      if (alive.current) setError(cause);
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  }

  return (
    <Modal
      title="Tentar preencher novamente?"
      className="template-retry-dialog"
      describedBy={descriptionId}
      closeDisabled={busy}
      close={() => {
        if (!submitting.current) close();
      }}
    >
      <div className="template-retry-body">
        <p id={descriptionId}>
          O template <strong>{title}</strong> será preenchido novamente com o
          contexto salvo deste pedido.
        </p>
        <div className="template-retry-consumption">
          <Info size={20} aria-hidden="true" />
          <div>
            <p>Uma nova tentativa pode consumir seu uso de IA.</p>
            <p>Suas edições manuais serão preservadas.</p>
          </div>
        </div>
        {!!error && (
          <div ref={errorNotice} tabIndex={-1} className="template-retry-error">
            <ErrorNotice
              error={
                error instanceof ApiError || !uncertain
                  ? error
                  : "Não foi possível confirmar o resultado da tentativa. Verifique sua conexão e tente consultar novamente."
              }
            />
            {uncertain && (
              <p className="small">
                A resposta não chegou. Verifique a mesma tentativa para evitar
                uma solicitação duplicada.
              </p>
            )}
            {requestId && (
              <p className="small muted">
                Referência para suporte: {requestId}
              </p>
            )}
          </div>
        )}
        {blockedReason && (
          <p className="notice" role="status">
            {blockedReason}
          </p>
        )}
        {busy && (
          <p className="template-retry-progress" role="status">
            <LoaderCircle
              size={18}
              className="preparation-spinner"
              aria-hidden="true"
            />
            {uncertain
              ? "Verificando a tentativa anterior…"
              : "Solicitando nova tentativa…"}
          </p>
        )}
      </div>
      <div className="actions">
        <button
          type="button"
          className="button"
          ref={cancelButton}
          disabled={busy}
          onClick={close}
        >
          Cancelar
        </button>
        <button
          type="button"
          className="button primary"
          disabled={busy || !!blockedReason}
          onClick={() => void submit()}
        >
          <RotateCw size={16} aria-hidden="true" />
          {busy
            ? "Aguarde…"
            : uncertain
              ? "Verificar tentativa"
              : "Confirmar nova tentativa"}
        </button>
      </div>
    </Modal>
  );
}
