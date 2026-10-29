import type {
  Attachment,
  AttachmentIssue,
  FieldValue,
  Template,
  TemplateField,
} from "./guidedTypes";

const placeholders = new Set([
  "a definir",
  "a preencher",
  "a informar",
  "a confirmar",
  "indefinido",
  "indefinida",
  "nao definido",
  "nao definida",
  "nao informado",
  "nao informada",
  "pendente",
  "tbd",
  "todo",
  "nao aplicavel",
  "nao se aplica",
  "nao disponivel",
  "sem informacao",
  "nao sei",
  "n/a",
  "to be defined",
  "to be determined",
  "to do",
  "placeholder",
  "xxx",
  "...",
  "???",
]);

function unresolved(value: string) {
  const normalized = value
    .normalize("NFKD")
    .replace(/[^\x00-\x7F]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/^[ \[\]().:;!?]+|[ \[\]().:;!?]+$/g, "");
  return (
    /\{\{[^{}\r\n]+\}\}/.test(value) ||
    placeholders.has(normalized) ||
    /^[.?!…\s]+$/.test(value)
  );
}

export function fieldText(value: FieldValue | undefined): string {
  if (Array.isArray(value)) return value.join("\n");
  if (typeof value === "boolean") return value ? "Sim" : "Não";
  return typeof value === "string" ? value : "";
}

export function fieldIsEmpty(value: FieldValue | undefined) {
  return (
    value === null ||
    value === undefined ||
    (typeof value === "string" && !value.trim()) ||
    (Array.isArray(value) && !value.some((item) => item.trim()))
  );
}

export function sameField(
  a: FieldValue | undefined,
  b: FieldValue | undefined,
) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export function fieldProblem(
  field: TemplateField,
  value: FieldValue | undefined,
) {
  const type = field.type || "text";
  if (!["text", "boolean", "enum", "string_list"].includes(type))
    return "O tipo deste campo precisa ser atualizado no catálogo.";
  const missing =
    value === null ||
    value === undefined ||
    (typeof value === "string" && !value.trim()) ||
    (Array.isArray(value) && !value.length);
  if (missing) return field.required ? "Informe este campo obrigatório." : "";
  if (
    (type === "boolean" && typeof value !== "boolean") ||
    (["text", "enum"].includes(type) && typeof value !== "string") ||
    (type === "string_list" &&
      (!Array.isArray(value) ||
        value.some((item) => typeof item !== "string" || !item.trim())))
  )
    return "Confira o formato deste campo.";
  if (type === "boolean") return "";
  const constraints = field.constraints || {};
  const values = Array.isArray(value) ? value : [fieldText(value)];
  if (["text", "string_list"].includes(type) && values.some(unresolved))
    return "Substitua o marcador por uma informação válida.";
  const maximum = Math.min(
    constraints.max_length ?? field.max_length,
    field.max_length,
  );
  if (values.some((item) => item.length > maximum))
    return `Use até ${maximum} caracteres por ${type === "string_list" ? "item" : "campo"}.`;
  if (
    constraints.min_length &&
    values.some((item) => item.length < constraints.min_length!)
  )
    return `Informe pelo menos ${constraints.min_length} caracteres.`;
  if (type === "enum" && !constraints.enum?.includes(value as string))
    return "Escolha uma das opções disponíveis.";
  if (Array.isArray(value)) {
    if (constraints.min_items && value.length < constraints.min_items)
      return `Informe pelo menos ${constraints.min_items} itens.`;
    if (value.length > (constraints.max_items ?? 30))
      return `Use até ${constraints.max_items ?? 30} itens.`;
  }
  return "";
}

export function renderTemplate(
  template: Template,
  fields: Record<string, FieldValue>,
) {
  const definitions = new Map(
    template.fields.map((field) => [field.key, field]),
  );
  return template.content.replace(
    /\{\{([A-Za-z][A-Za-z0-9_]*)\}\}/g,
    (marker, key: string) => {
      const field = definitions.get(key);
      if (!field) return marker;
      const text = fieldText(fields[key]);
      return text.trim() ? text : field.required ? marker : "";
    },
  );
}

export function attachmentIssues(
  attachment: Attachment,
  template: Template | undefined,
  limit: number,
): AttachmentIssue[] {
  if (!attachment.enabled) return [];
  const issue = (
    code: string,
    message: string,
    field_id?: string,
  ): AttachmentIssue => ({
    attachment_id: attachment.template_id,
    field_id,
    code,
    message,
    action: "Editar o anexo ou removê-lo deste pedido.",
  });
  if (!template)
    return [
      issue(
        "TEMPLATE_SCHEMA_UNAVAILABLE",
        "Carregue a versão deste template para conferir os campos.",
      ),
    ];
  const errors = template.fields.flatMap((field) => {
    const problem = fieldProblem(field, attachment.fields[field.key]);
    if (problem)
      return [
        issue(
          "REQUIRED_FIELD_MISSING",
          `${field.label}: ${problem}`,
          field.key,
        ),
      ];
    if (attachment.edited_content.includes(`{{${field.key}}}`))
      return [
        issue(
          "UNRESOLVED_PLACEHOLDER",
          `${field.label}: atualize o texto com o valor deste campo.`,
          field.key,
        ),
      ];
    return [];
  });
  if (!attachment.edited_content.trim())
    errors.push(
      issue("ATTACHMENT_EMPTY", "Preencha o conteúdo que será enviado."),
    );
  if (
    unresolved(attachment.edited_content) &&
    !errors.some((error) => error.code === "UNRESOLVED_PLACEHOLDER")
  )
    errors.push(
      issue(
        "FIELD_PLACEHOLDER",
        "O conteúdo ainda possui marcadores pendentes. Edite o texto completo antes de revisar.",
      ),
    );
  if (attachment.edited_content.length > limit)
    errors.push(
      issue(
        "ATTACHMENT_LIMIT_EXCEEDED",
        `Reduza o anexo para até ${limit} caracteres.`,
      ),
    );
  return errors;
}

export function orderedFields(template: Template) {
  return [...template.fields].sort(
    (a, b) => (a.display_order ?? 0) - (b.display_order ?? 0),
  );
}
