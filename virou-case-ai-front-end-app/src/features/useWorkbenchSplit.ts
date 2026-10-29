import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";

const storageKey = "viroucaseai.workspace.split";
const dividerWidth = 8;
const minimumCode = 640;
const minimumChat = 360;

export function useWorkbenchSplit() {
  const element = useRef<HTMLDivElement>(null);
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const bind = useCallback((value: HTMLDivElement | null) => {
    element.current = value;
    setNode(value);
  }, []);
  const [width, setWidth] = useState(0);
  const [ratio, setRatio] = useState(() => {
    try {
      const saved = Number(sessionStorage.getItem(storageKey));
      return saved > 0 && saved < 1 ? saved : 0.6;
    } catch {
      return 0.6;
    }
  });
  const [dragging, setDragging] = useState(false);
  const narrow =
    width < minimumCode + minimumChat + dividerWidth ||
    (node?.closest<HTMLElement>(".workspace-shell")?.clientWidth ||
      document.body.clientWidth ||
      window.innerWidth) < 1180;
  const usable = Math.max(0, width - dividerWidth);
  const maximum = Math.max(minimumCode, usable - minimumChat);
  const codeWidth = Math.min(maximum, Math.max(minimumCode, usable * ratio));
  useLayoutEffect(() => {
    if (!node) return;
    const update = () => setWidth(node.clientWidth);
    const observer = new ResizeObserver(update);
    observer.observe(node);
    update();
    return () => observer.disconnect();
  }, [node]);
  const resize = useCallback(
    (next: number) => {
      if (!usable) return;
      const value = Math.min(maximum, Math.max(minimumCode, next)) / usable;
      setRatio(value);
      try {
        sessionStorage.setItem(storageKey, String(value));
      } catch {}
    },
    [maximum, usable],
  );
  const move = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
      const node = element.current;
      const bounds = node?.getBoundingClientRect();
      if (node && bounds?.width && node.clientWidth) {
        const scale = bounds.width / node.clientWidth;
        resize((event.clientX - bounds.left) / scale - dividerWidth / 2);
      }
    },
    [resize],
  );
  const finish = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    setDragging(false);
  }, []);
  const keyboard = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? 64 : 16;
      const next =
        event.key === "Home"
          ? minimumCode
          : event.key === "End"
            ? maximum
            : event.key === "ArrowLeft"
              ? codeWidth - step
              : event.key === "ArrowRight"
                ? codeWidth + step
                : null;
      if (next === null) return;
      event.preventDefault();
      resize(next);
    },
    [codeWidth, maximum, resize],
  );
  return {
    element: bind,
    narrow,
    dragging,
    style: narrow
      ? undefined
      : {
          gridTemplateColumns: `${codeWidth}px ${dividerWidth}px minmax(0, 1fr)`,
        },
    separator: {
      role: "separator" as const,
      tabIndex: narrow ? -1 : 0,
      "aria-label": "Ajustar largura de Código e Chat",
      "aria-orientation": "vertical" as const,
      "aria-valuemin": minimumCode,
      "aria-valuemax": Math.round(maximum),
      "aria-valuenow": Math.round(codeWidth),
      "aria-valuetext": `${Math.round(codeWidth)} pixels para Código`,
      onKeyDown: keyboard,
      onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
        if (narrow || event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        setDragging(true);
      },
      onPointerMove: move,
      onPointerUp: finish,
      onPointerCancel: finish,
      onLostPointerCapture: () => setDragging(false),
    },
  };
}
