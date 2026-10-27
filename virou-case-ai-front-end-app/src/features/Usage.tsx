import { useSession } from "../app/session";
import { api, date, percent, states } from "../shared/api";
import { ErrorNotice, Loading, useLoad, WhatsApp } from "../shared/ui";
import type { Offer, Wallet } from "../shared/types";

export function Usage() {
  const { user } = useSession(),
    { data, error } = useLoad(() =>
      Promise.all([api<Wallet>("/wallet"), api<Offer>("/offer")]),
    );
  const [wallet, offer] = data ?? [];
  return (
    <>
      <div className="page-heading">
        <div>
          <p className="eyebrow">CLAREZA EM CADA ETAPA</p>
          <h1>Uso e acesso</h1>
          <p>Acompanhe seu período e seu uso de IA.</p>
        </div>
      </div>
      <ErrorNotice error={error} />
      {!data && !error && <Loading />}
      {wallet && offer && (
        <>
          <div className="usage-grid">
            <section className="panel">
              <div className="card-title">
                <h2>Experimentação</h2>
                <span className="badge">
                  {states[user?.access.status ?? "pending"]}
                </span>
              </div>
              <p className="price">
                {new Intl.NumberFormat("pt-BR", {
                  style: "currency",
                  currency: offer.currency,
                }).format(Number(offer.amount_minor) / 100)}
                <span> por {offer.access_days} dias</span>
              </p>
              <p className="muted">Renovação manual</p>
            </section>
            <section className="panel">
              <h2>Uso de IA</h2>
              <div className="usage-numbers">
                <strong>
                  {percent(wallet.consumed_ratio)} <small>utilizado</small>
                </strong>
                <strong>
                  {percent(wallet.available_ratio)} <small>disponível</small>
                </strong>
              </div>
              <progress
                max={1}
                value={wallet.consumed_ratio}
                aria-label="Uso do período"
              />
              <p className="small muted">
                {percent(wallet.reserved_ratio)} reservado · O saldo não acumula
                na renovação.
              </p>
              {wallet.pending_reconciliation && (
                <p role="status">Há consumo em apuração.</p>
              )}
            </section>
          </div>
          <section className="panel cycle">
            <h2>Seu ciclo de acesso</h2>
            <p className="muted">
              As datas importantes do seu período de experimentação.
            </p>
            <div className="timeline">
              {[
                ["Dia 1", "Ativação", user?.access.starts_at],
                ["Dia 30", "Fim do acesso", user?.access.access_expires_at],
                [
                  "Dia 32",
                  "Fim da hospedagem e retenção",
                  user?.access.purge_at,
                ],
              ].map(([day, title, value], i) => (
                <div key={day} className={i === 0 ? "active" : ""}>
                  <span className="timeline-dot" />
                  <strong>{day}</strong>
                  <span>{title}</span>
                  <time>{date(value)}</time>
                </div>
              ))}
            </div>
          </section>
          <div className="usage-contact">
            <WhatsApp url={offer.whatsapp_url} />
          </div>
        </>
      )}
    </>
  );
}
export function Account() {
  const { user } = useSession();
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Sua conta</h1>
          <p>Acesso pessoal à viroucaseai.</p>
        </div>
      </div>
      <section className="panel account-panel">
        <dl>
          <dt>Nome</dt>
          <dd>{user?.name}</dd>
          <dt>Login</dt>
          <dd>{user?.login}</dd>
          <dt>Acesso</dt>
          <dd>{states[user?.access.status ?? "pending"]}</dd>
        </dl>
        <p className="muted">
          Para redefinir sua senha, fale com o responsável pelo seu acesso.
        </p>
        <WhatsApp />
      </section>
    </>
  );
}
