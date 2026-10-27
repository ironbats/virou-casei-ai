import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../shared/api";
import type { Operation, Page } from "../shared/types";

export const inProgress = (operation: Operation) =>
  ["queued", "running", "reconciling"].includes(operation.state);

function merge(previous: Operation[], incoming: Operation[]) {
  const byId = new Map(previous.map((item) => [item.id, item]));
  for (const item of incoming) {
    const current = byId.get(item.id);
    if (
      !current ||
      (item.updated_at ?? item.created_at) >=
        (current.updated_at ?? current.created_at)
    ) {
      byId.set(item.id, item);
    }
  }
  return [...byId.values()].sort(
    (a, b) =>
      b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id),
  );
}

export function useProjectOperations(pid: string | undefined) {
  const [items, setItems] = useState<Operation[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [syncedAt, setSyncedAt] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const initialized = useRef(false);
  const known = useRef(items);
  known.current = items;
  const scope = useRef(pid);
  scope.current = pid;
  const accept = useCallback((operation: Operation) => {
    if (operation.project_id === scope.current)
      setItems((current) => merge(current, [operation]));
  }, []);
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), []);
  useEffect(() => {
    initialized.current = false;
    setItems([]);
    setCursor(null);
    setLoading(true);
    setSyncedAt(0);
  }, [pid]);
  useEffect(() => {
    if (!pid) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 1500;
    async function poll() {
      const request = new AbortController();
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(abort, 12000);
      try {
        const [page, active] = await Promise.all([
          api<Page<Operation>>(`/operations?project_id=${pid}&latest=true`, {
            signal: request.signal,
          }),
          api<Page<Operation>>(
            `/operations?project_id=${pid}&state=active&latest=true&kind=generation`,
            { signal: request.signal },
          ),
        ]);
        const incoming = [...page.items, ...active.items];
        const present = new Set(incoming.map((item) => item.id));
        const completed = await Promise.all(
          known.current
            .filter((item) => inProgress(item) && !present.has(item.id))
            .map((item) =>
              api<Operation>(`/operations/${item.id}`, {
                signal: request.signal,
              }),
            ),
        );
        if (controller.signal.aborted) return;
        setItems((current) => merge(current, [...incoming, ...completed]));
        if (!initialized.current) {
          setCursor(page.next_cursor ?? null);
          initialized.current = true;
        }
        setError(null);
        setLoading(false);
        setSyncedAt(Date.now());
        delay = [...page.items, ...active.items].some(inProgress) ? 1500 : 4000;
      } catch (failure) {
        if (controller.signal.aborted) return;
        setError(failure);
        setLoading(false);
        delay = Math.min(delay * 2, 15000);
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, delay);
    }
    void poll();
    const reconnect = () => refresh();
    window.addEventListener("online", reconnect);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("online", reconnect);
    };
  }, [pid, refreshKey, refresh]);
  async function loadMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    const projectId = pid;
    const request = new AbortController();
    const timeout = setTimeout(() => request.abort(), 12000);
    try {
      const page = await api<Page<Operation>>(
        `/operations?project_id=${pid}&latest=true&cursor=${encodeURIComponent(cursor)}`,
        { signal: request.signal },
      );
      if (scope.current !== projectId) return;
      setItems((current) => merge(current, page.items));
      setCursor(page.next_cursor ?? null);
      setError(null);
    } catch (failure) {
      if (scope.current === projectId) setError(failure);
    } finally {
      clearTimeout(timeout);
      if (scope.current === projectId) setLoadingMore(false);
    }
  }
  return {
    items,
    error,
    loading,
    loadingMore,
    cursor,
    syncedAt,
    accept,
    refresh,
    loadMore,
  };
}
