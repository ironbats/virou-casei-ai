import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  ClipboardList,
  Cpu,
  Gauge,
  LogOut,
  ShieldCheck,
  Users,
  Terminal,
} from "lucide-react";
import { api, brl, command, date, session } from "./api";
import "./styles.css";
import "./admin.css";
import { PasswordField } from "./password-field";
import { PublicationReconcileButton } from "./publication-reconcile";
import { IntegrationSettings } from "./integration-settings";
import { Observability } from "./observability";
import { AISettings } from "./ai-settings";
import { PreparationSettings } from "./preparation-settings";
import { GenerationSettings } from "./generation-settings";

import type {
  User,
  Account,
  Page,
  Operation,
  Audit,
  Purge,
  Status,
  Wallet,
  AdminModal,
} from "./admin-types";
import { ErrorNotice } from "./admin-ui";
import { AdminDialog } from "./admin-dialog";

function Brand() {
  return (
    <span className="brand">
      <span className="brand-mark">v</span>viroucaseai
    </span>
  );
}
function App() {
  const [user, setUser] = useState<User | null>(null),
    [loading, setLoading] = useState(true),
    [tab, setTab] = useState("accounts");
  const [error, setError] = useState<unknown>(null),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [cursor, setCursor] = useState("");
  const [accounts, setAccounts] = useState<Page<Account>>({ items: [] }),
    [ops, setOps] = useState<Page<Operation>>({ items: [] }),
    [audit, setAudit] = useState<Page<Audit>>({ items: [] }),
    [status, setStatus] = useState<Status | null>(null);
  const [modal, setModal] = useState<AdminModal>(null),
    [review, setReview] = useState<Record<string, unknown> | null>(null),
    [wallet, setWallet] = useState<Wallet | null>(null);
  const [aiRefresh, setAiRefresh] = useState(0);
  const [aiConfigurationRefresh, setAiConfigurationRefresh] = useState(0);
  const [integrationRefresh, setIntegrationRefresh] = useState(0);
  const [monitorRefresh, setMonitorRefresh] = useState(0);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const formRef = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (review || !formRef.current) return;
    for (const [name, value] of Object.entries(draft)) {
      const input = formRef.current.elements.namedItem(name);
      if (input instanceof HTMLInputElement && name !== "password") {
        if (input.type === "checkbox") input.checked = value === "on";
        else input.value = value;
      }
    }
  }, [review, modal]);
  async function refresh() {
    if (tab === "observability") setMonitorRefresh((value) => value + 1);
    if (tab === "ai") setAiRefresh((value) => value + 1);
    if (tab === "integrations") setIntegrationRefresh((value) => value + 1);
    if (tab === "accounts")
      setAccounts(await api("/accounts" + (cursor ? "?cursor=" + cursor : "")));
    if (tab === "operations")
      setOps(await api("/operations" + (cursor ? "?cursor=" + cursor : "")));
    if (tab === "audit")
      setAudit(
        await api("/audit-events" + (cursor ? "?cursor=" + cursor : "")),
      );
    if (tab === "status") setStatus(await api("/operational-status"));
  }
  useEffect(() => {
    api<User>("/me")
      .then((u) => {
        session(u.id, u.csrf_token);
        setUser(u);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
    const expire = () => setUser(null);
    window.addEventListener("admin-expired", expire);
    return () => window.removeEventListener("admin-expired", expire);
  }, []);
  useEffect(() => {
    if (user) {
      setError(null);
      refresh().catch(setError);
    }
  }, [user?.id, tab, cursor]);
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const f = new FormData(e.currentTarget);
    try {
      const u = await api<User>("/auth/login", {
        method: "POST",
        body: JSON.stringify({
          login: f.get("login"),
          password: f.get("password"),
        }),
      });
      session(u.id, u.csrf_token);
      setUser(u);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  function open(
    action: string,
    account?: Account,
    op?: Operation,
    purge?: Purge,
  ) {
    setModal({ action, account, op, purge });
    setDraft({});
    setReview(null);
    setWallet(null);
    setError(null);
    if (action === "wallet" && account)
      api<Wallet>(`/accounts/${account.id}/wallet`)
        .then(setWallet)
        .catch(setError);
  }
  function prepare(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.currentTarget).entries());
    const action = modal!.action;
    setDraft(f as Record<string, string>);
    const payload: Record<string, unknown> = { ...f };
    if (action === "grant") {
      payload.starts_at = new Date().toISOString();
      payload.access_days = Number(f.access_days);
    }
    if (action === "access-adjust") {
      payload.expires_at = new Date(String(f.expires_at)).toISOString();
      payload.expected_version = modal!.account!.access.entitlement_version;
    }
    if (action === "reconcile") {
      payload.confirmed_no_effect = f.confirmed_no_effect === "on";
      delete payload.input;
      delete payload.cached;
      delete payload.output;
      if (!payload.confirmed_no_effect)
        payload.usage = {
          input: Number(f.input),
          cached: Number(f.cached),
          output: Number(f.output),
        };
    }
    if (action === "purge") payload.fence = modal!.purge!.fence;
    setReview(payload);
  }
  async function execute() {
    if (!modal || !review || busy) return;
    setBusy(true);
    setError(null);
    const base = "/accounts/" + modal.account?.id;
    const routes: Record<string, string> = {
      create: "/accounts",
      grant: base + "/access-grants",
      topup: base + "/topups",
      suspend: base + "/suspend",
      reset: base + "/credentials/reset",
      adjust: base + "/wallet-adjustments",
      "access-adjust": base + "/access-adjustments",
      reconcile: "/operations/" + modal.op?.id + "/reconcile",
      purge: "/accounts/" + modal.purge?.account_id + "/purge-evidence",
    };
    try {
      await command(routes[modal.action], review);
      setModal(null);
      setReview(null);
      setDraft({});
      setMessage("Operação registrada e auditada.");
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }
  const nextCursor =
    tab === "accounts"
      ? accounts.next_cursor
      : tab === "operations"
        ? ops.next_cursor
        : tab === "audit"
          ? audit.next_cursor
          : null;
  const titles: Record<string, string> = {
    create: "Criar usuário",
    settings: "Ajustes da conta",
    "access-adjust": "Ajustar prazo de acesso",
    grant: "Ativar ou renovar acesso",
    topup: "Registrar recarga",
    suspend: "Suspender uso",
    reset: "Redefinir senha",
    adjust: "Ajustar saldo",
    wallet: "Saldo e concessões",
    reconcile: "Reconciliar operação",
    purge: "Registrar evidência de backups",
  };
  if (loading)
    return <main className="centered">Verificando sessão local…</main>;
  if (!user)
    return (
      <main className="login-page">
        <Brand />
        <section className="login-card">
          <p className="eyebrow">ADMINISTRAÇÃO LOCAL</p>
          <h1>
            Operação com
            <br />
            <em>controle.</em>
          </h1>
          <p className="muted">Acesso restrito à máquina local.</p>
          <form onSubmit={login} className="form">
            <label>
              Login
              <input name="login" autoComplete="username" required />
            </label>
            <label>
              Senha
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </label>
            <ErrorNotice error={error} />
            <button className="button primary" disabled={busy}>
              Entrar no backoffice
            </button>
          </form>
        </section>
      </main>
    );
  return (
    <div className="admin-layout">
      <header className="admin-header">
        <Brand />
        <span className="badge">
          <ShieldCheck size={15} />
          Administração local
        </span>
        <button
          className="button"
          onClick={async () => {
            try {
              await api("/auth/logout", { method: "POST" });
              for (const k of Object.keys(sessionStorage))
                if (k.startsWith("vira-admin-intent:"))
                  sessionStorage.removeItem(k);
              session("", "");
              setUser(null);
              setAccounts({ items: [] });
              setOps({ items: [] });
              setAudit({ items: [] });
              setStatus(null);
              setModal(null);
              setReview(null);
              setWallet(null);
              setDraft({});
            } catch (e) {
              setError(e);
            }
          }}
        >
          <LogOut size={16} />
          Sair
        </button>
      </header>
      <nav className="admin-nav" aria-label="Administração">
        {[
          [Users, "accounts", "Contas e acesso"],
          [Cpu, "ai", "Agentes de IA"],
          [ShieldCheck, "integrations", "Integrações e templates"],
          [Gauge, "operations", "Operações"],
          [ClipboardList, "audit", "Auditoria"],
          [ShieldCheck, "status", "Estado operacional"],
          [Terminal, "observability", "Observabilidade"],
        ].map(([Icon, key, title]) => {
          const I = Icon as typeof Users;
          return (
            <button
              key={String(key)}
              className={tab === key ? "selected" : ""}
              onClick={() => {
                setTab(String(key));
                setCursor("");
                setMessage("");
              }}
            >
              <I size={18} />
              {String(title)}
            </button>
          );
        })}
      </nav>
      <main
        id="main"
        className={`admin-main${tab === "observability" ? " monitor-main" : ""}`}
      >
        <div className="page-heading">
          <div>
            <p className="eyebrow">VIROUCASEAI · OPERAÇÃO MANUAL</p>
            <h1>
              {tab === "accounts"
                ? "Contas e acesso"
                : tab === "observability"
                  ? "Observabilidade"
                  : tab === "operations"
                    ? "Operações"
                    : tab === "audit"
                      ? "Auditoria"
                      : tab === "ai"
                        ? "Agentes de IA"
                        : tab === "integrations"
                          ? "Integrações e templates"
                          : "Estado operacional"}
            </h1>
            <p>
              {tab === "observability"
                ? "Jev, agentes e Cloudflare: acompanhe etapas, falhas e duração das execuções."
                : tab === "integrations"
                  ? "Contas, conexões e templates em um só lugar, com versões e histórico preservados."
                  : "Pagamentos externos, decisões explícitas e histórico preservado."}
            </p>
          </div>
          {tab === "accounts" && (
            <button className="button primary" onClick={() => open("create")}>
              Criar usuário
            </button>
          )}
          <button className="button" onClick={() => refresh().catch(setError)}>
            Atualizar
          </button>
        </div>
        <ErrorNotice error={modal ? null : error} />
        {message && (
          <div className="notice success" role="status">
            {message}
          </div>
        )}
        {tab === "observability" && (
          <Observability refreshToken={monitorRefresh} />
        )}
        {tab === "ai" && (
          <>
            <AISettings
              refreshToken={aiRefresh}
              onChanged={() => setAiConfigurationRefresh((value) => value + 1)}
            />
            <GenerationSettings
              refreshToken={aiRefresh + aiConfigurationRefresh}
            />
            <PreparationSettings
              refreshToken={aiRefresh + aiConfigurationRefresh}
            />
          </>
        )}
        {tab === "integrations" && (
          <IntegrationSettings refreshToken={integrationRefresh} />
        )}
        {tab === "accounts" && (
          <div className="admin-accounts">
            {accounts.items.length === 0 && (
              <section className="empty-state">
                <Users size={40} />
                <h2>Nenhuma conta provisionada.</h2>
                <p>
                  O cadastro é manual e não concede acesso ou orçamento até a
                  confirmação do pagamento.
                </p>
              </section>
            )}
            {accounts.items.map((a) => (
              <article className="panel" key={a.id}>
                <div className="card-title">
                  <div>
                    <h2>{a.name}</h2>
                    <p className="small muted">{a.login}</p>
                  </div>
                  <span className="badge">
                    {(
                      {
                        active: "Acesso ativo",
                        grace: "Acesso encerrado · arquivos disponíveis",
                        suspended: "Suspenso",
                        pending: "Aguardando ativação",
                        no_access: "Sem acesso",
                        purged: "Dados removidos",
                        purge_eligible: "Prazo dos arquivos encerrado",
                        purging: "Remoção em andamento",
                        reconciling: "Em apuração",
                      } as Record<string, string>
                    )[a.access.status] ?? "Sem acesso"}
                  </span>
                </div>
                <dl className="account-dates">
                  <dt>Uso da plataforma até</dt>
                  <dd>{date(a.access.access_expires_at)}</dd>
                  <dt>Arquivos para download até</dt>
                  <dd>{date(a.access.purge_at)}</dd>
                </dl>
                <p className="small muted">
                  Após o fim do acesso, os arquivos ficam disponíveis por mais 2
                  dias apenas para consulta e download.
                </p>
                <div className="actions">
                  {[
                    ["grant", "Ativar / renovar"],
                    ["wallet", "Ver saldo"],
                    ["topup", "Recarga"],
                    ["settings", "Ajustes da conta"],
                    ["reset", "Redefinir senha"],
                    ["suspend", "Suspender"],
                  ].map(([act, title]) => (
                    <button
                      className={"button " + (act === "grant" ? "primary" : "")}
                      key={act}
                      onClick={() => open(act, a)}
                    >
                      {title}
                    </button>
                  ))}
                </div>
              </article>
            ))}
          </div>
        )}
        {tab === "operations" && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Operação / conta</th>
                  <th>Estado</th>
                  <th>Apuração</th>
                  <th>Modelo e custo real</th>
                  <th>Ação</th>
                </tr>
              </thead>
              <tbody>
                {ops.items.map((o) => (
                  <tr key={o.id}>
                    <td>
                      {o.id}
                      <small>{o.account_id}</small>
                      <span>{o.type}</span>
                    </td>
                    <td>{o.state}</td>
                    <td>{o.billing_status}</td>
                    <td>
                      {o.model_label ?? "Sem modelo"}
                      <small>
                        {o.attempt?.actual_brl
                          ? brl(o.attempt.actual_brl)
                          : "Não apurado"}
                      </small>
                      <details>
                        <summary>Política e uso</summary>
                        <pre>
                          {JSON.stringify(
                            { policy: o.policy, attempt: o.attempt },
                            null,
                            2,
                          )}
                        </pre>
                      </details>
                    </td>
                    <td>
                      {o.type === "publication" ? (
                        <PublicationReconcileButton
                          operation={o}
                          onDone={refresh}
                        />
                      ) : (
                        <button
                          className="button"
                          disabled={
                            o.billing_status !== "reconciliation_pending"
                          }
                          onClick={() => open("reconcile", undefined, o)}
                        >
                          Reconciliar
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {ops.items.length === 0 && (
              <p className="muted">Nenhuma operação encontrada.</p>
            )}
          </div>
        )}
        {tab === "audit" && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Data</th>
                  <th>Ação</th>
                  <th>Operador</th>
                  <th>Recurso</th>
                  <th>Motivo</th>
                </tr>
              </thead>
              <tbody>
                {audit.items.map((a) => (
                  <tr key={a.id}>
                    <td>{date(a.created_at)}</td>
                    <td>{a.action}</td>
                    <td>{a.actor_id}</td>
                    <td>{a.resource_id}</td>
                    <td>{a.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {tab === "status" && status && (
          <>
            <div className="admin-status">
              {[
                ["IA habilitada", status.ai_enabled],
                ["Pausa após restore", status.restore_hold],
                ["Rotina de lifecycle ativa", status.lifecycle_enabled],
                ["Publicação validada", status.publication_enabled],
              ].map(([name, value]) => (
                <section className="panel" key={String(name)}>
                  <h2>{String(name)}</h2>
                  <span className="badge">{value ? "Sim" : "Não"}</span>
                </section>
              ))}
            </div>
            <section className="panel">
              <h2>Inventário de expurgo</h2>
              <p className="muted">
                A remoção de fontes locais não comprova eliminação de backups
                externos. Registre evidências específicas antes de confirmar o
                expurgo integral.
              </p>
              {status.purges.map((p) => (
                <div
                  className="operation-row"
                  key={p.account_id + p.destination}
                >
                  <div>
                    <strong>{p.destination}</strong>
                    <p className="small">
                      {p.account_id} · {p.state}
                    </p>
                    <pre>{JSON.stringify(p.evidence)}</pre>
                  </div>
                  {p.state === "requires_operator_evidence" && (
                    <button
                      className="button"
                      onClick={() => open("purge", undefined, undefined, p)}
                    >
                      Registrar evidência
                    </button>
                  )}
                </div>
              ))}
            </section>
          </>
        )}
        <div className="actions pagination">
          {cursor && (
            <button className="button" onClick={() => setCursor("")}>
              Primeira página
            </button>
          )}
          {nextCursor && (
            <button className="button" onClick={() => setCursor(nextCursor)}>
              Próxima página
            </button>
          )}
        </div>
      </main>
      {modal && (
        <AdminDialog
          title={titles[modal.action]}
          {...{
            modal,
            setModal,
            busy,
            setReview,
            setError,
            setDraft,
            error,
            open,
            wallet,
            review,
            execute,
            formRef,
            prepare,
            draft,
          }}
        />
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
