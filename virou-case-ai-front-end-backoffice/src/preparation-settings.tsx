import { useId, useState } from "react";
import { Settings2 } from "lucide-react";
import { date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { useAdminPolicy } from "./admin-policy-editor";
import { IntegrationDialog, useRemote } from "./integration-ui";
import "./integration-settings.css";
import "./observability.css";
import {
  fieldConstraints,
  NumberSetting,
  type AgentConfiguration,
} from "./agent-configuration-fields";

type PreparationSettingsValue = {
  enabled: boolean;
  model_id: string | null;
  max_context_bytes: number | string;
  max_output_tokens: number | string;
  max_repairs: number;
};
type PreparationPolicy = {
  version: number;
  settings: PreparationSettingsValue;
  updated_at: string;
  available_models: {
    id: string;
    connection_id: string;
    provider: string;
    model: string;
    name: string;
    ready: boolean;
  }[];
};

export function PreparationSettings({
  refreshToken,
}: {
  refreshToken: number;
}) {
  const remote = useRemote<PreparationPolicy>(
    "/template-preparation-policy",
    refreshToken,
  );
  const configuration = useRemote<AgentConfiguration>(
    "/ai/configuration-schema",
    refreshToken,
  );
  const [editor, setEditor] = useState<PreparationPolicy | null>(null);
  const chosen = remote.data?.available_models.find(
    (model) => model.id === remote.data?.settings.model_id,
  );
  return (
    <section
      className="panel preparation-settings"
      aria-label="Preenchimento de templates"
    >
      <div className="integration-intro">
        <div>
          <h2>Preenchimento de templates</h2>
          <p>
            Define a preparação de sugestões antes da revisão humana e da
            construção.
          </p>
        </div>
        <button
          className="button"
          disabled={
            !remote.data ||
            remote.loading ||
            !configuration.data ||
            configuration.loading ||
            !!configuration.error
          }
          onClick={() => setEditor(remote.data)}
        >
          <Settings2 size={16} />
          Configurar preenchimento
        </button>
      </div>
      <ErrorNotice error={remote.error || configuration.error} />
      {!!(remote.error || configuration.error) && (
        <button
          className="button"
          onClick={() => {
            remote.refresh();
            configuration.refresh();
          }}
        >
          Tentar carregar configuração
        </button>
      )}
      {remote.loading && !remote.data && (
        <p role="status">Consultando configuração…</p>
      )}
      {remote.data && (
        <dl className="integration-summary">
          <div>
            <dt>Disponibilidade</dt>
            <dd>
              {remote.data.settings.enabled ? "Habilitado" : "Desabilitado"}
            </dd>
          </div>
          <div>
            <dt>Seleção de modelo</dt>
            <dd>
              {remote.data.settings.model_id
                ? chosen
                  ? `${chosen.name} · ${chosen.provider}`
                  : "Modelo configurado indisponível"
                : "Automática pela configuração autorizada"}
            </dd>
          </div>
          <div>
            <dt>Limites por preparação</dt>
            <dd>
              {remote.data.settings.max_output_tokens} tokens de saída ·{" "}
              {remote.data.settings.max_repairs} reparação
            </dd>
          </div>
          <div>
            <dt>Versão da política</dt>
            <dd>
              v{remote.data.version} · {date(remote.data.updated_at)}
            </dd>
          </div>
        </dl>
      )}
      {editor && configuration.data && (
        <PreparationEditor
          initial={editor}
          configuration={configuration.data}
          close={() => setEditor(null)}
          saved={remote.refresh}
        />
      )}
    </section>
  );
}

function PreparationEditor({
  initial,
  configuration,
  close,
  saved,
}: {
  initial: PreparationPolicy;
  configuration: AgentConfiguration;
  close: () => void;
  saved: () => void;
}) {
  const reasonId = useId();
  const form = useAdminPolicy(
    "/template-preparation-policy",
    initial,
    (result) => result.settings,
    (settings, reason) => ({ settings, reason }),
    saved,
  );
  const set = <K extends keyof PreparationSettingsValue>(
    key: K,
    value: PreparationSettingsValue[K],
  ) => form.setValue((current) => ({ ...current, [key]: value }));
  const models = form.current.available_models;
  const fields = configuration.preparation.schema.properties;
  const repairs = fieldConstraints(fields.max_repairs);
  const selectedModel = configuration.available_models.find(
    (model) => model.id === form.value.model_id,
  );
  return (
    <IntegrationDialog
      title="Configurar preenchimento"
      close={close}
      dirty={form.dirty}
      busy={form.busy}
    >
      <form className="form policy-form" onSubmit={form.submit}>
        <p className="small muted">
          Política v{form.current.version}. A preparação cria sugestões
          revisáveis; a publicação e a construção continuam exigindo suas ações
          próprias.
        </p>
        <ErrorNotice error={form.error} />
        {!!form.error && (
          <p className="small">
            Os valores digitados foram mantidos. Se outra pessoa alterou a
            versão, feche e reabra a configuração para consultar os dados atuais
            antes de reaplicar suas mudanças.
          </p>
        )}
        {form.message && (
          <p className="notice" role="status">
            {form.message}
          </p>
        )}
        <fieldset className="integration-section" disabled={form.busy}>
          <legend>Disponibilidade e modelo</legend>
          <label className="policy-checkbox">
            <input
              type="checkbox"
              checked={form.value.enabled}
              onChange={(event) => set("enabled", event.target.checked)}
            />
            Habilitar preenchimento assistido
          </label>
          <label>
            Modelo para o preenchimento
            <select
              value={form.value.model_id || ""}
              onChange={(event) => set("model_id", event.target.value || null)}
            >
              <option value="">Automático · configuração autorizada</option>
              {form.value.model_id &&
                !models.some((model) => model.id === form.value.model_id) && (
                  <option value={form.value.model_id} disabled>
                    Modelo configurado indisponível
                  </option>
                )}
              {models.map((model) => (
                <option key={model.id} value={model.id} disabled={!model.ready}>
                  {model.name} · {model.provider} · {model.model}
                  {model.ready ? "" : " · indisponível"}
                </option>
              ))}
            </select>
          </label>
          <p className="field-help">
            Somente conexões e modelos cadastrados são utilizados. Sem um modelo
            válido, a preparação fica indisponível e nenhuma chamada é
            realizada.
          </p>
          {form.value.model_id && (
            <p className="field-help">
              Conexão:{" "}
              {models.find((model) => model.id === form.value.model_id)
                ?.connection_id || "Não disponível"}
            </p>
          )}
        </fieldset>
        <fieldset className="integration-section" disabled={form.busy}>
          <legend>Limites da preparação</legend>
          <div className="form-grid">
            <NumberSetting
              label="Contexto máximo (bytes)"
              field={fields.max_context_bytes}
              value={form.value.max_context_bytes}
              onChange={(value) => set("max_context_bytes", value ?? "")}
            />
            <NumberSetting
              label="Saída máxima (tokens)"
              field={fields.max_output_tokens}
              value={form.value.max_output_tokens}
              onChange={(value) => set("max_output_tokens", value ?? "")}
              help={
                selectedModel
                  ? `A saída efetiva também respeita o limite de ${selectedModel.max_output_tokens.toLocaleString("pt-BR")} tokens do modelo selecionado.`
                  : "A saída efetiva também respeita o limite do modelo autorizado utilizado na preparação."
              }
            />
            <label>
              Reparações permitidas
              <select
                value={form.value.max_repairs}
                onChange={(event) =>
                  set("max_repairs", Number(event.target.value))
                }
              >
                {Array.from(
                  {
                    length:
                      (repairs.maximum as number) -
                      (repairs.minimum as number) +
                      1,
                  },
                  (_, index) => index + (repairs.minimum as number),
                ).map((value) => (
                  <option key={value} value={value}>
                    {value === 0
                      ? "Nenhuma reparação"
                      : `Até ${value} ${value === 1 ? "reparação" : "reparações"}`}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="field-help">
            A reserva considera o limite de reparação configurado. Uma nova
            chamada pode gerar consumo.
          </p>
        </fieldset>
        <div className="policy-reason">
          <label htmlFor={reasonId}>Motivo da alteração</label>
          <textarea
            id={reasonId}
            required
            minLength={8}
            maxLength={300}
            value={form.reason}
            disabled={form.busy}
            onChange={(event) => form.setReason(event.target.value)}
            rows={3}
          />
        </div>
        <div className="integration-footer">
          <span>Alterações versionadas e auditadas.</span>
          <button
            type="submit"
            className="button primary"
            disabled={
              form.busy || !form.changed || form.reason.trim().length < 8
            }
          >
            {form.busy ? "Salvando…" : "Salvar política de preenchimento"}
          </button>
        </div>
      </form>
    </IntegrationDialog>
  );
}
