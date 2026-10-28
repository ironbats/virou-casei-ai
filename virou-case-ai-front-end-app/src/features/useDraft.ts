import { useEffect, useRef, useState } from "react";
import { api, ApiError, command } from "../shared/api";
import type { Draft } from "../shared/types";

export function useDraft<T>(
  path: string,
  payload: (data: T) => unknown,
  merge?: (local: T, saved: T, remote: T) => T | null,
  debounceMs = 700,
) {
  const [data, render] = useState<T | null>(null),
    [status, setStatus] = useState("Carregando rascunho…"),
    [error, setError] = useState<unknown>(null),
    [conflict, setConflict] = useState<Draft<T> | null>(null);
  const current = useRef<T | null>(null),
    version = useRef(0),
    saved = useRef(""),
    snapshot = useRef<T | null>(null),
    pending = useRef<Promise<void> | null>(null),
    blocked = useRef(false),
    alive = useRef(true),
    serialize = useRef(payload);
  serialize.current = payload;
  const scope = useRef(path);
  scope.current = path;
  function setData(value: T | ((old: T) => T)) {
    if (!current.current) return;
    current.current =
      typeof value === "function"
        ? (value as (old: T) => T)(current.current)
        : value;
    render(current.current);
    setStatus("Alterações no rascunho");
  }
  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    api<Draft<T>>(path)
      .then((d) => {
        if (!alive.current || cancelled) return;
        version.current = d.version;
        current.current = d.data;
        snapshot.current = d.data;
        saved.current = JSON.stringify(serialize.current(d.data));
        render(d.data);
        setStatus("Rascunho salvo");
      })
      .catch((e) => {
        if (alive.current && !cancelled) {
          setError(e);
          setStatus("Não foi possível carregar o rascunho");
        }
      });
    return () => {
      cancelled = true;
      alive.current = false;
      current.current = null;
    };
  }, [path]);
  async function flush(): Promise<void> {
    if (pending.current) {
      await pending.current;
      return flush();
    }
    if (blocked.current)
      throw new Error(
        "Compare e resolva o conflito do rascunho antes de continuar.",
      );
    if (!current.current)
      throw new Error("Aguarde o carregamento do rascunho.");
    const submitted = current.current;
    const body = serialize.current(submitted),
      encoded = JSON.stringify(body);
    if (encoded === saved.current) return;
    setStatus("Salvando rascunho…");
    setError(null);
    pending.current = (async () => {
      try {
        const response = await command<{ version: number }>(
          path,
          body,
          "PUT",
          version.current,
        );
        version.current = response.version;
        saved.current = encoded;
        snapshot.current = submitted;
        if (alive.current) setStatus("Rascunho salvo");
      } catch (e) {
        if (alive.current) {
          setError(e);
          setStatus("Rascunho não salvo — conteúdo preservado nesta tela");
          if (e instanceof ApiError && [409, 412].includes(e.status)) {
            blocked.current = true;
            const remote = await api<Draft<T>>(path);
            if (alive.current) {
              const merged =
                merge && snapshot.current && current.current
                  ? merge(current.current, snapshot.current, remote.data)
                  : null;
              if (merged) {
                version.current = remote.version;
                snapshot.current = remote.data;
                saved.current = JSON.stringify(serialize.current(remote.data));
                current.current = merged;
                render(merged);
                blocked.current = false;
                setConflict(null);
                setError(null);
                setStatus("Rascunho sincronizado");
                return;
              }
              setConflict(remote);
            }
          }
        }
        throw e;
      } finally {
        pending.current = null;
      }
    })();
    await pending.current;
    if (
      alive.current &&
      current.current &&
      JSON.stringify(serialize.current(current.current)) !== saved.current
    )
      await flush();
  }
  useEffect(() => {
    if (
      !data ||
      blocked.current ||
      JSON.stringify(serialize.current(data)) === saved.current
    )
      return;
    const timer = setTimeout(() => void flush().catch(() => {}), debounceMs);
    return () => clearTimeout(timer);
  }, [data]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (
        current.current &&
        JSON.stringify(serialize.current(current.current)) !== saved.current
      ) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, []);
  function resolve(remote: boolean) {
    if (!conflict) return;
    snapshot.current = conflict.data;
    version.current = conflict.version;
    saved.current = JSON.stringify(serialize.current(conflict.data));
    blocked.current = false;
    setConflict(null);
    setError(null);
    if (remote) {
      current.current = conflict.data;
      render(conflict.data);
      setStatus("Rascunho recarregado");
    } else {
      render(current.current ? { ...current.current } : null);
      setStatus("Alterações locais prontas para reaplicar");
    }
  }
  async function reload() {
    if (pending.current) await pending.current;
    const remote = await api<Draft<T>>(path);
    if (!alive.current || scope.current !== path) return;
    const unchanged =
      current.current &&
      JSON.stringify(serialize.current(current.current)) === saved.current;
    const next = unchanged
      ? remote.data
      : merge && current.current && snapshot.current
        ? merge(current.current, snapshot.current, remote.data)
        : null;
    if (!next) {
      blocked.current = true;
      setConflict(remote);
      setStatus("Compare as alterações do rascunho");
      return;
    }
    version.current = remote.version;
    snapshot.current = remote.data;
    saved.current = JSON.stringify(serialize.current(remote.data));
    current.current = next;
    render(next);
    setError(null);
    setConflict(null);
    blocked.current = false;
    setStatus("Rascunho sincronizado");
  }
  return {
    reload,
    data,
    setData,
    status,
    error,
    conflict,
    resolve,
    flush,
    version: () => version.current,
  };
}
