import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";
import {
  ClipboardList,
  Cpu,
  Gauge,
  LogOut,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import { api, brl, command, date, session } from "./api";
import "./styles.css";
import "./admin.css";
import { PasswordField } from "./password-field";
import { AISettings } from "./ai-settings";

type User = { id: string; name: string; csrf_token: string };
type Account = {
  id: string;
  name: string;
  login: string;
  access: {
    status: string;
    entitlement_version?: number;
    starts_at?: string;
    access_expires_at?: string;
    purge_at?: string;
  };
  suspended: boolean;
};
type Page<T> = { items: T[]; next_cursor?: string | null };
type Operation = {
  id: string;
  state: string;
  account_id: string;
  type: string;
  billing_status: string;
  model_label?: string;
  policy: Record<string, unknown>;
  attempt?: {
    state: string;
    usage: unknown;
    actual_brl: string;
    external_id: string;
  };
};
type Audit = {
  id: string;
  action: string;
  actor_id: string;
  resource_id: string;
  reason: string;
  created_at: string;
};
type Purge = {
  account_id: string;
  fence: string;
  destination: string;
  state: string;
  evidence: unknown;
};
type Status = {
  ai_enabled: boolean;
  restore_hold: boolean;
  lifecycle_enabled: boolean;
  publication_enabled: boolean;
  purges: Purge[];
};
type Wallet = {
  available_brl: string;
  reserved_brl: string;
  consumed_brl: string;
  grants: Record<string, string | boolean>[];
};
function Brand() {
  return (
    <span className="brand">
      <span className="brand-mark">v</span>viroucaseai
    </span>
  );
}
function ErrorNotice({ error }: { error: unknown }) {
  return error ? (
    <div role="alert" className="notice error">
      {error instanceof Error ? error.message : String(error)}
    </div>
  ) : null;
}
function Dialog({
  title,
  children,
  close,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      aria-label={title}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button className="icon-button" onClick={close} aria-label="Fechar">
          <X />
        </button>
      </div>
      {children}
    </dialog>
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
  const [modal, setModal] = useState<{
      action: string;
      account?: Account;
      op?: Operation;
      purge?: Purge;
    } | null>(null),
    [review, setReview] = useState<Record<string, unknown> | null>(null),
    [wallet, setWallet] = useState<Wallet | null>(null);
  const [aiRefresh, setAiRefresh] = useState(0);
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
    if (tab === "ai") setAiRefresh((value) => value + 1);
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
          [Gauge, "operations", "Operações"],
          [ClipboardList, "audit", "Auditoria"],
          [ShieldCheck, "status", "Estado operacional"],
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
      <main id="main" className="admin-main">
        <div className="page-heading">
          <div>
            <p className="eyebrow">VIROUCASEAI · OPERAÇÃO MANUAL</p>
            <h1>
              {tab === "accounts"
                ? "Contas e acesso"
                : tab === "operations"
                  ? "Operações"
                  : tab === "audit"
                    ? "Auditoria"
                    : tab === "ai"
                      ? "Agentes de IA"
                      : "Estado operacional"}
            </h1>
            <p>
              Pagamentos externos, decisões explícitas e histórico preservado.
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
        {tab === "ai" && <AISettings refreshToken={aiRefresh} />}
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
                      <button
                        className="button"
                        disabled={o.billing_status !== "reconciliation_pending"}
                        onClick={() => open("reconcile", undefined, o)}
                      >
                        Reconciliar
                      </button>
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
        <Dialog
          title={titles[modal.action]}
          close={() => {
            if (!busy) {
              setModal(null);
              setReview(null);
              setError(null);
              setDraft({});
            }
          }}
        >
          <ErrorNotice error={error} />
          {modal.action === "settings" ? (
            <div className="form">
              <p>
                Escolha o que deseja ajustar para{" "}
                <strong>{modal.account?.name}</strong>.
              </p>
              <button
                className="button primary"
                onClick={() => open("access-adjust", modal.account)}
              >
                Período de acesso
              </button>
              <button
                className="button"
                onClick={() => open("adjust", modal.account)}
              >
                Saldo de uso de IA
              </button>
              <p className="small muted">
                Alterar o prazo mantém o saldo e o histórico. Para iniciar um
                novo ciclo com pagamento, use Ativar / renovar.
              </p>
            </div>
          ) : modal.action === "wallet" ? (
            wallet ? (
              <>
                <div className="wallet-values">
                  <p>
                    Disponível<strong>{brl(wallet.available_brl)}</strong>
                  </p>
                  <p>
                    Reservado<strong>{brl(wallet.reserved_brl)}</strong>
                  </p>
                  <p>
                    Consumido<strong>{brl(wallet.consumed_brl)}</strong>
                  </p>
                </div>
                {wallet.grants.map((g) => (
                  <details key={String(g.id)}>
                    <summary>
                      {g.kind} · {g.closed ? "Ciclo encerrado" : "Ciclo atual"}
                    </summary>
                    <dl>
                      {Object.entries(g).map(([k, v]) => (
                        <div key={k}>
                          <dt>{k}</dt>
                          <dd>{String(v)}</dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                ))}
              </>
            ) : (
              <p>Carregando saldo…</p>
            )
          ) : review ? (
            <>
              <h3>Confira antes de confirmar</h3>
              {modal.action === "grant" && (
                <div className="notice">
                  <span>
                    Novo ciclo: {date(String(review.starts_at))} até{" "}
                    {date(
                      new Date(
                        new Date(String(review.starts_at)).getTime() +
                          Number(review.access_days) * 86400000,
                      ).toISOString(),
                    )}
                    . Arquivos para download até{" "}
                    {date(
                      new Date(
                        new Date(String(review.starts_at)).getTime() +
                          (Number(review.access_days) + 2) * 86400000,
                      ).toISOString(),
                    )}
                    . O saldo livre anterior não acumula.
                  </span>
                </div>
              )}
              {modal.action === "topup" && (
                <p>A recarga não altera as datas de acesso ou hospedagem.</p>
              )}
              <dl className="review-list">
                {Object.entries(review)
                  .filter(
                    ([k]) =>
                      !["password", "expected_version", "starts_at"].includes(
                        k,
                      ),
                  )
                  .map(([k, v]) => (
                    <div key={k}>
                      <dt>
                        {(
                          {
                            name: "Nome",
                            login: "Login",
                            access_days: "Dias de acesso",
                            expires_at: "Uso da plataforma até",
                            reason: "Motivo",
                            reference: "Referência do pagamento",
                            amount_paid_brl: "Pagamento (R$)",
                            api_credit_brl: "Crédito de IA (R$)",
                            amount_brl: "Ajuste de saldo (R$)",
                          } as Record<string, string>
                        )[k] ?? k}
                      </dt>
                      <dd>
                        {k === "expires_at"
                          ? date(String(v))
                          : typeof v === "object"
                            ? JSON.stringify(v)
                            : String(v)}
                      </dd>
                    </div>
                  ))}
                {"password" in review && (
                  <p>Senha definida; seu valor não é exibido nesta revisão.</p>
                )}
              </dl>
              <div className="actions">
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => setReview(null)}
                >
                  Revisar campos
                </button>
                <button
                  className="button primary"
                  disabled={busy}
                  onClick={execute}
                >
                  {busy ? "Registrando…" : "Confirmar e registrar"}
                </button>
              </div>
            </>
          ) : (
            <form
              className="form account-form"
              ref={formRef}
              onSubmit={prepare}
            >
              {modal.account && (
                <p>
                  <strong>{modal.account.name}</strong> · {modal.account.login}
                </p>
              )}
              {modal.action === "create" && (
                <>
                  <label>
                    Nome
                    <input name="name" required maxLength={100} />
                  </label>
                  <label>
                    Login
                    <input
                      name="login"
                      required
                      minLength={3}
                      maxLength={160}
                      pattern="[a-zA-Z0-9@._+\-]+"
                    />
                  </label>
                </>
              )}
              {["create", "reset"].includes(modal.action) && (
                <PasswordField initial={draft.password} />
              )}
              {modal.action === "grant" && (
                <>
                  <div className="notice">
                    <strong>Começa agora</strong>
                    <p>
                      Defina por quantos dias a conta poderá usar a plataforma.
                      Após o vencimento, haverá 2 dias adicionais apenas para
                      consultar e baixar arquivos.
                    </p>
                  </div>
                  <label>
                    Período de acesso (dias)
                    <input
                      name="access_days"
                      type="number"
                      min="1"
                      max="3650"
                      step="1"
                      required
                      defaultValue="30"
                    />
                  </label>
                  <p className="small muted">
                    Pagamento do ciclo: R$ 75,00. Crédito de IA padrão: R$
                    50,00. Renovar inicia um novo ciclo; o saldo livre anterior
                    não acumula.
                  </p>
                </>
              )}
              {["grant", "topup"].includes(modal.action) && (
                <details
                  className="payment-details"
                  open={modal.action === "topup"}
                >
                  <summary>Dados do pagamento e crédito de IA</summary>
                  <div className="form">
                    <label>
                      Valor pago em reais
                      <input
                        name="amount_paid_brl"
                        type="number"
                        min="0.01"
                        step="0.01"
                        required
                        defaultValue={modal.action === "grant" ? "75" : ""}
                        readOnly={modal.action === "grant"}
                      />
                    </label>
                    <label>
                      Crédito de IA em reais
                      <input
                        name="api_credit_brl"
                        type="number"
                        min="0.00000001"
                        step="0.00000001"
                        defaultValue={modal.action === "grant" ? "50" : ""}
                        required
                      />
                    </label>
                    <label>
                      Referência única do pagamento
                      <input
                        name="reference"
                        minLength={4}
                        maxLength={160}
                        required
                        defaultValue={"REF-" + crypto.randomUUID()}
                      />
                      <small>
                        Identificador criado automaticamente. Você pode
                        substituí-lo pelo código do comprovante, com pelo menos
                        4 caracteres.
                      </small>
                    </label>
                  </div>
                </details>
              )}
              {modal.action === "access-adjust" && (
                <>
                  <p className="notice">
                    Defina a data e hora até as quais o usuário poderá usar a
                    plataforma. O saldo não muda. Os arquivos ficam disponíveis
                    por mais 2 dias após essa data.
                  </p>
                  <label>
                    Uso da plataforma até
                    <input
                      name="expires_at"
                      type="datetime-local"
                      required
                      defaultValue={(() => {
                        const d = new Date(
                          modal.account?.access.access_expires_at ??
                            Date.now() + 30 * 86400000,
                        );
                        return new Date(
                          d.getTime() - d.getTimezoneOffset() * 60000,
                        )
                          .toISOString()
                          .slice(0, 16);
                      })()}
                    />
                  </label>
                  {modal.account?.suspended && (
                    <p className="notice">
                      Esta conta está suspensa. Ajustar o prazo mantém a
                      suspensão.
                    </p>
                  )}
                </>
              )}
              {modal.action === "adjust" && (
                <>
                  <p>
                    Ajustes negativos só afetam saldo livre. Reservas incertas
                    precisam de reconciliação própria.
                  </p>
                  <label>
                    Valor do ajuste em reais
                    <input
                      name="amount_brl"
                      type="number"
                      step="0.00000001"
                      required
                    />
                  </label>
                </>
              )}
              {modal.action === "reconcile" && (
                <>
                  <p>
                    Use somente evidência de uso final do provedor ou
                    comprovação de ausência de efeito. Timeout sozinho não
                    permite liberar a reserva.
                  </p>
                  <label className="check-row">
                    <input name="confirmed_no_effect" type="checkbox" />
                    Ausência de efeito comprovada
                  </label>
                  <div className="usage-inputs">
                    <label>
                      Tokens de entrada
                      <input
                        name="input"
                        type="number"
                        min="0"
                        defaultValue="0"
                      />
                    </label>
                    <label>
                      Entrada em cache
                      <input
                        name="cached"
                        type="number"
                        min="0"
                        defaultValue="0"
                      />
                    </label>
                    <label>
                      Tokens de saída faturados
                      <input
                        name="output"
                        type="number"
                        min="0"
                        defaultValue="0"
                      />
                    </label>
                  </div>
                </>
              )}
              {["reconcile", "purge"].includes(modal.action) && (
                <label>
                  Referência da evidência
                  <input
                    name="evidence_reference"
                    required
                    minLength={10}
                    maxLength={300}
                  />
                </label>
              )}
              <label>
                Motivo
                <input
                  name="reason"
                  required
                  minLength={8}
                  maxLength={300}
                  defaultValue={
                    modal.action === "grant"
                      ? "Ativação ou renovação de acesso"
                      : modal.action === "create"
                        ? "Criação de usuário pelo administrador"
                        : modal.action === "access-adjust"
                          ? "Ajuste do prazo de acesso da conta"
                          : ""
                  }
                />
              </label>
              <button className="button primary">Revisar operação</button>
            </form>
          )}
        </Dialog>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
