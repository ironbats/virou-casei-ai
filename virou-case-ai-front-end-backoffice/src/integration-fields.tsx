import type { Provider } from "./integration-types";

export function IntegrationFields({
  provider,
  settings,
  set,
}: {
  provider: Provider;
  settings: Record<string, unknown>;
  set: (name: string, value: unknown) => void;
}) {
  function field(
    name: string,
    label: string,
    type = "text",
    min?: number,
    max?: number,
    pattern?: string,
  ) {
    return (
      <label>
        {label}
        <input
          type={type}
          value={String(settings[name] ?? "")}
          required
          min={min}
          max={max}
          pattern={pattern}
          onChange={(e) =>
            set(
              name,
              type === "number" && e.target.value !== ""
                ? Number(e.target.value)
                : e.target.value,
            )
          }
        />
      </label>
    );
  }
  function checkbox(name: string, label: string) {
    return (
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={!!settings[name]}
          onChange={(e) => set(name, e.target.checked)}
        />
        {label}
      </label>
    );
  }
  return provider === "cloudflare" ? (
    <>
      <fieldset className="integration-section">
        <legend>Conta e hospedagem</legend>
        <div className="form-grid">
          {field(
            "account_id",
            "Account ID Cloudflare",
            "text",
            undefined,
            undefined,
            "[a-fA-F0-9]{32}",
          )}
          {field(
            "environment",
            "Ambiente",
            "text",
            undefined,
            undefined,
            "[a-z0-9-]{2,40}",
          )}
          {field(
            "worker_prefix",
            "Prefixo dos projetos",
            "text",
            undefined,
            undefined,
            "[a-z][a-z0-9-]{1,17}",
          )}
          <label>
            Tipo do token
            <select
              required
              value={String(settings.token_type || "")}
              onChange={(e) => set("token_type", e.target.value)}
            >
              <option value="">Selecione</option>
              <option value="user">Token de usuário</option>
              <option value="account">Token de conta</option>
            </select>
          </label>
          {field("compatibility_date", "Data de compatibilidade", "date")}
          <label>
            Plano permitido
            <select
              required
              value={String(settings.plan || "")}
              onChange={(e) => {
                set("plan", e.target.value);
                set("allow_paid", false);
              }}
            >
              <option value="">Selecione</option>
              <option value="free">Free — sem upgrade automático</option>
            </select>
          </label>
        </div>
        <p className="field-help">
          O Account ID tem 32 caracteres e está disponível no painel da
          Cloudflare. Use um nome de ambiente como producao ou homologacao.
        </p>
        {checkbox("spa", "Frontend com navegação SPA")}
        <fieldset className="integration-stack">
          <legend>Perfis permitidos (selecione ao menos um)</legend>
          {[
            ["hono-typescript-d1", "Hono / TypeScript / D1"],
            ["fastapi-python-d1", "FastAPI / Python / D1"],
          ].map(([id, label]) => (
            <label key={id} className="checkbox-label">
              <input
                type="checkbox"
                checked={((settings.allowed_stacks as string[]) || []).includes(
                  id,
                )}
                onChange={(e) =>
                  set(
                    "allowed_stacks",
                    e.target.checked
                      ? [...((settings.allowed_stacks as string[]) || []), id]
                      : ((settings.allowed_stacks as string[]) || []).filter(
                          (v) => v !== id,
                        ),
                  )
                }
              />
              {label}
            </label>
          ))}
        </fieldset>
      </fieldset>
      <fieldset className="integration-section">
        <legend>Capacidade e validação</legend>
        <div className="form-grid">
          {field(
            "max_projects",
            "Projetos hospedados na conta (máximo 10)",
            "number",
            1,
            10,
          )}
          {field("max_concurrent", "Publicações simultâneas", "number", 1, 5)}
          {field(
            "validation_hours",
            "Validade da validação (horas)",
            "number",
            1,
            168,
          )}
          {field(
            "timeout_seconds",
            "Timeout Cloudflare (segundos)",
            "number",
            5,
            120,
          )}
        </div>
      </fieldset>
    </>
  ) : (
    <>
      <fieldset className="integration-section">
        <legend>Recomendador Jev</legend>
        <div className="form-grid">
          {field(
            "model",
            "Modelo Jev",
            "text",
            undefined,
            undefined,
            "jev-[a-zA-Z0-9.\\-]+",
          )}
          {field("timeout_seconds", "Timeout Jev (segundos)", "number", 1, 30)}
          {field(
            "daily_requests",
            "Limite diário por usuário",
            "number",
            1,
            1000,
          )}
          <label>
            Confiança mínima
            <input
              type="number"
              step="any"
              min="0"
              max="1"
              required
              value={String(settings.min_confidence ?? "")}
              onChange={(e) =>
                set(
                  "min_confidence",
                  e.target.value === "" ? "" : Number(e.target.value),
                )
              }
            />
          </label>
        </div>
        {checkbox(
          "recommendation_enabled",
          "Habilitar chamadas ao Jev após ativar",
        )}
        <p className="field-help">
          Somente a conexão padrão fornece os limites do fluxo guiado e as
          recomendações. Chamadas externas exigem token e ativação.
        </p>
      </fieldset>
      <fieldset className="integration-section">
        <legend>Limites do fluxo guiado</legend>
        <div className="form-grid">
          {field(
            "max_objective_chars",
            "Limite de caracteres da ideia",
            "number",
            100,
            12000,
          )}
          {field(
            "max_message_chars",
            "Limite de caracteres da mensagem",
            "number",
            100,
            12000,
          )}
          {field("max_attachments", "Máximo de anexos", "number", 1, 20)}
          {field(
            "max_attachment_chars",
            "Limite por anexo",
            "number",
            100,
            24000,
          )}
          {field(
            "max_context_bytes",
            "Contexto máximo em bytes",
            "number",
            10000,
            60000,
          )}
          {field(
            "autosave_ms",
            "Intervalo de salvamento (ms)",
            "number",
            300,
            5000,
          )}
        </div>
      </fieldset>
    </>
  );
}
