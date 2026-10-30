import { useCallback, useEffect, useRef, useState } from "react";
import { api, AdminApiError } from "./api";
import { normalizeSnapshot, type Snapshot } from "./cloudflare-snapshot";
import type { Connection } from "./integration-types";
import type { Verification } from "./publication-verification-status";
import type { AutomaticVerification } from "./automatic-publication-status";

export type PublicationDiagnostic = {
  snapshot?: Snapshot;
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
  automatic_verification?: AutomaticVerification;
};

export function usePublicationDiagnostics(config: Connection) {
  const [data, setData] = useState<PublicationDiagnostic | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [rawDiagnostic, setRawDiagnostic] = useState("");
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const refreshRequest = useRef<() => void>(() => {});
  const generation = useRef(0);
  const refresh = useCallback(() => refreshRequest.current(), []);

  useEffect(() => {
    let alive = true;
    let request: AbortController | null = null;
    let timer: number | undefined;
    let current: PublicationDiagnostic | null = null;
    let unauthorized = false;
    let refreshPending = false;
    setData(null);
    setRawDiagnostic("");
    setError(null);
    setStale(false);
    setReceivedAt(null);
    setLoading(true);
    generation.current += 1;
    const schedule = (delay: number | null) => {
      window.clearTimeout(timer);
      if (
        alive &&
        !unauthorized &&
        !document.hidden &&
        delay != null &&
        delay > 0
      ) {
        timer = window.setTimeout(() => void load(), Math.max(1000, delay));
      }
    };
    const interval = (result: PublicationDiagnostic) => {
      if (result.snapshot)
        return (
          result.snapshot.poll_after_ms ??
          result.snapshot.polling?.interval_ms ??
          null
        );
      const state = result.verification?.state;
      const automatic = result.automatic_verification;
      return (state && ["queued", "running", "reconciling"].includes(state)) ||
        (automatic &&
          ["scheduled", "retry_wait", "queued", "running"].includes(
            automatic.state,
          ))
        ? 4000
        : null;
    };
    async function load() {
      if (!alive || request || unauthorized || document.hidden) return;
      window.clearTimeout(timer);
      request = new AbortController();
      const controller = request;
      const sequence = ++generation.current;
      let timedOut = false;
      let retry: number | null = null;
      setLoading(true);
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 12000);
      try {
        const result = await api<PublicationDiagnostic>(
          `/integration-connections/${config.connection_id}/publication-diagnostics`,
          { signal: controller.signal },
        );
        if (
          !alive ||
          sequence !== generation.current ||
          controller.signal.aborted
        )
          return;
        const originalDiagnostic = JSON.stringify(result, null, 2);
        if (result.snapshot)
          result.snapshot = normalizeSnapshot(result.snapshot);
        result.can_publish = result.can_publish === true;
        result.reasons = Array.isArray(result.reasons)
          ? result.reasons.filter(
              (item) =>
                item &&
                typeof item.code === "string" &&
                typeof item.message === "string",
            )
          : [];
        result.verification_reasons = Array.isArray(result.verification_reasons)
          ? result.verification_reasons.filter(
              (item) =>
                item &&
                typeof item.code === "string" &&
                typeof item.message === "string",
            )
          : [];
        result.stack_readiness = Array.isArray(result.stack_readiness)
          ? result.stack_readiness
              .filter((item) => item && typeof item.stack === "string")
              .map((item) => ({
                ...item,
                can_publish: item.can_publish === true,
                reasons: Array.isArray(item.reasons)
                  ? item.reasons.filter(
                      (reason) =>
                        reason &&
                        typeof reason.code === "string" &&
                        typeof reason.message === "string",
                    )
                  : [],
              }))
          : [];
        result.workers = Array.isArray(result.workers)
          ? result.workers
              .filter((item) => item && typeof item.id === "string")
              .map((item) => ({
                ...item,
                available: item.available === true,
                message:
                  typeof item.message === "string"
                    ? item.message
                    : "Estado do executor não informado",
                checked_at:
                  typeof item.checked_at === "string" ? item.checked_at : "",
              }))
          : [];
        if (!result.verification || typeof result.verification.id !== "string")
          result.verification = null;
        else {
          result.verification.stack_results = Array.isArray(
            result.verification.stack_results,
          )
            ? result.verification.stack_results
            : [];
          result.verification.resources = Array.isArray(
            result.verification.resources,
          )
            ? result.verification.resources
            : [];
        }
        if (
          result.snapshot &&
          result.snapshot.connection.id !== config.connection_id
        ) {
          throw new Error(
            "O diagnóstico recebido é incompatível com esta conexão. Atualize o estado.",
          );
        }
        const acceptedVersion = Number(
          current?.snapshot?.connection.configuration_version,
        );
        const incomingVersion = Number(
          result.snapshot?.connection.configuration_version,
        );
        if (
          Number.isFinite(incomingVersion) &&
          (incomingVersion < config.version ||
            (Number.isFinite(acceptedVersion) &&
              incomingVersion < acceptedVersion))
        ) {
          throw new Error(
            "Uma leitura anterior da configuração foi descartada. Atualize o estado.",
          );
        }
        current = result;
        setData(result);
        setRawDiagnostic(originalDiagnostic);
        setError(null);
        setStale(false);
        setReceivedAt(new Date().toISOString());
        retry = interval(result);
      } catch (failure) {
        if (!alive || sequence !== generation.current) return;
        if (controller.signal.aborted && !timedOut) return;
        if (
          failure instanceof AdminApiError &&
          [401, 403, 404].includes(failure.status)
        ) {
          current = null;
          setData(null);
          setRawDiagnostic("");
          setReceivedAt(null);
          unauthorized = true;
        }
        setStale(true);
        setError(
          timedOut
            ? new Error(
                "O diagnóstico demorou para responder. A consulta será tentada novamente automaticamente.",
              )
            : failure,
        );
        retry = unauthorized ? null : 4000;
      } finally {
        window.clearTimeout(timeout);
        if (request === controller) request = null;
        if (alive && sequence === generation.current) {
          setLoading(false);
          if (refreshPending && !unauthorized) {
            refreshPending = false;
            schedule(1000);
          } else schedule(retry);
        }
      }
    }
    const expire = () => {
      unauthorized = true;
      generation.current += 1;
      request?.abort();
      window.clearTimeout(timer);
      current = null;
      setData(null);
      setRawDiagnostic("");
      setReceivedAt(null);
      setLoading(false);
      setStale(true);
      setError(
        new Error(
          "Sessão expirada. Entre novamente para consultar o diagnóstico.",
        ),
      );
    };
    const visibility = () => {
      if (document.hidden) {
        window.clearTimeout(timer);
      } else {
        void load();
      }
    };
    const reconnect = () => void load();
    refreshRequest.current = () => {
      if (request) refreshPending = true;
      else void load();
    };
    window.addEventListener("admin-expired", expire);
    window.addEventListener("online", reconnect);
    document.addEventListener("visibilitychange", visibility);
    void load();
    return () => {
      alive = false;
      generation.current += 1;
      request?.abort();
      window.clearTimeout(timer);
      window.removeEventListener("admin-expired", expire);
      window.removeEventListener("online", reconnect);
      document.removeEventListener("visibilitychange", visibility);
      refreshRequest.current = () => {};
    };
  }, [config.connection_id, config.version, config.state]);

  return { data, rawDiagnostic, error, loading, stale, receivedAt, refresh };
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
  if (!data.can_publish) {
    const automatic = data.automatic_verification;
    if (automatic?.enabled) {
      if (["queued", "running"].includes(automatic.state))
        return "Verificação automática em andamento";
      if (automatic.state === "scheduled")
        return "Aguardando verificação automática";
      if (automatic.state === "retry_wait")
        return "Aguardando nova tentativa automática";
    }
    return "Publicação bloqueada";
  }
  return data.stack_readiness?.some((profile) => !profile.can_publish)
    ? "Publicação parcialmente liberada"
    : "Publicação liberada";
}
