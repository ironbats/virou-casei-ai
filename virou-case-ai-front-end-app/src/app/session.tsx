import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
  type FormEvent,
} from "react";
import { api, clearSession, setSession } from "../shared/api";
import { ErrorNotice, Loading, Modal } from "../shared/ui";
import type { User } from "../shared/types";

type SessionValue = {
  user: User | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};
const Context = createContext<SessionValue>({
  user: null,
  loading: true,
  refresh: async () => {},
  logout: async () => {},
});
export const useSession = () => useContext(Context);
export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null),
    [loading, setLoading] = useState(true),
    [expired, setExpired] = useState(false);
  async function refresh() {
    const me = await api<User>("/me");
    setSession(me.id, me.csrf_token);
    setUser(me);
  }
  useEffect(() => {
    refresh()
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    const handler = () => setExpired(true);
    window.addEventListener("vira-session-expired", handler);
    return () => window.removeEventListener("vira-session-expired", handler);
  }, []);
  async function logout() {
    await api("/auth/logout", { method: "POST" });
    clearSession();
    setUser(null);
    location.assign("/login");
  }
  return (
    <Context.Provider value={{ user, loading, refresh, logout }}>
      {children}
      {expired && (
        <Modal title="Sua sessão terminou" close={() => setExpired(false)}>
          <p>
            Entre com a mesma conta para continuar com as alterações abertas.
          </p>
          <LoginForm
            onSuccess={async (next) => {
              if (user && next.id !== user.id) {
                clearSession();
                location.assign("/app");
                return;
              }
              await refresh();
              setExpired(false);
            }}
          />
        </Modal>
      )}
    </Context.Provider>
  );
}
export function LoginForm({
  onSuccess,
}: {
  onSuccess: (user: User) => Promise<void>;
}) {
  const [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    const data = new FormData(event.currentTarget);
    try {
      const user = await api<User>("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          login: data.get("login"),
          password: data.get("password"),
        }),
      });
      setSession(user.id, user.csrf_token);
      await onSuccess(user);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="form">
      <label>
        Login
        <input
          name="login"
          autoComplete="username"
          required
          maxLength={160}
          autoFocus
        />
      </label>
      <label>
        Senha
        <input
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={128}
        />
      </label>
      <ErrorNotice error={error} />
      <button className="button primary" disabled={busy}>
        {busy ? "Entrando…" : "Entrar"}
      </button>
    </form>
  );
}
export function SessionLoading() {
  return (
    <main className="centered">
      <Loading />
    </main>
  );
}
