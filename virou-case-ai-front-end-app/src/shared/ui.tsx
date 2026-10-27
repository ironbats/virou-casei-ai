import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertCircle, ArrowUpRight, Check, X } from "lucide-react";
import { Link } from "react-router-dom";

export function Brand() {
  return (
    <Link className="brand" to="/" aria-label="viroucaseai — início">
      <span className="brand-mark" aria-hidden="true">
        v
      </span>
      viroucaseai
    </Link>
  );
}
export function ErrorNotice({ error }: { error: unknown }) {
  return error ? (
    <div className="notice error" role="alert">
      <AlertCircle size={20} />
      <span>{error instanceof Error ? error.message : String(error)}</span>
    </div>
  ) : null;
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <span className="spinner" /> Carregando…
    </div>
  );
}
export function Modal({
  title,
  children,
  close,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    el?.showModal();
    return () => el?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      aria-label={title}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button className="icon-button" onClick={close} aria-label="Fechar">
          <X size={22} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function WhatsApp({
  url = "https://wa.me/5511941810735",
  children = "Falar no WhatsApp",
}: {
  url?: string;
  children?: ReactNode;
}) {
  return (
    <a
      className="button primary"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
      <ArrowUpRight size={18} />
    </a>
  );
}
export function CopyButton({ text }: { text: string }) {
  const [status, setStatus] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setStatus("Copiado");
    } catch {
      setStatus("Não foi possível copiar. Selecione o texto abaixo.");
    }
  }
  return (
    <>
      <button className="button" onClick={copy}>
        {status === "Copiado" ? <Check size={16} /> : null}Copiar template
      </button>
      <span className="sr-only" role="status">
        {status}
      </span>
    </>
  );
}
export function useLoad<T>(loader: () => Promise<T>, keys: unknown[] = []) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState<unknown>(null),
    [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setError(null);
    loader()
      .then((x) => {
        if (alive) setData(x);
      })
      .catch((e) => {
        if (alive) setError(e);
      });
    return () => {
      alive = false;
    };
  }, [...keys, tick]);
  return { data, error, reload: () => setTick((t) => t + 1), setData };
}
