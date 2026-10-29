import { createContext, useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const WorkspaceChromeContext = createContext<HTMLDivElement | null>(
  null,
);

export function WorkspaceTopbar({ children }: { children: ReactNode }) {
  const target = useContext(WorkspaceChromeContext);
  return target ? createPortal(children, target) : <>{children}</>;
}
