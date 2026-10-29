import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../shared/api";
import type { Operation, Page } from "../shared/types";
import { inProgress } from "./useProjectOperations";

export type PublicationReadiness = {
  can_publish: boolean;
  revision_id: string | null;
  configuration_version: number | null;
  reasons: { code: string; message: string }[];
  publication: {
    url?: string;
    revision_id?: string;
    published_at?: string;
  } | null;
};

export function publicationUrl(value?: string) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export function usePublicationReadiness(
  pid: string,
  revision: string | null,
  operationState = "",
  enabled = true,
) {
  const [data, setData] = useState<PublicationReadiness | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setData(null);
    setError(null);
    setLoading(true);
    if (!enabled) return;
    async function load() {
      const request = new AbortController();
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, 12000);
      try {
        const result = await api<PublicationReadiness>(
          `/projects/${encodeURIComponent(pid)}/publication-readiness${revision ? `?revision_id=${encodeURIComponent(revision)}` : ""}`,
          { signal: request.signal },
        );
        if (controller.signal.aborted) return;
        setData(result);
        setError(null);
      } catch (failure) {
        if (!controller.signal.aborted)
          setError(
            failure instanceof ApiError
              ? failure
              : new Error(
                  "Não foi possível verificar a publicação. Confira sua conexão e tente novamente.",
                ),
          );
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = setTimeout(load, 15000);
        }
      }
    }
    void load();
    window.addEventListener("online", refresh);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("online", refresh);
    };
  }, [pid, revision, operationState, refreshKey, refresh, enabled]);
  return { data, error, loading, refresh };
}

export function usePublicationOperation(
  pid: string,
  operationId: string | null,
) {
  const [operation, setOperation] = useState<Operation | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [syncedAt, setSyncedAt] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const known = useRef<Operation | null>(null);
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), []);
  const accept = useCallback(
    (incoming: Operation) => {
      if (incoming.project_id !== pid || incoming.type !== "publication")
        return;
      known.current = incoming;
      setOperation(incoming);
      setError(null);
      setLoading(false);
      setSyncedAt(Date.now());
    },
    [pid],
  );
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 1500;
    if (
      known.current?.project_id !== pid ||
      (operationId && known.current?.id !== operationId)
    ) {
      known.current = null;
      setOperation(null);
      setSyncedAt(0);
    }
    setLoading(!known.current);
    setError(null);
    async function load() {
      const request = new AbortController();
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, 12000);
      let repeat = true;
      try {
        let incoming: Operation | null;
        if (operationId) {
          incoming = await api<Operation>(
            `/operations/${encodeURIComponent(operationId)}`,
            {
              signal: request.signal,
            },
          );
          if (incoming.project_id !== pid || incoming.type !== "publication")
            throw new ApiError(
              "PUBLICATION_NOT_FOUND",
              "Esta publicação não pertence ao projeto.",
              404,
            );
        } else {
          const page = await api<Page<Operation>>(
            `/operations?project_id=${encodeURIComponent(pid)}&latest=true&kind=publication`,
            { signal: request.signal },
          );
          incoming =
            page.items.find(
              (item) => item.type === "publication" && item.project_id === pid,
            ) ?? null;
        }
        if (controller.signal.aborted) return;
        const current = known.current;
        if (
          incoming &&
          current?.id === incoming.id &&
          (incoming.updated_at ?? incoming.created_at) <
            (current.updated_at ?? current.created_at)
        )
          incoming = current;
        known.current = incoming;
        setOperation(incoming);
        setError(null);
        setSyncedAt(Date.now());
        delay = incoming && inProgress(incoming) ? 1500 : 15000;
        repeat = !operationId || (!!incoming && inProgress(incoming));
      } catch (failure) {
        if (controller.signal.aborted) return;
        setError(
          failure instanceof ApiError
            ? failure
            : new Error(
                "Não foi possível atualizar o acompanhamento. Confira sua conexão e tente novamente.",
              ),
        );
        if (
          failure instanceof ApiError &&
          [401, 403, 404].includes(failure.status)
        ) {
          known.current = null;
          setOperation(null);
          repeat = false;
        }
        delay = Math.min(delay * 2, 15000);
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
        if (!controller.signal.aborted) setLoading(false);
      }
      if (!controller.signal.aborted && repeat) timer = setTimeout(load, delay);
    }
    void load();
    window.addEventListener("online", refresh);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("online", refresh);
    };
  }, [pid, operationId, refreshKey, refresh]);
  return { operation, error, loading, syncedAt, accept, refresh };
}
