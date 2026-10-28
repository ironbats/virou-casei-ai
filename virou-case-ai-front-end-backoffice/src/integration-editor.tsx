import { useState } from "react";
import { command } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog } from "./integration-ui";
import { IntegrationFields } from "./integration-fields";
import {
  providerLabels,
  type Connection,
  type Provider,
} from "./integration-types";

export function IntegrationEditor({
  provider,
  current,
  defaults,
  close,
  saved,
}: {
  provider: Provider;
  current: Connection | null;
  defaults: Record<string, unknown>;
  close: () => void;
  saved: (name: string) => void;
}) {
  const initial =
    current?.settings ||
    (provider === "jev"
      ? defaults
      : { spa: false, allow_paid: false, allowed_stacks: [] });
  const [settings, setSettings] = useState<Record<string, unknown>>(initial);
  const [name, setName] = useState(current?.name || "");
  const [token, setToken] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dirty =
    name !== (current?.name || "") ||
    !!token ||
    !!reason ||
    JSON.stringify(settings) !== JSON.stringify(initial);
  return (
    <IntegrationDialog
      title={`${current ? "Editar conexão" : "Nova conexão"} ${providerLabels[provider]}`}
      close={close}
      dirty={dirty}
      busy={busy}
    >
      <p className="field-help">
        {current
          ? `Uma nova versão de ${current.name} exigirá validação e ativação. As demais conexões permanecem disponíveis.`
          : "Cadastre uma conta com nome próprio. Após salvar, valide e ative a conexão em Gerenciar."}
      </p>
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy) return;
          setError(null);
          if (
            provider === "cloudflare" &&
            !(settings.allowed_stacks as string[])?.length
          ) {
            setError("Selecione ao menos um perfil permitido.");
            return;
          }
          setBusy(true);
          try {
            await command(
              `/integrations/${provider}/connections${current ? "/" + current.connection_id : ""}`,
              { name: name.trim(), settings, token: token || null, reason },
              current ? "PUT" : "POST",
              current?.version,
            );
            setToken("");
            saved(name.trim());
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <ErrorNotice error={error} />
        <fieldset className="form-fieldset" disabled={busy}>
          <label>
            Nome da conexão
            <input
              autoFocus
              required
              minLength={3}
              maxLength={80}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={
                provider === "cloudflare"
                  ? "Ex.: Cloudflare produção"
                  : "Ex.: Jev principal"
              }
            />
          </label>
          <IntegrationFields
            provider={provider}
            settings={settings}
            set={(key, value) =>
              setSettings((old) => ({ ...old, [key]: value }))
            }
          />
          <fieldset className="integration-section">
            <legend>Credencial</legend>
            <label>
              {current?.has_token
                ? "Substituir token (deixe vazio para preservar)"
                : "Token da integração"}
              <input
                type="password"
                autoComplete="new-password"
                value={token}
                minLength={10}
                maxLength={4096}
                required={
                  !current?.has_token &&
                  (provider === "cloudflare" ||
                    !!settings.recommendation_enabled)
                }
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
            <p className="field-help">
              {current?.has_token
                ? "Token cadastrado. Deixar vazio preserva apenas o token desta conexão."
                : "O token será armazenado de forma protegida e não será exibido após salvar."}
            </p>
          </fieldset>
          <label>
            Motivo da alteração
            <textarea
              required
              minLength={8}
              maxLength={300}
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
        </fieldset>
        <div className="integration-footer">
          <span className="field-help">
            {current
              ? `Editando a versão ${current.version}`
              : "Nova conexão independente"}
          </span>
          <button className="button primary" disabled={busy}>
            {busy
              ? "Salvando…"
              : current
                ? "Salvar nova versão"
                : "Cadastrar conexão"}
          </button>
        </div>
      </form>
    </IntegrationDialog>
  );
}
