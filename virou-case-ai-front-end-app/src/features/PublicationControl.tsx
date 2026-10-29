import { useEffect, useRef, useState } from "react";
import { ArrowRight, Upload, Info } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { ErrorNotice } from "../shared/ui";
import type { Operation } from "../shared/types";
import { WorkspaceMenu } from "./WorkspaceMenu";
import { inProgress } from "./useProjectOperations";
import { publicationBlockedReason } from "./publicationDiagnostic";
import {
  automaticVerificationMessage,
  usePublicationReadiness,
} from "./usePublication";

export function PublicationControl({
  pid,
  revision,
  dirty,
  busy,
  operations,
  beforeNavigate,
  compact = false,
}: {
  pid: string;
  revision: string | null;
  dirty: boolean;
  busy: boolean;
  operations: Operation[];
  beforeNavigate: () => Promise<void>;
  compact?: boolean;
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
  const automaticMessage = automaticVerificationMessage(readiness.data);
  const reason = dirty
    ? "Salve as alterações em uma revisão antes de publicar."
    : readiness.error
      ? "Não foi possível verificar a publicação."
      : readiness.loading
        ? "Verificando publicação…"
        : active && operation
          ? publicationBlockedReason(operation)
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
  const details = (
    <>
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
        {operation?.state === "reconciling" &&
          operation.result_ref.publication_recovery?.message && (
            <p>{operation.result_ref.publication_recovery.message}</p>
          )}
        {automaticMessage && <p>{automaticMessage}</p>}
      </div>
      {(!!readiness.error ||
        (!!reason &&
          !readiness.loading &&
          !active &&
          !readiness.data?.automatic_verification?.enabled)) && (
        <button className="text-button small" onClick={readiness.refresh}>
          Verificar novamente
        </button>
      )}
      <ErrorNotice error={error} />
    </>
  );
  return (
    <div className={"publication-control" + (compact ? " is-compact" : "")}>
      <button
        className="button primary"
        disabled={
          busy ||
          leaving ||
          dirty ||
          active ||
          !!readiness.error ||
          readiness.loading ||
          readiness.data?.revision_id !== revision ||
          !readiness.data?.can_publish
        }
        aria-describedby={
          compact ? "publication-compact-reason" : "publication-reason"
        }
        onClick={() => void openPublication()}
      >
        <Upload size={16} />
        {leaving ? "Abrindo publicação…" : "Publicar"}
      </button>
      {compact && (
        <span
          id="publication-compact-reason"
          className="publication-compact-reason"
        >
          {dirty
            ? "Salve a revisão"
            : active
              ? operation?.state === "reconciling"
                ? "Resultado pendente"
                : "Em andamento"
              : reason && !readiness.loading
                ? "Conferir detalhes"
                : ""}
        </span>
      )}
      {compact ? (
        <WorkspaceMenu
          label="Detalhes da publicação"
          icon={<Info size={17} aria-hidden="true" />}
        >
          {details}
        </WorkspaceMenu>
      ) : (
        details
      )}
    </div>
  );
}
