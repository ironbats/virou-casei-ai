import { useEffect, useRef, type ReactNode } from "react";
import { MoreHorizontal } from "lucide-react";

export function WorkspaceMenu({
  label,
  children,
  icon = <MoreHorizontal size={19} aria-hidden="true" />,
}: {
  label: string;
  children: ReactNode;
  icon?: ReactNode;
}) {
  const element = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!element.current?.contains(event.target as Node))
        element.current?.removeAttribute("open");
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, []);
  return (
    <details
      className="workspace-menu"
      ref={element}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.open = false;
          event.currentTarget.querySelector("summary")?.focus();
        }
      }}
    >
      <summary
        className="icon-button"
        role="button"
        aria-label={label}
        title={label}
      >
        {icon}
      </summary>
      <div
        className="workspace-menu-content"
        onClick={(event) => {
          if ((event.target as HTMLElement).closest("button, a"))
            element.current?.removeAttribute("open");
        }}
      >
        {children}
      </div>
    </details>
  );
}
