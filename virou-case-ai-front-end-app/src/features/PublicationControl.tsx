import { useEffect, useRef, useState } from "react";
import { ArrowRight, Upload } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { ErrorNotice } from "../shared/ui";
import type { Operation } from "../shared/types";
import { inProgress } from "./useProjectOperations";
import { usePublicationReadiness } from "./usePublication";

export function PublicationControl({
  pid,
  revision,
  dirty,
  busy,
  operations,
  beforeNavigate,
}: {
  pid: string;
  revision: string | null;
  dirty: boolean;
  busy: boolean;
  operations: Operation[];
  beforeNavigate: () => Promise<void>;
}) {
  const navigate = useNavigate();
  const operation = operations.find((item) => item.type === "publication");
  const readiness = usePublicationReadiness(pid, revision, operation?.state);
  const [error, setError] = useState<unknown>(null);
  const [leaving, setLeaving] = useState(false);
  const navigating = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const active = !!operation && inProgress(operation);
  const reason = dirty
    ? "Salve as alterações em uma revisão antes de publicar."
    : readiness.error
      ? "Não foi possível verificar a publicação."
      : readiness.loading
        ? "Verificando publicação…"
        : readiness.data?.reasons.map((item) => item.message).join(" ");
  async function openPublication() {
    if (navigating.current) return;
    navigating.current = true;
    setLeaving(true);
    setError(null);
    try {
      await beforeNavigate();
      if (!mounted.current) return;
      navigate(`/app/projetos/${encodeURIComponent(pid)}/publicar`);
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      navigating.current = false;
      if (mounted.current) setLeaving(false);
    }
  }
  return (
    <div className="publication-control">
      <button
        className="button primary"
        disabled={
          busy ||
          leaving ||
          dirty ||
          active ||
          !!readiness.error ||
          !readiness.data?.can_publish
        }
        aria-describedby="publication-reason"
        onClick={() => void openPublication()}
      >
        <Upload size={16} />
        {leaving ? "Abrindo publicação…" : "Publicar"}
      </button>
      {(operation ||
        readiness.data?.publication?.url ||
        (!!reason && !readiness.loading)) && (
        <button
          className="text-button small"
          disabled={busy || leaving}
          onClick={() => void openPublication()}
        >
          {operation || readiness.data?.publication?.url
            ? "Acompanhar publicação"
            : "Ver detalhes da publicação"}{" "}
          <ArrowRight size={14} />
        </button>
      )}
      <div id="publication-reason" className="publication-reason" role="status">
        {reason}
      </div>
      {(!!readiness.error || (!!reason && !readiness.loading)) && (
        <button className="text-button small" onClick={readiness.refresh}>
          Verificar novamente
        </button>
      )}
      <ErrorNotice error={error} />
    </div>
  );
}
