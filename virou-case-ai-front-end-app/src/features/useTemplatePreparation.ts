import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../shared/api";
import {
  preparationActive,
  preparationStates,
  type TemplatePreparation,
} from "./templatePreparationTypes";

function validPreparation(value: TemplatePreparation, pid: string) {
  if (
    !value ||
    value.project_id !== pid ||
    typeof value.id !== "string" ||
    !(value.state in preparationStates) ||
    !Number.isInteger(value.state_version) ||
    !Array.isArray(value.items) ||
    !value.progress ||
    !Number.isInteger(value.progress.total) ||
    !Number.isInteger(value.progress.completed) ||
    value.progress.total < 0 ||
    value.progress.completed < 0 ||
    value.progress.completed > value.progress.total ||
    value.items.some(
      (item) =>
        !item ||
        typeof item.attachment_id !== "string" ||
        typeof item.source_version_id !== "string" ||
        typeof item.title !== "string" ||
        !(item.state in preparationStates) ||
        !Array.isArray(item.suggested_fields) ||
        !Array.isArray(item.questions) ||
        !Array.isArray(item.errors),
    )
  )
    throw new Error("Não foi possível conferir a preparação deste projeto.");
  return value;
}

export function preparationError(error: unknown) {
  return error instanceof TypeError ||
    (error instanceof DOMException && error.name === "AbortError")
    ? new Error(
        "Não foi possível consultar a preparação. Confira sua conexão e atualize o status.",
      )
    : error;
}

export function useTemplatePreparation(pid: string, enabled: boolean) {
  const [items, setItems] = useState<TemplatePreparation[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [awaiting, setAwaiting] = useState(false);
  const [tick, setTick] = useState(0);
  const scope = useRef(pid);
  const alive = useRef(true);
  const known = useRef<TemplatePreparation[]>([]);
  const inflight = useRef<Promise<TemplatePreparation[]> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const serial = useRef(0);
  scope.current = pid;

  const merge = useCallback((incoming: TemplatePreparation[]) => {
    const byId = new Map(known.current.map((value) => [value.id, value]));
    for (const value of incoming) {
      const previous = byId.get(value.id);
      if (!previous || value.state_version >= previous.state_version)
        byId.set(value.id, value);
    }
    const next = [...byId.values()]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(0, 20);
    known.current = next;
    setItems(next);
    return next;
  }, []);

  const refresh = useCallback(async () => {
    if (inflight.current) return inflight.current;
    const generation = serial.current;
    const abort = new AbortController();
    controller.current = abort;
    const timer = setTimeout(() => abort.abort(), 12000);
    setLoading(true);
    const pending = (async () => {
      try {
        const response = await api<{ items: TemplatePreparation[] }>(
          `/projects/${encodeURIComponent(pid)}/template-preparations`,
          { signal: abort.signal },
        );
        if (!Array.isArray(response.items))
          throw new Error(
            "A consulta de preparação retornou dados incompletos.",
          );
        const values = response.items.map((value) =>
          validPreparation(value, pid),
        );
        if (
          !alive.current ||
          scope.current !== pid ||
          generation !== serial.current
        )
          return [];
        setError(null);
        return merge(values);
      } catch (cause) {
        if (
          alive.current &&
          scope.current === pid &&
          generation === serial.current
        )
          setError(preparationError(cause));
        throw cause;
      } finally {
        clearTimeout(timer);
        if (scope.current === pid && generation === serial.current) {
          inflight.current = null;
          if (alive.current) setLoading(false);
        }
      }
    })();
    inflight.current = pending;
    return pending;
  }, [pid, merge]);

  useEffect(() => {
    alive.current = true;
    known.current = [];
    setItems([]);
    setError(null);
    setAwaiting(false);
    return () => {
      alive.current = false;
      serial.current++;
      controller.current?.abort();
      inflight.current = null;
    };
  }, [pid]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    async function poll() {
      try {
        await refresh();
        failures = 0;
      } catch (cause) {
        failures++;
        if (cause instanceof ApiError && [401, 403, 404].includes(cause.status))
          return;
      }
      if (!cancelled && (awaiting || known.current.some(preparationActive)))
        timer = setTimeout(
          () => void poll(),
          Math.min(1500 * 2 ** failures, 12000),
        );
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enabled, refresh, awaiting, tick]);

  const accept = useCallback(
    (value: TemplatePreparation) => {
      if (!alive.current || scope.current !== pid) return;
      merge([validPreparation(value, pid)]);
      setAwaiting(false);
      setError(null);
      setTick((value) => value + 1);
    },
    [pid, merge],
  );

  return {
    items,
    current: items[0] || null,
    error,
    loading,
    awaiting,
    setAwaiting,
    refresh,
    accept,
  };
}
