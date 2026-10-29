import {
  BookOpen,
  Folder,
  LogOut,
  Menu,
  UserRound,
  BarChart3,
  ArrowRight,
} from "lucide-react";
import { NavLink, Navigate, Outlet, useMatch } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { useSession, SessionLoading } from "./session";
import { api, percent, states } from "../shared/api";
import { Brand, ErrorNotice, Modal, useLoad } from "../shared/ui";
import type { Wallet } from "../shared/types";
import { WorkspaceChromeContext } from "./WorkspaceChrome";
import "../features/workspace.css";

export function Shell() {
  const { user, loading, logout } = useSession(),
    [open, setOpen] = useState(false),
    [error, setError] = useState<unknown>(null);
  const shell = useRef<HTMLDivElement>(null);
  const [online, setOnline] = useState(navigator.onLine);
  const workspace = !!useMatch("/app/projetos/:projectId");
  const [topbar, setTopbar] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    setOpen(false);
  }, [workspace]);
  useEffect(() => {
    const element = shell.current;
    if (!workspace || loading || !element) return;
    const update = () => {
      const scale = element.offsetWidth
        ? element.getBoundingClientRect().width / element.offsetWidth
        : 1;
      document.documentElement.style.setProperty(
        "--workspace-height",
        `${(window.visualViewport?.height ?? window.innerHeight) / (scale || 1)}px`,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    window.visualViewport?.addEventListener("resize", update);
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.visualViewport?.removeEventListener("resize", update);
      window.removeEventListener("resize", update);
      document.documentElement.style.removeProperty("--workspace-height");
    };
  }, [workspace, loading]);
  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  const { data: wallet } = useLoad(
    () => (user ? api<Wallet>("/wallet") : Promise.resolve(null)),
    [user?.id],
  );
  if (loading) return <SessionLoading />;
  if (!user) return <Navigate to="/login" replace />;
  return (
    <WorkspaceChromeContext.Provider value={topbar}>
      <div
        ref={shell}
        className={"app-layout" + (workspace ? " workspace-shell" : "")}
      >
        {workspace && (
          <header className="workspace-global-header">
            <button
              className="icon-button workspace-nav-toggle"
              aria-label="Abrir navegação"
              aria-expanded={open}
              onClick={() => setOpen(true)}
            >
              <Menu size={20} />
            </button>
            <Brand />
            <div className="workspace-topbar-slot" ref={setTopbar} />
          </header>
        )}
        <header className="mobile-header">
          <Brand />
          <button
            className="icon-button"
            aria-label="Abrir navegação"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            <Menu />
          </button>
        </header>
        <aside className={"sidebar " + (open ? "open" : "")}>
          <Brand />
          <nav aria-label="Navegação principal">
            {[
              [Folder, "/app/projetos", "Meus projetos"],
              [BookOpen, "/app/aprendizado", "Aprender"],
              [BarChart3, "/app/consumo", "Uso e acesso"],
            ].map(([Icon, path, label]) => {
              const I = Icon as typeof Folder;
              return (
                <NavLink
                  key={String(path)}
                  to={String(path)}
                  onClick={() => setOpen(false)}
                  aria-label={String(label)}
                  title={String(label)}
                >
                  <I size={20} />
                  <span className="navigation-label">{String(label)}</span>
                </NavLink>
              );
            })}
          </nav>
          <div className="sidebar-bottom">
            <section className="access-summary">
              <strong>
                {states[user.access.status] ?? user.access.status}
              </strong>
              <p>30 dias por ciclo</p>
              {wallet && (
                <>
                  <div className="small">
                    Uso de IA · {percent(wallet.consumed_ratio)}
                  </div>
                  <progress
                    value={wallet.consumed_ratio}
                    max={1}
                    aria-label="Uso de IA"
                  />
                </>
              )}
              <NavLink to="/app/consumo">
                Ver uso e acesso <ArrowRight size={15} />
              </NavLink>
            </section>
            <NavLink
              className="account-link"
              to="/app/conta"
              aria-label={"Conta de " + user.name}
              title={"Conta de " + user.name}
            >
              <span className="avatar">{user.name.slice(0, 1)}</span>
              <span className="navigation-label">{user.name}</span>
              <UserRound size={16} />
            </NavLink>
            <ErrorNotice error={error} />
            <button
              className="logout"
              aria-label="Sair"
              title="Sair"
              onClick={() => logout().catch(setError)}
            >
              <LogOut size={19} />
              <span className="navigation-label">Sair</span>
            </button>
          </div>
        </aside>
        {workspace && open && (
          <Modal
            title="Navegação"
            className="workspace-navigation-drawer"
            close={() => setOpen(false)}
          >
            <nav aria-label="Navegação expandida">
              <NavLink to="/app/projetos" onClick={() => setOpen(false)}>
                <Folder size={20} />
                Meus projetos
              </NavLink>
              <NavLink to="/app/aprendizado" onClick={() => setOpen(false)}>
                <BookOpen size={20} />
                Aprender
              </NavLink>
              <NavLink to="/app/consumo" onClick={() => setOpen(false)}>
                <BarChart3 size={20} />
                Uso e acesso
              </NavLink>
              <NavLink to="/app/conta" onClick={() => setOpen(false)}>
                <UserRound size={20} />
                Conta de {user.name}
              </NavLink>
              <button
                className="logout"
                onClick={() => logout().catch(setError)}
              >
                <LogOut size={20} />
                Sair
              </button>
            </nav>
            <ErrorNotice error={error} />
          </Modal>
        )}
        <main id="main" className="app-main">
          {!online && (
            <div role="status" className="notice">
              Você está sem conexão. Suas edições locais continuam nesta tela;
              reconecte para salvar.
            </div>
          )}
          <Outlet />
        </main>
      </div>
    </WorkspaceChromeContext.Provider>
  );
}
