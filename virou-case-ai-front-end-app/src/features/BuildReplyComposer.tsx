import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { LoaderCircle, Send } from "lucide-react";
import { useSession } from "../app/session";
import { command } from "../shared/api";
import { ErrorNotice } from "../shared/ui";
import type { Operation } from "../shared/types";
import { uncertainRetry } from "./TemplateFillRetryDialog";

type ReplyIntent = {
  message_text: string;
  base_project_revision: string | null;
  policy_version: string;
};

function savedReply(key: string) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || "null");
    if (!saved || typeof saved.text !== "string") return null;
    const intent = saved.intent;
    return {
      text: saved.text,
      intent:
        intent &&
        intent.message_text === saved.text.trim() &&
        (intent.base_project_revision === null ||
          typeof intent.base_project_revision === "string") &&
        typeof intent.policy_version === "string"
          ? {
              message_text: intent.message_text as string,
              base_project_revision: intent.base_project_revision as
                string | null,
              policy_version: intent.policy_version as string,
            }
          : null,
    };
  } catch {
    return null;
  }
}

export function BuildReplyComposer({
  pid,
  operationId,
  initialText,
  policyVersion,
  limit,
  blockedReason,
  focusRequest,
  beforeSend,
  onAccepted,
  onBusyChange,
}: {
  pid: string;
  operationId: string;
  initialText: string;
  policyVersion: string;
  limit: number;
  blockedReason: string;
  focusRequest: number;
  beforeSend: () => Promise<string | null>;
  onAccepted: (operation: Operation) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const id = useId();
  const { user } = useSession();
  const storageKey = `vira-intent:reply-draft:${user?.id || ""}:${pid}:${operationId}`;
  const [saved] = useState(() => savedReply(storageKey));
  const [text, setText] = useState(saved?.text || initialText);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(!!saved?.intent);
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const errorNotice = useRef<HTMLDivElement>(null);
  const pending = useRef(false);
  const intent = useRef<ReplyIntent | null>(saved?.intent || null);
  const alive = useRef(true);
  const overLimit = text.length > limit;

  function preserve(nextText: string, nextIntent: ReplyIntent | null) {
    try {
      if (nextText || nextIntent)
        sessionStorage.setItem(
          storageKey,
          JSON.stringify({ text: nextText, intent: nextIntent }),
        );
      else sessionStorage.removeItem(storageKey);
      if (alive.current) setStorageUnavailable(false);
    } catch {
      if (alive.current) setStorageUnavailable(true);
    }
  }

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (focusRequest <= 0 || !form.current?.getClientRects().length) return;
    input.current?.focus({ preventScroll: true });
    form.current.scrollIntoView({
      block: "end",
      inline: "nearest",
      behavior: "auto",
    });
  }, [focusRequest]);
  useEffect(() => {
    if (initialText && !text && !intent.current) {
      setText(initialText);
      preserve(initialText, null);
    }
  }, [initialText, focusRequest]);
  useEffect(() => {
    if (error && !busy) errorNotice.current?.focus({ preventScroll: true });
  }, [error, busy]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending.current || blockedReason || overLimit || !text.trim()) return;
    pending.current = true;
    onBusyChange(true);
    setBusy(true);
    setError(null);
    try {
      if (!intent.current)
        intent.current = {
          message_text: text.trim(),
          base_project_revision: await beforeSend(),
          policy_version: policyVersion,
        };
      preserve(text, intent.current);
      const operation = await command<Operation>(
        `/projects/${pid}/messages/${operationId}/reply`,
        intent.current,
      );
      intent.current = null;
      preserve("", null);
      if (!alive.current) return;
      setText("");
      setUncertain(false);
      onAccepted(operation);
    } catch (cause) {
      const pendingConfirmation = uncertainRetry(cause) && !!intent.current;
      if (!pendingConfirmation) intent.current = null;
      preserve(text, intent.current);
      if (alive.current) {
        setError(cause);
        setUncertain(pendingConfirmation);
      }
    } finally {
      pending.current = false;
      onBusyChange(false);
      if (alive.current) setBusy(false);
    }
  }

  return (
    <form
      className="build-reply-composer"
      ref={form}
      onSubmit={(event) => void submit(event)}
    >
      <label className="guided-label" htmlFor={id}>
        Sua resposta ao agente
      </label>
      <p id={`${id}-context`} className="small muted">
        Continue este pedido com o contexto e os anexos já enviados.
      </p>
      <textarea
        id={id}
        ref={input}
        rows={3}
        value={text}
        placeholder="Confirme sua escolha ou explique o detalhe solicitado."
        disabled={busy || !!blockedReason}
        readOnly={uncertain}
        aria-invalid={overLimit}
        aria-describedby={`${id}-context ${id}-counter`}
        onChange={(event) => {
          setText(event.target.value);
          setError(null);
          preserve(event.target.value, null);
        }}
      />
      <p id={`${id}-counter`} className="guided-counter">
        {text.length}/{limit}
      </p>
      {overLimit && (
        <p className="notice error" role="alert">
          A resposta excede o limite indicado. Reduza o texto para continuar.
        </p>
      )}
      {!!error && (
        <div ref={errorNotice} tabIndex={-1} className="build-reply-error">
          <ErrorNotice error={error} />
        </div>
      )}
      {uncertain && (
        <p className="notice" role="status">
          O envio anterior aguarda confirmação. Verifique a mesma tentativa; sua
          resposta foi preservada.
        </p>
      )}
      {storageUnavailable && (
        <p className="small" role="status">
          Sua resposta continua nesta tela. O navegador não permitiu salvar o
          rascunho para recuperar após recarregar.
        </p>
      )}
      {blockedReason && (
        <p className="notice" role="status">
          {blockedReason}
        </p>
      )}
      <button
        type="submit"
        className="button primary full"
        disabled={busy || !!blockedReason || overLimit || !text.trim()}
      >
        {busy ? (
          <LoaderCircle
            size={17}
            className="build-spinner"
            aria-hidden="true"
          />
        ) : (
          <Send size={17} aria-hidden="true" />
        )}
        {busy
          ? "Enviando resposta…"
          : uncertain
            ? "Verificar envio"
            : "Enviar resposta"}
      </button>
    </form>
  );
}
