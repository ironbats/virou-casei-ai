import { useEffect, useRef, useState } from "react";
import {
  fieldConstraints,
  NumberSetting,
  type AgentConfiguration,
} from "./agent-configuration-fields";

const thinkingLabels: Record<string, string> = {
  auto: "Automático por modelo e etapa",
  provider_default: "Padrão do provedor",
  budget: "Orçamento em tokens",
  level: "Nível de raciocínio",
};
const effortLabels: Record<string, string> = {
  none: "Nenhum",
  minimal: "Mínimo",
  low: "Baixo",
  medium: "Médio",
  high: "Alto",
  xhigh: "Muito alto",
  max: "Máximo",
};

export function ModelExecutionFields({
  configuration,
  provider,
  model,
  initialMaxOutputTokens,
  initialExecution,
}: {
  configuration: AgentConfiguration;
  provider: string;
  model: string;
  initialMaxOutputTokens?: number;
  initialExecution?: Record<string, unknown> | null;
}) {
  const [maxOutput, setMaxOutput] = useState<unknown>(
    initialMaxOutputTokens ?? configuration.model.defaults.max_output_tokens,
  );
  const [custom, setCustom] = useState(!!initialExecution);
  const [execution, setExecution] = useState<Record<string, unknown>>({
    ...(configuration.model.defaults.execution as Record<string, unknown>),
    ...initialExecution,
  });
  const [sampling, setSampling] = useState(
    initialExecution?.temperature != null
      ? "temperature"
      : initialExecution?.top_p != null
        ? "top_p"
        : "default",
  );
  const samplingControl = useRef<HTMLSelectElement>(null);
  const thinkingControl = useRef<HTMLSelectElement>(null);
  const levelControl = useRef<HTMLSelectElement>(null);
  const effortControl = useRef<HTMLSelectElement>(null);
  const providerSettings = configuration.provider_capabilities[provider];
  const capability =
    providerSettings?.models[model] || providerSettings?.default;
  const fields = configuration.model.execution_schema.properties;
  const outputField = configuration.model.schema.properties.max_output_tokens;
  const thinking = String(execution.thinking_mode || "");
  const set = (name: string, value: unknown) =>
    setExecution((current) => ({ ...current, [name]: value }));
  const budgetField = fieldConstraints(fields.thinking_budget);
  const dynamicBudget = capability?.thinking_budget_dynamic
    ? budgetField.minimum
    : undefined;
  const minimumBudget =
    dynamicBudget ??
    (capability?.thinking_budget_can_disable
      ? 0
      : (capability?.thinking_budget_min ?? budgetField.minimum));
  const maximumOutput = capability
    ? Math.min(
        capability.max_output_tokens,
        outputField.maximum ?? capability.max_output_tokens,
      )
    : outputField.maximum;
  const supportedSampling =
    sampling === "default" ||
    (sampling === "temperature"
      ? capability?.supports_temperature
      : capability?.supports_top_p);
  const validSampling =
    supportedSampling &&
    (sampling === "default" ||
      !capability?.sampling_requires_reasoning_none ||
      execution.reasoning_effort === "none");
  const supportedThinking = capability?.thinking_modes.includes(thinking);
  const level = String(execution.thinking_level || "");
  const effort = String(execution.reasoning_effort || "");
  const supportedLevel = !level || capability?.thinking_levels.includes(level);
  const supportedEffort =
    !effort || capability?.reasoning_efforts.includes(effort);
  useEffect(() => {
    samplingControl.current?.setCustomValidity(
      validSampling
        ? ""
        : "A amostragem escolhida não é compatível com o modelo e o esforço de raciocínio atuais.",
    );
    thinkingControl.current?.setCustomValidity(
      supportedThinking
        ? ""
        : "Escolha um controle de raciocínio compatível com o modelo.",
    );
    levelControl.current?.setCustomValidity(
      supportedLevel ? "" : "Escolha um nível compatível com o modelo.",
    );
    effortControl.current?.setCustomValidity(
      supportedEffort ? "" : "Escolha um esforço compatível com o modelo.",
    );
  }, [
    custom,
    validSampling,
    supportedThinking,
    supportedLevel,
    supportedEffort,
    thinking,
  ]);
  return (
    <>
      <h3>Execução do modelo</h3>
      <NumberSetting
        name="max_output_tokens"
        label="Limite de tokens de saída"
        field={{ ...outputField, maximum: maximumOutput }}
        value={maxOutput}
        onChange={setMaxOutput}
        help="Limite enviado em cada chamada. O orçamento de raciocínio do provedor também pode consumir tokens da resposta."
      />
      <label className="check-row">
        <input
          name="custom_execution"
          type="checkbox"
          checked={custom}
          onChange={(event) => setCustom(event.target.checked)}
        />
        Personalizar execução
      </label>
      {!custom && (
        <p className="small muted">
          Mantém o comportamento atual da plataforma para tempo de espera,
          amostragem e raciocínio. Os limites da construção e do preenchimento
          são configurados nas respectivas seções.
        </p>
      )}
      {custom && (
        <>
          <p className="notice">
            Ao alterar estes parâmetros, o modelo será desabilitado até um novo
            teste de geração. Os controles disponíveis dependem do modelo e são
            validados pelo servidor ao salvar.
          </p>
          <div className="form-grid">
            <NumberSetting
              name="request_timeout_seconds"
              label="Tempo máximo da chamada (segundos)"
              field={fields.request_timeout_seconds}
              value={execution.request_timeout_seconds}
              onChange={(value) => set("request_timeout_seconds", value)}
            />
            <NumberSetting
              name="connect_timeout_seconds"
              label="Tempo máximo de conexão (segundos)"
              field={fields.connect_timeout_seconds}
              value={execution.connect_timeout_seconds}
              onChange={(value) => set("connect_timeout_seconds", value)}
            />
          </div>
          <label>
            Amostragem
            <select
              ref={samplingControl}
              name="sampling_mode"
              value={sampling}
              onChange={(event) => setSampling(event.target.value)}
            >
              <option value="default">Padrão do provedor</option>
              {!supportedSampling && (
                <option value={sampling} disabled>
                  Configuração atual incompatível com este modelo
                </option>
              )}
              {capability?.supports_temperature && (
                <option value="temperature">Temperatura</option>
              )}
              {capability?.supports_top_p && (
                <option value="top_p">Top P</option>
              )}
            </select>
          </label>
          {capability?.sampling_requires_reasoning_none && (
            <p className="field-help">
              Este modelo permite amostragem personalizada somente com esforço
              de raciocínio Nenhum.
            </p>
          )}
          {sampling === "temperature" && capability?.supports_temperature && (
            <NumberSetting
              name="temperature"
              label="Temperatura"
              field={{
                ...fieldConstraints(fields.temperature),
                anyOf: undefined,
              }}
              value={execution.temperature}
              onChange={(value) => set("temperature", value)}
              help="Configure apenas um controle de amostragem por vez."
            />
          )}
          {sampling === "top_p" && capability?.supports_top_p && (
            <NumberSetting
              name="top_p"
              label="Top P"
              field={{ ...fieldConstraints(fields.top_p), anyOf: undefined }}
              value={execution.top_p}
              onChange={(value) => set("top_p", value)}
              help="Configure apenas um controle de amostragem por vez."
            />
          )}
          <label>
            Controle de raciocínio
            <select
              ref={thinkingControl}
              name="thinking_mode"
              value={thinking}
              required
              onChange={(event) => set("thinking_mode", event.target.value)}
            >
              {thinking && !capability?.thinking_modes.includes(thinking) && (
                <option value={thinking} disabled>
                  Configuração atual incompatível com este modelo
                </option>
              )}
              {(capability?.thinking_modes || []).map((mode) => (
                <option key={mode} value={mode}>
                  {thinkingLabels[mode] || mode}
                </option>
              ))}
            </select>
          </label>
          {thinking === "budget" && supportedThinking && (
            <NumberSetting
              name="thinking_budget"
              label="Orçamento de raciocínio (tokens)"
              field={{
                ...budgetField,
                anyOf: undefined,
                minimum: minimumBudget,
                maximum: capability?.thinking_budget_max ?? budgetField.maximum,
              }}
              value={execution.thinking_budget}
              onChange={(value) => set("thinking_budget", value)}
              help={`Para este modelo, use de ${capability?.thinking_budget_min?.toLocaleString("pt-BR")} a ${capability?.thinking_budget_max?.toLocaleString("pt-BR")} tokens${capability?.thinking_budget_can_disable ? ", 0 para desabilitar" : ""}${dynamicBudget !== undefined ? `, ou ${dynamicBudget} para orçamento dinâmico` : ""}.`}
              validate={(value) =>
                typeof value === "number" &&
                value !== dynamicBudget &&
                !(value === 0 && capability?.thinking_budget_can_disable) &&
                capability?.thinking_budget_min != null &&
                value < capability.thinking_budget_min
                  ? "Informe o orçamento mínimo do modelo ou a opção dinâmica indicada."
                  : ""
              }
            />
          )}
          {thinking === "level" && supportedThinking && (
            <label>
              Nível de raciocínio
              <select
                ref={levelControl}
                name="thinking_level"
                required
                value={level}
                onChange={(event) => set("thinking_level", event.target.value)}
              >
                <option value="">Selecione</option>
                {!supportedLevel && (
                  <option value={level} disabled>
                    Nível atual incompatível com este modelo
                  </option>
                )}
                {(capability?.thinking_levels || []).map((level) => (
                  <option key={level} value={level}>
                    {effortLabels[level] || level}
                  </option>
                ))}
              </select>
            </label>
          )}
          {provider === "openai" && (
            <label>
              Esforço de raciocínio
              <select
                ref={effortControl}
                name="reasoning_effort"
                value={effort}
                onChange={(event) =>
                  set("reasoning_effort", event.target.value || null)
                }
              >
                <option value="">Padrão do provedor</option>
                {!supportedEffort && (
                  <option value={effort} disabled>
                    Esforço atual incompatível com este modelo
                  </option>
                )}
                {(capability?.reasoning_efforts || []).map((effort) => (
                  <option key={effort} value={effort}>
                    {effortLabels[effort] || effort}
                  </option>
                ))}
              </select>
            </label>
          )}
        </>
      )}
    </>
  );
}

export function modelExecutionPayload(form: FormData, provider: string) {
  if (!form.has("custom_execution")) return null;
  const get = (name: string) => String(form.get(name) ?? "");
  const optionalNumber = (name: string) =>
    get(name) === "" ? null : Number(get(name));
  return {
    request_timeout_seconds: Number(get("request_timeout_seconds")),
    connect_timeout_seconds: Number(get("connect_timeout_seconds")),
    temperature:
      get("sampling_mode") === "temperature"
        ? optionalNumber("temperature")
        : null,
    top_p: get("sampling_mode") === "top_p" ? optionalNumber("top_p") : null,
    thinking_mode: get("thinking_mode"),
    thinking_budget:
      get("thinking_mode") === "budget"
        ? optionalNumber("thinking_budget")
        : null,
    thinking_level:
      get("thinking_mode") === "level" ? get("thinking_level") || null : null,
    reasoning_effort:
      provider === "openai" ? get("reasoning_effort") || null : null,
  };
}

export function modelExecutionChanged(
  previous: Record<string, unknown> | null | undefined,
  next: Record<string, unknown> | null,
) {
  if (!previous || !next) return previous != null || next != null;
  return Object.keys({ ...previous, ...next }).some(
    (key) => previous[key] !== next[key],
  );
}
