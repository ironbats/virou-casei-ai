import { useEffect, useId, useRef } from "react";
import "./integration-settings.css";

export type ConfigurationField = {
  type?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  enum?: (string | number | null)[];
  default?: unknown;
  anyOf?: ConfigurationField[];
};
export type ConfigurationSchema = {
  properties: Record<string, ConfigurationField>;
};
export type AgentModelOption = {
  id: string;
  provider: string;
  model: string;
  name: string;
  ready: boolean;
  max_output_tokens: number;
};
export type ProviderModelCapability = {
  max_output_tokens: number;
  thinking_modes: string[];
  thinking_budget_min: number | null;
  thinking_budget_max: number | null;
  thinking_budget_can_disable: boolean;
  thinking_budget_dynamic: boolean;
  thinking_levels: string[];
  reasoning_efforts: string[];
  supports_temperature: boolean;
  supports_top_p: boolean;
  sampling_requires_reasoning_none: boolean;
};
export type AgentConfiguration = {
  model: {
    schema: ConfigurationSchema;
    execution_schema: ConfigurationSchema;
    defaults: Record<string, unknown>;
  };
  construction: {
    schema: ConfigurationSchema;
    defaults: Record<string, unknown>;
  };
  preparation: {
    schema: ConfigurationSchema;
    defaults: Record<string, unknown>;
  };
  available_models: AgentModelOption[];
  provider_capabilities: Record<
    string,
    {
      default: ProviderModelCapability;
      models: Record<string, ProviderModelCapability>;
    }
  >;
};

export function fieldConstraints(field: ConfigurationField) {
  return {
    ...field,
    ...field.anyOf?.find((variant) => variant.type !== "null"),
  };
}

export function NumberSetting({
  label,
  field,
  value,
  onChange,
  help,
  name,
  disabled,
  validate,
}: {
  label: string;
  field: ConfigurationField;
  value: unknown;
  onChange: (value: number | string | null) => void;
  help?: string;
  name?: string;
  disabled?: boolean;
  validate?: (value: number | string | null) => string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const bounds = fieldConstraints(field);
  const nullable = field.anyOf?.some((variant) => variant.type === "null");
  const minimum = bounds.minimum ?? bounds.exclusiveMinimum;
  const maximum = bounds.maximum ?? bounds.exclusiveMaximum;
  useEffect(() => {
    const next =
      value === null || value === undefined || value === ""
        ? null
        : Number(value);
    const invalid =
      typeof next === "number" &&
      ((bounds.exclusiveMinimum !== undefined &&
        next <= bounds.exclusiveMinimum) ||
        (bounds.exclusiveMaximum !== undefined &&
          next >= bounds.exclusiveMaximum));
    input.current?.setCustomValidity(
      invalid
        ? "Informe um valor dentro do intervalo indicado."
        : validate?.(next) || "",
    );
  }, [value, bounds.exclusiveMinimum, bounds.exclusiveMaximum, validate]);
  const range = [
    minimum === undefined
      ? ""
      : `${bounds.exclusiveMinimum === undefined ? "Mínimo" : "Maior que"}: ${minimum.toLocaleString("pt-BR")}`,
    maximum === undefined
      ? ""
      : `${bounds.exclusiveMaximum === undefined ? "Máximo" : "Menor que"}: ${maximum.toLocaleString("pt-BR")}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="agent-configuration-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={input}
        name={name}
        type="number"
        min={minimum}
        max={maximum}
        step={bounds.type === "integer" ? 1 : "any"}
        required={!nullable}
        disabled={disabled}
        value={value === null || value === undefined ? "" : String(value)}
        aria-describedby={`${id}-help`}
        onChange={(event) => {
          const text = event.currentTarget.value;
          const next = text === "" ? (nullable ? null : "") : Number(text);
          const invalid =
            typeof next === "number" &&
            ((bounds.exclusiveMinimum !== undefined &&
              next <= bounds.exclusiveMinimum) ||
              (bounds.exclusiveMaximum !== undefined &&
                next >= bounds.exclusiveMaximum));
          event.currentTarget.setCustomValidity(
            invalid
              ? "Informe um valor dentro do intervalo indicado."
              : validate?.(next) || "",
          );
          onChange(next);
        }}
      />
      <p id={`${id}-help`} className="field-help">
        {help}
        {help && range ? " " : ""}
        {range ? `${range}.` : ""}
        {nullable ? " Deixe vazio para usar o comportamento padrão." : ""}
      </p>
    </div>
  );
}

export function ModelSelection({
  label,
  value,
  onChange,
  models,
}: {
  label: string;
  value: unknown;
  onChange: (value: string | null) => void;
  models: AgentModelOption[];
}) {
  const id = useId();
  return (
    <div className="agent-configuration-field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={String(value || "")}
        onChange={(event) => onChange(event.target.value || null)}
      >
        <option value="">Automático · configuração autorizada</option>
        {!!value && !models.some((model) => model.id === value) && (
          <option value={String(value)} disabled>
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
    </div>
  );
}
