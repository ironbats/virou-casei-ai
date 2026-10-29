import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Connection } from "./integration-types";
import type { Verification } from "./publication-verification-status";

export type PublicationDiagnostic = {
  can_publish: boolean;
  can_verify?: boolean;
  verification_reasons?: { code: string; message: string }[];
  reasons: { code: string; message: string }[];
  stack_readiness?: {
    stack: string;
    can_publish: boolean;
    reasons: { code: string; message: string }[];
  }[];
  workers: {
    id: string;
    available: boolean;
    code?: string;
    message: string;
    checked_at: string;
    failed_checks?: string[];
    diagnostic_id?: string;
    remediation?: string | null;
    details?: {
      stage?: string;
      exit_code?: number | null;
      stderr?: string;
      exception_type?: string;
      duration_ms?: number;
    };
    environment?: Record<string, unknown>;
  }[];
  verification: Verification | null;
};

export function usePublicationDiagnostics(config: Connection) {
  const [data, setData] = useState<PublicationDiagnostic | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    setData(null);
    setError(null);
    setLoading(true);
    async function load() {
      const request = new AbortController();
      const cancel = () => request.abort();
      let timedOut = false;
      controller.signal.addEventListener("abort", cancel, { once: true });
      const timeout = window.setTimeout(() => {
        timedOut = true;
        request.abort();
      }, 12000);
      try {
        const result = await api<PublicationDiagnostic>(
          `/integration-connections/${config.connection_id}/publication-diagnostics`,
          { signal: request.signal },
        );
        if (!controller.signal.aborted) {
          setData(result);
          setError(null);
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setData(null);
          setError(
            timedOut
              ? new Error(
                  "O diagnóstico demorou para responder. A consulta será tentada novamente automaticamente.",
                )
              : failure,
          );
        }
      } finally {
        window.clearTimeout(timeout);
        controller.signal.removeEventListener("abort", cancel);
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = window.setTimeout(load, 4000);
        }
      }
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [config.connection_id, config.version, config.state, revision]);

  return { data, error, loading, refresh };
}

export type PublicationDiagnosticsRemote = ReturnType<
  typeof usePublicationDiagnostics
>;

export function publicationReadiness(
  data: PublicationDiagnostic | null,
  loading: boolean,
) {
  if (!data)
    return loading ? "Consultando publicação…" : "Diagnóstico indisponível";
  if (!data.can_publish) return "Publicação bloqueada";
  return data.stack_readiness?.some((profile) => !profile.can_publish)
    ? "Publicação parcialmente liberada"
    : "Publicação liberada";
}
