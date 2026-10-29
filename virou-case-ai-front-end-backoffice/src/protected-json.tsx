import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronUp, Copy } from "lucide-react";
import { ErrorNotice } from "./admin-ui";

export function ProtectedJson({
  value,
  available,
  onCopy,
  onCopyingChange,
  label,
}: {
  value: unknown;
  available: boolean;
  onCopy?: () => Promise<string>;
  onCopyingChange?: (copying: boolean) => void;
  label: string;
}) {
  const text = available ? (JSON.stringify(value, null, 2) ?? "null") : "";
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [copying, setCopying] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const match = useRef<HTMLElement>(null);
  const positions = useMemo(() => {
    if (!query) return [];
    const source = text.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    const found: number[] = [];
    let position = source.indexOf(needle);
    while (position >= 0) {
      found.push(position);
      position = source.indexOf(needle, position + needle.length);
    }
    return found;
  }, [text, query]);
  const index = positions[Math.min(active, positions.length - 1)];
  useEffect(() => {
    setActive(0);
    setCopied(false);
    setError(null);
  }, [text]);
  useEffect(() => {
    match.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active, query]);
  return (
    <div className="protected-json">
      <div className="trace-json-toolbar">
        <label>
          Buscar neste JSON
          <input
            type="search"
            maxLength={160}
            value={query}
            disabled={!available}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
          />
        </label>
        <div className="trace-search-navigation">
          <span role="status">
            {query
              ? positions.length
                ? `${Math.min(active + 1, positions.length)} de ${positions.length} ocorrências`
                : "Nenhuma ocorrência"
              : "JSON formatado"}
          </span>
          <button
            type="button"
            className="icon-button"
            aria-label="Ocorrência anterior"
            disabled={!positions.length}
            onClick={() =>
              setActive(
                (current) =>
                  (current - 1 + positions.length) % positions.length,
              )
            }
          >
            <ChevronUp size={16} />
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label="Próxima ocorrência"
            disabled={!positions.length}
            onClick={() =>
              setActive((current) => (current + 1) % positions.length)
            }
          >
            <ChevronDown size={16} />
          </button>
        </div>
        <button
          type="button"
          className="button"
          disabled={!available || !onCopy || copying}
          onClick={async () => {
            if (!onCopy || copying) return;
            setCopying(true);
            onCopyingChange?.(true);
            setCopied(false);
            setError(null);
            try {
              const authorized = await onCopy();
              if (authorized !== text)
                throw new Error(
                  "O conteúdo protegido mudou. Atualize o detalhe antes de copiar novamente.",
                );
              await navigator.clipboard.writeText(authorized);
              setCopied(true);
            } catch (failure) {
              setError(failure);
            } finally {
              setCopying(false);
              onCopyingChange?.(false);
            }
          }}
        >
          {copied ? <Check size={16} /> : <Copy size={16} />}
          {copying
            ? "Autorizando cópia…"
            : copied
              ? "JSON copiado"
              : "Copiar JSON protegido"}
        </button>
      </div>
      <ErrorNotice error={error} />
      {available && (
        <pre className="trace-json-content" tabIndex={0} aria-label={label}>
          <code>
            {index === undefined ? (
              text
            ) : (
              <>
                {text.slice(0, index)}
                <mark ref={match}>
                  {text.slice(index, index + query.length)}
                </mark>
                {text.slice(index + query.length)}
              </>
            )}
          </code>
        </pre>
      )}
    </div>
  );
}
