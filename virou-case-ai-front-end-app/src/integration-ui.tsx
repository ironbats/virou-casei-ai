import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { X } from "lucide-react";
import { api } from "./api";

export function useRemote<T>(path: string, revision = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api<T>(path, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path, revision, reload]);
  return { data, error, loading, refresh: () => setReload((v) => v + 1) };
}

export function IntegrationDialog({
  title,
  children,
  close,
  dirty = false,
  busy = false,
  className = "",
  focusClose = false,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  dirty?: boolean;
  busy?: boolean;
  className?: string;
  focusClose?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const discard = useRef<HTMLDivElement>(null);
  const [confirm, setConfirm] = useState(false);
  const requestClose = () => {
    if (!busy) dirty ? setConfirm(true) : close();
  };
  useLayoutEffect(() => {
    const previous = document.activeElement;
    const dialog = ref.current;
    dialog?.showModal();
    dialog
      ?.querySelector<HTMLElement>(
        focusClose
          ? 'button[aria-label="Fechar"]'
          : 'input:not([type="hidden"]):not(:disabled), select:not(:disabled), textarea:not(:disabled)',
      )
      ?.focus();
    return () => {
      dialog?.close();
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);
  useEffect(() => {
    if (confirm) discard.current?.focus();
  }, [confirm]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  return (
    <dialog
      ref={ref}
      className={`integration-dialog ${className}`}
      aria-label={title}
      onKeyDown={(event) => {
        if (!focusClose || event.key !== "Tab") return;
        const focusable = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex]',
          ),
        ).filter(
          (element) =>
            element.tabIndex >= 0 &&
            element.getClientRects().length > 0 &&
            !element.closest("[hidden]") &&
            getComputedStyle(element).visibility !== "hidden",
        );
        if (!focusable.length) {
          event.preventDefault();
          event.currentTarget.focus();
          return;
        }
        const active = document.activeElement;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (
          event.shiftKey &&
          (active === first || !focusable.includes(active as HTMLElement))
        ) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }}
      onCancel={(e) => {
        e.preventDefault();
        requestClose();
      }}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button
          className="icon-button"
          aria-label="Fechar"
          disabled={busy}
          onClick={requestClose}
        >
          <X />
        </button>
      </div>
      {confirm ? (
        <div className="notice" ref={discard} tabIndex={-1}>
          <strong>Descartar alterações não salvas?</strong>
          <p>Os dados preenchidos neste formulário serão perdidos.</p>
          <div className="actions">
            <button
              className="button primary"
              onClick={() => setConfirm(false)}
            >
              Continuar editando
            </button>
            <button className="button" onClick={close}>
              Descartar e fechar
            </button>
          </div>
        </div>
      ) : null}
      <div hidden={confirm}>{children}</div>
    </dialog>
  );
}

export function Pagination({
  total,
  page,
  setPage,
}: {
  total: number;
  page: number;
  setPage: (n: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / 10));
  return (
    <div className="integration-pagination">
      <span role="status">
        {total} registro{total === 1 ? "" : "s"} · Página {page + 1} de {pages}
      </span>
      <div className="actions">
        <button
          className="button"
          disabled={!page}
          onClick={() => setPage(page - 1)}
        >
          Anterior
        </button>
        <button
          className="button"
          disabled={page + 1 >= pages}
          onClick={() => setPage(page + 1)}
        >
          Próxima
        </button>
      </div>
    </div>
  );
}
