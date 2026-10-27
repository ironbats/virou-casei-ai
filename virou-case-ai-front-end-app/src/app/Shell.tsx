import {
  BookOpen,
  Folder,
  LogOut,
  Menu,
  UserRound,
  BarChart3,
  ArrowRight,
} from "lucide-react";
import { NavLink, Navigate, Outlet } from "react-router-dom";
import { useEffect, useState } from "react";
import { useSession, SessionLoading } from "./session";
import { api, percent, states } from "../shared/api";
import { Brand, ErrorNotice, useLoad } from "../shared/ui";
import type { Wallet } from "../shared/types";

export function Shell() {
  const { user, loading, logout } = useSession(),
    [open, setOpen] = useState(false),
    [error, setError] = useState<unknown>(null);
  const [online, setOnline] = useState(navigator.onLine);
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
    <div className="app-layout">
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
              >
                <I size={20} />
                {String(label)}
              </NavLink>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          <section className="access-summary">
            <strong>{states[user.access.status] ?? user.access.status}</strong>
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
          <NavLink className="account-link" to="/app/conta">
            <span className="avatar">{user.name.slice(0, 1)}</span>
            {user.name}
            <UserRound size={16} />
          </NavLink>
          <ErrorNotice error={error} />
          <button className="logout" onClick={() => logout().catch(setError)}>
            <LogOut size={19} />
            Sair
          </button>
        </div>
      </aside>
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
  );
}
