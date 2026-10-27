import { useEffect, useRef, useState } from "react";
import { canFormat, formatCode } from "./formatCode";

export function useFormattedSource(
  path: string,
  source: string,
  onChange: (value: string) => void,
) {
  const [display, setDisplay] = useState({
    path: "",
    source: "",
    value: "",
    error: "",
  });
  const edited = useRef<{ path: string; value: string } | null>(null);
  const revision = useRef(0);
  useEffect(() => {
    const id = ++revision.current;
    if (edited.current?.path === path && edited.current.value === source) {
      setDisplay({ path, source, value: source, error: "" });
      return;
    }
    if (!canFormat(path) || !source) {
      setDisplay({ path, source, value: source, error: "" });
      return;
    }
    void formatCode(path, source).then(
      (value) => {
        if (id === revision.current)
          setDisplay({ path, source, value, error: "" });
      },
      () => {
        if (id === revision.current)
          setDisplay({
            path,
            source,
            value: source,
            error:
              "Exibindo o conteúdo original: não foi possível formatar este arquivo.",
          });
      },
    );
    return () => {
      revision.current++;
    };
  }, [path, source]);
  return {
    value: display.value,
    ready: display.path === path && display.source === source,
    error: display.error,
    edit(value: string) {
      revision.current++;
      edited.current = { path, value };
      setDisplay({ path, source: value, value, error: "" });
      onChange(value);
    },
  };
}
