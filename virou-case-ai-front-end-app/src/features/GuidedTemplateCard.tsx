import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  FileText,
  LoaderCircle,
  Pencil,
  Plus,
  Sparkles,
} from "lucide-react";
import type { Attachment, AttachmentIssue, Template } from "./guidedTypes";
import {
  fieldIsEmpty,
  fieldText,
  orderedFields,
  renderTemplate,
} from "./templateFields";
import { TemplateFieldGenerationAction } from "./TemplateFieldGenerationAction";
import type { useTemplateFieldGeneration } from "./useTemplateFieldGeneration";

export function attachmentState(
  attachment: Attachment,
  issues: AttachmentIssue[] = [],
  template?: Template,
) {
  if (attachment.fill_state === "queued") return "Aguardando";
  if (attachment.fill_state === "filling") return "Preenchendo com IA…";
  if (attachment.fill_state === "failed") return "Falha no preenchimento";
  if (attachment.fill_state === "stale") return "Contexto alterado";
  if (attachment.fill_state === "reconciling")
    return "Conferindo resultado da IA";
  if (issues.length || attachment.fill_state === "needs_input")
    return "Precisa de informação";
  const fields = Object.values(attachment.field_values || {});
  if (
    fields.some((field) => field.edited_by_user) ||
    (template &&
      attachment.edited_content !== renderTemplate(template, attachment.fields))
  )
    return "Conteúdo editado";
  if (
    fields.some(
      (field) =>
        ["applied", "confirmed"].includes(field.state) && !field.edited_by_user,
    )
  )
    return "Preenchido pela IA";
  return "Conteúdo para revisão";
}

export function GuidedTemplateCard({
  template,
  schema,
  attachment: a,
  category,
  recommended,
  disabled,
  atLimit,
  update,
  edit,
  retry,
  fieldGeneration,
  issues = [],
}: {
  template: Template;
  schema?: Template;
  attachment: Attachment;
  category: string;
  recommended: boolean;
  disabled: boolean;
  atLimit: boolean;
  update: (value: Attachment) => void;
  edit: (value: Attachment, field?: string) => void;
  retry: (value: Attachment) => void;
  fieldGeneration: ReturnType<typeof useTemplateFieldGeneration>;
  issues?: AttachmentIssue[];
}) {
  const [expanded, setExpanded] = useState(false);
  const card = useRef<HTMLElement>(null);
  const generatedFocus = useRef<{
    field: string;
    trigger: HTMLElement | null;
  } | null>(null);
  const newer = template.version_id !== a.source_version_id;
  const state = attachmentState(a, issues, schema || template);
  const working = ["queued", "filling", "reconciling"].includes(
    a.fill_state || "",
  );
  const missingFields = orderedFields(schema || template).filter((field) =>
    fieldIsEmpty(a.fields[field.key]),
  );
  const generationReason = (fieldId: string) =>
    fieldGeneration.reason(a, fieldId) ||
    (disabled
      ? "Aguarde a operação atual antes de gerar."
      : newer && !schema
        ? "Carregue a versão deste template antes de gerar."
        : "");
  useEffect(() => {
    const focus = generatedFocus.current;
    if (!focus || fieldIsEmpty(a.fields[focus.field])) return;
    generatedFocus.current = null;
    requestAnimationFrame(() => {
      if (
        document.activeElement === focus.trigger ||
        document.activeElement === document.body
      )
        card.current?.focus();
    });
  }, [a.fields]);
  function generate(fieldId: string) {
    generatedFocus.current = {
      field: fieldId,
      trigger:
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null,
    };
    void fieldGeneration.generate(a, fieldId);
  }
  return (
    <article
      ref={card}
      tabIndex={-1}
      className={"template-card " + (a.enabled ? "selected" : "")}
      id={`attachment-${a.template_id}`}
    >
      <div className="template-card-heading">
        <label className="template-check">
          <input
            type="checkbox"
            checked={a.enabled}
            disabled={disabled || (!a.enabled && atLimit)}
            onChange={(e) => update({ ...a, enabled: e.target.checked })}
          />
          <span className="sr-only">Selecionar {template.title}</span>
        </label>
        <FileText size={22} aria-hidden="true" />
        <div className="template-card-title">
          <strong>{a.title || template.title}</strong>
          <div className="template-badges">
            <span>{category}</span>
            <span>
              Esquema v{a.schema_version ?? a.version ?? template.version}
            </span>
            {recommended && (
              <span className="badge soft">
                <Sparkles size={12} aria-hidden="true" /> Recomendado pelo Jev
              </span>
            )}
          </div>
        </div>
        {a.enabled && (
          <span
            className={`template-fill-state ${issues.length || a.fill_state === "failed" ? "pending" : working ? "working" : ""}`}
            role="status"
          >
            {working && (
              <LoaderCircle
                size={14}
                className="preparation-spinner"
                aria-hidden="true"
              />
            )}
            {state}
          </span>
        )}
        <button
          type="button"
          className="template-expand"
          aria-expanded={expanded}
          aria-controls={`template-content-${a.template_id}`}
          aria-label={`${expanded ? "Recolher" : "Abrir"} ${template.title}`}
          onClick={() => setExpanded(!expanded)}
        >
          <ChevronDown size={18} />
        </button>
      </div>
      {expanded && (
        <div
          className="template-card-content"
          id={`template-content-${a.template_id}`}
        >
          <p>{template.summary}</p>
          {a.enabled &&
            orderedFields(schema || template).map((field) => {
              const provenance = a.field_values?.[field.key];
              const problems = issues.filter(
                (issue) => issue.field_id === field.key,
              );
              return (
                <div className="template-field-preview" key={field.key}>
                  <strong>
                    {field.label}
                    {field.required ? " *" : ""}
                  </strong>
                  <div>
                    <p className="template-field-value">
                      {fieldText(a.fields[field.key]) ||
                        (working
                          ? "A IA está preparando este campo…"
                          : "Informação pendente")}
                    </p>
                    <TemplateFieldGenerationAction
                      field={field}
                      empty={fieldIsEmpty(a.fields[field.key])}
                      status={fieldGeneration.status(a, field.key)}
                      blockedReason={generationReason(field.key)}
                      onGenerate={() => generate(field.key)}
                    />
                    {provenance && (
                      <span className="small muted">
                        {provenance.edited_by_user
                          ? "Editado por você"
                          : provenance.source_type === "policy_suggestion"
                            ? "Sugestão da IA · revise"
                            : provenance.source_type === "project_context"
                              ? "Origem: contexto salvo"
                              : provenance.source_type === "request"
                                ? "Origem: seu pedido"
                                : "Origem: IA · revise o conteúdo"}
                      </span>
                    )}
                    {problems.map((issue, index) => (
                      <button
                        className="text-button preparation-field-error"
                        key={index}
                        disabled={disabled}
                        onClick={() => edit(a, field.key)}
                      >
                        {issue.message}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          {newer && (
            <button
              className="text-button"
              disabled={disabled}
              onClick={() => {
                if (
                  confirm(
                    "Substituir a cópia pessoal deste anexo pela nova versão do catálogo? As edições desta cópia serão substituídas.",
                  )
                )
                  update({
                    ...a,
                    source_version_id: template.version_id,
                    edited_content: template.content,
                    fields: {},
                    version: template.version,
                    title: template.title,
                    category: template.category,
                    edited: false,
                    field_values: {},
                    validation_errors: [],
                  });
              }}
            >
              Usar versão {template.version} do catálogo
            </button>
          )}
        </div>
      )}
      {a.enabled &&
        a.fill_message &&
        ["failed", "needs_input", "stale", "reconciling"].includes(
          a.fill_state || "",
        ) && (
          <p
            className="template-card-message"
            role={a.fill_state === "failed" ? "alert" : "status"}
          >
            {a.fill_message}
          </p>
        )}
      {a.enabled &&
        !expanded &&
        missingFields.map((field) => {
          const question = a.fill_questions?.find((item) =>
            item.field_ids.includes(field.key),
          );
          return (
            <div className="template-question-row" key={field.key}>
              <button
                type="button"
                className="text-button template-question"
                disabled={disabled}
                onClick={() => edit(a, field.key)}
              >
                {question?.question || `${field.label}: informação pendente.`}
              </button>
              <TemplateFieldGenerationAction
                field={field}
                empty
                status={fieldGeneration.status(a, field.key)}
                blockedReason={generationReason(field.key)}
                onGenerate={() => generate(field.key)}
              />
            </div>
          );
        })}
      {a.enabled &&
        missingFields.some((field) => field.sensitivity !== "secret") && (
          <p className="small muted template-generation-consumption">
            Gerar usa o pedido salvo e pode consumir seu uso de IA. Revise a
            sugestão antes de construir.
          </p>
        )}
      {a.enabled && issues.length > 0 && !expanded && (
        <button
          className="text-button preparation-field-error template-card-message"
          disabled={disabled}
          onClick={() => edit(a, issues[0].field_id)}
        >
          {issues[0].message}
        </button>
      )}
      <div className="template-card-actions">
        {a.enabled ? (
          <>
            {a.fill_state === "failed" && (
              <button
                className="text-button"
                disabled={disabled}
                onClick={() => retry(a)}
              >
                Tentar preencher novamente
              </button>
            )}
            <button
              className="text-button"
              disabled={disabled}
              onClick={() => edit(a)}
            >
              <Pencil size={15} aria-hidden="true" /> Editar conteúdo
            </button>
          </>
        ) : (
          <button
            className="button"
            disabled={disabled || atLimit}
            onClick={() => update({ ...a, enabled: true })}
          >
            <Plus size={15} aria-hidden="true" /> Adicionar este template
          </button>
        )}
      </div>
    </article>
  );
}
