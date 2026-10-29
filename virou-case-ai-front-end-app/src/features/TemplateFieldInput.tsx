import type { FieldValue, TemplateField } from "./guidedTypes";
import { fieldText } from "./templateFields";

export function TemplateFieldInput({
  field,
  value,
  id,
  disabled,
  invalid,
  describedBy,
  onChange,
}: {
  field: TemplateField;
  value: FieldValue | undefined;
  id: string;
  disabled: boolean;
  invalid?: boolean;
  describedBy?: string;
  onChange: (value: FieldValue) => void;
}) {
  const common = {
    id,
    disabled,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
    "aria-required": field.required || undefined,
  };
  if (field.type === "boolean")
    return (
      <select
        {...common}
        value={typeof value === "boolean" ? String(value) : ""}
        onChange={(event) =>
          onChange(
            event.target.value === "" ? null : event.target.value === "true",
          )
        }
      >
        <option value="">Selecione uma resposta</option>
        <option value="true">Sim</option>
        <option value="false">Não</option>
      </select>
    );
  if (field.type === "enum")
    return (
      <select
        {...common}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Selecione uma opção</option>
        {(field.constraints?.enum || []).map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  return (
    <textarea
      {...common}
      value={fieldText(value)}
      maxLength={
        field.type === "string_list"
          ? ((field.constraints?.max_length ?? field.max_length) + 1) *
            (field.constraints?.max_items ?? 30)
          : (field.constraints?.max_length ?? field.max_length)
      }
      rows={2}
      onChange={(event) =>
        onChange(
          field.type === "string_list"
            ? event.target.value.trim()
              ? event.target.value.split("\n")
              : []
            : event.target.value,
        )
      }
    />
  );
}
