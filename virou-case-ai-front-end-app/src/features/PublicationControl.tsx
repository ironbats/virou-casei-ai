import { useEffect, useState } from "react";
import { Upload, ExternalLink, Copy } from "lucide-react";
import { api, command, date } from "../shared/api";
import { ErrorNotice, Modal } from "../shared/ui";
import type { Operation } from "../shared/types";

type Readiness = {
  can_publish: boolean;
  revision_id: string;
  configuration_version: number | null;
  reasons: { code: string; message: string }[];
  publication: {
    url?: string;
    revision_id?: string;
    published_at?: string;
  } | null;
};
export function PublicationControl({
  pid,
  revision,
  dirty,
  busy,
  operations,
  onAccepted,
}: {
  pid: string;
  revision: string | null;
  dirty: boolean;
  busy: boolean;
  operations: Operation[];
  onAccepted: (op: Operation) => void;
}) {
  const [readiness, setReadiness] = useState<Readiness | null>(null),
    [error, setError] = useState<unknown>(null),
    [refresh, setRefresh] = useState(0);
  const [open, setOpen] = useState(false),
    [sending, setSending] = useState(false),
    [copied, setCopied] = useState(false);
  const operation = operations.find((o) => o.type === "publication");
  useEffect(() => {
    const controller = new AbortController();
    setReadiness(null);
    setError(null);
    let timer: number | undefined;
    const load = async () => {
      try {
        const result = await api<Readiness>(
          `/projects/${pid}/publication-readiness${revision ? `?revision_id=${encodeURIComponent(revision)}` : ""}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) { setReadiness(result); setError(null); }
      } catch (e) {
        if (!controller.signal.aborted) setError(e);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(load, 15000);
      }
    };
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [pid, revision, refresh, operation?.state]);
  const reason = dirty
    ? "Salve as alterações em uma revisão antes de publicar."
    : readiness?.reasons.map((r) => r.message).join(" ");
  async function publish() {
    if (!readiness?.can_publish || dirty || sending) return;
    setSending(true);
    setError(null);
    try {
      onAccepted(
        await command<Operation>(`/projects/${pid}/publications`, {
          revision_id: revision,
        }),
      );
      setOpen(false);
      setRefresh((n) => n + 1);
    } catch (e) {
      setError(e);
    } finally {
      setSending(false);
    }
  }
  const url =
    operation?.state === "succeeded"
      ? operation.result_ref.url
      : readiness?.publication?.url;
  return (
    <div className="publication-control">
      <button
        className="button primary"
        disabled={busy || sending || dirty || !readiness?.can_publish}
        aria-describedby="publication-reason"
        onClick={() => setOpen(true)}
      >
        <Upload size={16} />
        Publicar
      </button>
      <div id="publication-reason" className="publication-reason" role="status">
        {error
          ? "Não foi possível verificar a publicação."
          : !readiness
            ? "Verificando publicação…"
            : reason}
      </div>
      {(error || reason) && (
        <button
          className="text-button small"
          disabled={sending}
          onClick={() => setRefresh((n) => n + 1)}
        >
          Verificar novamente
        </button>
      )}
      {operation && (
        <p className="small">
          {operation.progress_message || operation.state}
          {operation.error_code ? ` · ${operation.error_code}` : ""}
        </p>
      )}
      {url && (
        <div className="actions">
          <a
            className="text-button"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            <ExternalLink size={14} />
            Abrir publicação
          </a>
          <button
            className="text-button"
            onClick={() =>
              void navigator.clipboard
                .writeText(url)
                .then(() => setCopied(true))
                .catch(setError)
            }
          >
            <Copy size={14} />
            {copied ? "Copiado" : "Copiar link"}
          </button>
          <span className="small">
            Revisão{" "}
            {(
              operation?.result_ref.revision_id ||
              readiness?.publication?.revision_id
            )?.slice(0, 8)}{" "}
            ·{" "}
            {date(
              operation?.result_ref.verified_at ||
                readiness?.publication?.published_at,
            )}
          </span>
        </div>
      )}
      <ErrorNotice error={error} />
      {open && (
        <Modal title="Publicar revisão salva" close={() => setOpen(false)}>
          <p>
            A revisão {revision?.slice(0, 8)} será enviada à hospedagem
            configurada pela plataforma.
          </p>
          <p>
            O link será exibido após a verificação externa de frontend, API e
            banco.
          </p>
          <ErrorNotice error={error} />
          <button
            className="button primary"
            disabled={sending}
            onClick={() => void publish()}
          >
            {sending ? "Enviando…" : "Publicar esta revisão"}
          </button>
        </Modal>
      )}
    </div>
  );
}
