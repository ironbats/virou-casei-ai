import { useId, useState } from "react";
import { Settings2 } from "lucide-react";
import { ErrorNotice } from "./admin-ui";
import { useAdminPolicy } from "./admin-policy-editor";
import { IntegrationDialog, useRemote } from "./integration-ui";
import { stateLabels } from "./integration-types";
import {
  ModelSelection,
  NumberSetting,
  type AgentConfiguration,
} from "./agent-configuration-fields";

type GenerationPolicy = {
  version: number;
  state: string;
  settings: Record<string, unknown>;
  defaults: Record<string, unknown>;
};

export function GenerationSettings({ refreshToken }: { refreshToken: number }) {
  const remote = useRemote<GenerationPolicy>("/integrations/jev", refreshToken);
  const configuration = useRemote<AgentConfiguration>(
    "/ai/configuration-schema",
    refreshToken,
  );
  const [editor, setEditor] = useState<GenerationPolicy | null>(null);
  const settings = {
    ...configuration.data?.construction.defaults,
    ...remote.data?.defaults,
    ...remote.data?.settings,
  };
  const chosen = configuration.data?.available_models.find(
    (model) => model.id === settings.generation_model_id,
  );
  const error = remote.error || configuration.error;
  return (
    <section
      className="panel generation-settings"
      aria-label="Construção de código"
    >
      <div className="integration-intro">
        <div>
          <h2>Construção de código</h2>
          <p>
            Controle o modelo, o contexto e as recuperações da construção e das
            respostas ao agente.
          </p>
        </div>
        <button
          className="button"
          disabled={
            !remote.data ||
            !configuration.data ||
            remote.loading ||
            configuration.loading ||
            !!error
          }
          onClick={() => setEditor(remote.data)}
        >
          <Settings2 size={16} /> Configurar construção
        </button>
      </div>
      <ErrorNotice error={error} />
      {!!error && (
        <button
          className="button"
          onClick={() => {
            remote.refresh();
            configuration.refresh();
          }}
        >
          Tentar carregar parâmetros da construção
        </button>
      )}
      {(remote.loading || configuration.loading) && !remote.data && (
        <p role="status">Consultando parâmetros da construção…</p>
      )}
      {remote.data && configuration.data && (
        <dl className="integration-summary">
          <div>
            <dt>Modelo da construção</dt>
            <dd>
              {settings.generation_model_id
                ? chosen
                  ? `${chosen.name} · ${chosen.provider}`
                  : "Modelo configurado indisponível"
                : "Automático pela configuração autorizada"}
            </dd>
          </div>
          <div>
            <dt>Limite total por operação</dt>
            <dd>{String(settings.max_generation_calls)} chamadas</dd>
          </div>
          <div>
            <dt>Recuperações dentro do limite total</dt>
            <dd>
              {String(settings.max_truncation_retries)} de corte ·{" "}
              {String(settings.max_response_repairs)} de formato
            </dd>
          </div>
          <div>
            <dt>Versão da política</dt>
            <dd>
              v{remote.data.version} ·{" "}
              {stateLabels[remote.data.state] || remote.data.state}
            </dd>
          </div>
        </dl>
      )}
      {editor && configuration.data && (
        <GenerationEditor
          initial={editor}
          configuration={configuration.data}
          close={() => setEditor(null)}
          saved={remote.refresh}
        />
      )}
    </section>
  );
}

function GenerationEditor({
  initial,
  configuration,
  close,
  saved,
}: {
  initial: GenerationPolicy;
  configuration: AgentConfiguration;
  close: () => void;
  saved: () => void;
}) {
  const reasonId = useId();
  const form = useAdminPolicy(
    "/integrations/jev",
    initial,
    (result) => ({
      ...configuration.construction.defaults,
      ...result.defaults,
      ...result.settings,
    }),
    (settings, reason) => ({ settings, reason }),
    saved,
  );
  const set = (key: string, value: unknown) =>
    form.setValue((current) => ({ ...current, [key]: value }));
  const fields = configuration.construction.schema.properties;
  const numeric = (key: string, label: string, help: string) => (
    <NumberSetting
      key={key}
      field={fields[key]}
      label={label}
      value={form.value[key]}
      onChange={(value) => set(key, value)}
      help={help}
    />
  );
  return (
    <IntegrationDialog
      title="Configurar construção"
      close={close}
      dirty={form.dirty}
      busy={form.busy}
    >
      <form className="form policy-form" onSubmit={form.submit}>
        <p className="small muted">
          Política v{form.current.version}. Os parâmetros são aplicados às novas
          operações. A operação em andamento mantém a configuração registrada no
          início.
        </p>
        <ErrorNotice error={form.error} />
        {!!form.error && (
          <p className="small">
            Os valores digitados foram preservados. Em caso de conflito de
            versão, feche e reabra a configuração antes de reaplicar as
            alterações.
          </p>
        )}
        {form.message && (
          <p className="notice" role="status">
            {form.message}
          </p>
        )}
        <fieldset className="integration-section" disabled={form.busy}>
          <legend>Modelo e orçamento da operação</legend>
          <ModelSelection
            label="Modelo para a construção"
            value={form.value.generation_model_id}
            onChange={(value) => set("generation_model_id", value)}
            models={configuration.available_models}
          />
          <div className="form-grid">
            {numeric(
              "max_generation_context_bytes",
              "Contexto máximo da geração (bytes)",
              "Limite para o contexto da construção, incluindo os arquivos e a continuação da conversa.",
            )}
            {numeric(
              "max_generation_calls",
              "Máximo de chamadas por operação",
              "Inclui a chamada inicial, continuações e recuperações. Uma nova chamada pode gerar consumo.",
            )}
          </div>
        </fieldset>
        <fieldset className="integration-section" disabled={form.busy}>
          <legend>Recuperação de respostas</legend>
          <div className="form-grid">
            {numeric(
              "max_truncation_retries",
              "Recuperações de resposta cortada",
              "Após um corte confirmado, o agente tenta novamente com um lote menor.",
            )}
            {numeric(
              "max_response_repairs",
              "Reparações de formato",
              "Permite corrigir respostas cujo formato não atende ao contrato esperado.",
            )}
          </div>
          <p className="field-help">
            Essas recuperações compartilham o limite total de chamadas; não são
            chamadas adicionais fora do orçamento da operação.
          </p>
        </fieldset>
        <fieldset className="integration-section" disabled={form.busy}>
          <legend>Tamanho dos lotes de código</legend>
          <div className="form-grid">
            {numeric(
              "max_batch_characters",
              "Tamanho máximo do lote (caracteres)",
              "A geração também considera o limite de saída do modelo para escolher o tamanho efetivo.",
            )}
            {numeric(
              "minimum_batch_characters",
              "Tamanho mínimo do lote (caracteres)",
              "Piso utilizado na redução dos lotes após um corte.",
            )}
            {numeric(
              "batch_shrink_factor",
              "Fator de redução após corte",
              "Multiplicador aplicado ao lote anterior. Um fator menor pede menos conteúdo na próxima tentativa.",
            )}
          </div>
        </fieldset>
        <p className="small muted">
          Esta configuração usa a política existente do fluxo guiado. Os dados e
          a credencial do recomendador Jev são preservados; mudanças exclusivas
          nos parâmetros locais não desativam a conexão.
        </p>
        <div className="policy-reason">
          <label htmlFor={reasonId}>Motivo da alteração</label>
          <textarea
            id={reasonId}
            required
            minLength={8}
            maxLength={300}
            rows={3}
            value={form.reason}
            disabled={form.busy}
            onChange={(event) => form.setReason(event.target.value)}
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
            {form.busy ? "Salvando…" : "Salvar política de construção"}
          </button>
        </div>
      </form>
    </IntegrationDialog>
  );
}
