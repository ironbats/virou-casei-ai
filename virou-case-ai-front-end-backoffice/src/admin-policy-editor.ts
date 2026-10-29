import { useRef, useState, type FormEvent } from "react";
import { command } from "./api";

export function useAdminPolicy<T extends { version: number }, V>(
  path: string,
  initial: T,
  select: (result: T) => V,
  body: (value: V, reason: string) => unknown,
  onSaved: () => void,
) {
  const [current, setCurrent] = useState(initial);
  const [value, setValue] = useState(() => select(initial));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState("");
  const lock = useRef(false);
  const changed = JSON.stringify(value) !== JSON.stringify(select(current));
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (lock.current || !changed) return;
    if (reason.trim().length < 8) {
      setError(new Error("Informe um motivo com pelo menos 8 caracteres."));
      return;
    }
    lock.current = true;
    setBusy(true);
    setError(null);
    setMessage("");
    try {
      const result = await command<T>(
        path,
        body(value, reason.trim()),
        "PUT",
        current.version,
      );
      setCurrent(result);
      setValue(select(result));
      setReason("");
      setMessage(`Configuração salva na versão ${result.version}.`);
      onSaved();
    } catch (failure) {
      setError(failure);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return {
    current,
    value,
    setValue,
    reason,
    setReason,
    busy,
    error,
    message,
    changed,
    dirty: changed || !!reason,
    submit,
  };
}
