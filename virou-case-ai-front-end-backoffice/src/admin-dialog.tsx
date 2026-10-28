import type { Dispatch, SetStateAction, RefObject, FormEvent } from "react";
import { brl, date } from "./api";
import { PasswordField } from "./password-field";
import { ErrorNotice, Dialog } from "./admin-ui";
import type {
  AdminModal,
  Account,
  Operation,
  Purge,
  Wallet,
} from "./admin-types";
type Setter<T> = Dispatch<SetStateAction<T>>;
type Props = {
  modal: AdminModal;
  setModal: Setter<AdminModal>;
  title: string;
  busy: boolean;
  setReview: Setter<Record<string, unknown> | null>;
  setError: Setter<unknown>;
  setDraft: Setter<Record<string, string>>;
  error: unknown;
  open: (
    action: string,
    account?: Account,
    op?: Operation,
    purge?: Purge,
  ) => void;
  wallet: Wallet | null;
  review: Record<string, unknown> | null;
  execute: () => Promise<void>;
  formRef: RefObject<HTMLFormElement | null>;
  prepare: (e: FormEvent<HTMLFormElement>) => void;
  draft: Record<string, string>;
};
export function AdminDialog({
  modal,
  setModal,
  title,
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
}: Props) {
  return (
    <>
      {modal && (
        <Dialog
          title={title}
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
    </>
  );
}
