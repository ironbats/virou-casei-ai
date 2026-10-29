import { useEffect, useRef, useState } from "react";
import { Check, Sparkles } from "lucide-react";
import { Modal } from "../shared/ui";
import type {
  Attachment,
  BuildDraft,
  FieldValue,
  Template,
} from "./guidedTypes";
import { TemplateFieldInput } from "./TemplateFieldInput";
import { TemplateFieldGenerationAction } from "./TemplateFieldGenerationAction";
import { acceptableFields } from "./TemplatePreparationPanel";
import { mergeBuildAttachment } from "./mergeBuildDraft";
import type { useTemplateFieldGeneration } from "./useTemplateFieldGeneration";
import {
  fieldIsEmpty,
  fieldProblem,
  fieldText,
  orderedFields,
  renderTemplate,
  sameField,
} from "./templateFields";
import {
  fieldSources,
  type AcceptedField,
  type PreparationItem,
} from "./templatePreparationTypes";

export function TemplateEditor({
  template,
  attachment,
  limit,
  onSave,
  close,
  preparation,
  canAccept = false,
  onAccept,
  focusField,
  liveAttachment,
  fieldGeneration,
}: {
  template: Template;
  attachment: Attachment;
  limit: number;
  onSave: (value: Attachment, saved: Attachment) => Promise<void>;
  close: () => void;
  preparation?: PreparationItem;
  canAccept?: boolean;
  onAccept?: (fields: AcceptedField[]) => Promise<BuildDraft>;
  focusField?: string;
  liveAttachment?: Attachment;
  fieldGeneration?: ReturnType<typeof useTemplateFieldGeneration>;
}) {
  const [value, setValue] = useState(attachment);
  const [manual, setManual] = useState(
    attachment.edited_content !== renderTemplate(template, attachment.fields),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState("");
  const saved = useRef(attachment);
  const localValue = useRef(value);
  localValue.current = value;
  const form = useRef<HTMLFormElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const generatedFocus = useRef<{
    field: string;
    trigger: HTMLElement | null;
  } | null>(null);
  const dirty =
    JSON.stringify(value.fields) !== JSON.stringify(saved.current.fields) ||
    value.edited_content !== saved.current.edited_content;
  const eligible = preparation
    ? acceptableFields(preparation, value, template)
    : [];
  useEffect(() => {
    const field =
      focusField && document.getElementById(`template-field-${focusField}`);
    if (field) {
      field.focus();
      field.scrollIntoView({ block: "nearest" });
    } else heading.current?.focus();
  }, [focusField]);
  useEffect(() => {
    if (!liveAttachment) return;
    if (
      liveAttachment.source_version_id !== attachment.source_version_id ||
      liveAttachment.enabled !== attachment.enabled
    ) {
      setError(
        "A seleção deste anexo mudou. Seu texto foi preservado nesta janela; confira o pedido atual antes de salvar.",
      );
      return;
    }
    if (JSON.stringify(liveAttachment) === JSON.stringify(saved.current))
      return;
    const merged = mergeBuildAttachment(
      localValue.current,
      saved.current,
      liveAttachment,
      template,
    );
    if (!merged) {
      setError(
        "Este campo mudou em outra sessão. Seu texto foi preservado nesta janela; compare o pedido atual antes de substituir.",
      );
      return;
    }
    const focus = generatedFocus.current;
    if (focus && !fieldIsEmpty(merged.fields[focus.field])) {
      generatedFocus.current = null;
      requestAnimationFrame(() => {
        if (
          document.activeElement !== focus.trigger &&
          document.activeElement !== document.body
        )
          return;
        const field = document.getElementById(`template-field-${focus.field}`);
        if (field && form.current?.contains(field)) field.focus();
      });
    }
    saved.current = liveAttachment;
    localValue.current = merged;
    setValue(merged);
    setManual(
      merged.edited_content !== renderTemplate(template, merged.fields),
    );
    setTouched(
      (old) =>
        new Set(
          [...old].filter(
            (field) =>
              liveAttachment.field_values?.[field]?.edited_by_user !== false ||
              !sameField(merged.fields[field], liveAttachment.fields[field]),
          ),
        ),
    );
  }, [
    liveAttachment,
    template,
    attachment.source_version_id,
    attachment.enabled,
  ]);

  function replaceValue(next: Attachment) {
    localValue.current = next;
    setValue(next);
  }
  function leave() {
    if (busy) return;
    if (
      !dirty ||
      confirm("Descartar as alterações ainda não salvas deste anexo?")
    )
      close();
  }
  function change(field: string, fieldValue: FieldValue) {
    const fields = { ...value.fields, [field]: fieldValue };
    const provenance = { ...value.field_values };
    delete provenance[field];
    setTouched((old) => new Set(old).add(field));
    replaceValue({
      ...value,
      fields,
      field_values: provenance,
      validation_errors: [],
      edited_content: manual
        ? value.edited_content
        : renderTemplate(template, fields),
    });
    setNotice("");
  }
  async function accept(fields: AcceptedField[]) {
    if (!onAccept || !canAccept || busy || !fields.length) return;
    setBusy(true);
    setError("");
    try {
      await onSave(value, saved.current);
      saved.current = value;
      const next = await onAccept(fields);
      const updated = next.attachments.find(
        (item) =>
          item.template_id === attachment.template_id &&
          item.source_version_id === attachment.source_version_id,
      );
      if (!updated)
        throw new Error(
          "Este anexo mudou. Feche a edição e confira a seleção atual.",
        );
      replaceValue(updated);
      saved.current = updated;
      setManual(
        updated.edited_content !== renderTemplate(template, updated.fields),
      );
      setTouched(new Set());
      setNotice(
        "Rascunho atualizado. Confira os campos preservados e o conteúdo final antes de construir.",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }
  async function generate(fieldId: string) {
    if (!fieldGeneration || busy) return;
    generatedFocus.current = {
      field: fieldId,
      trigger:
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null,
    };
    const submitted = localValue.current;
    const previous = saved.current;
    await fieldGeneration.generate(submitted, fieldId, async () => {
      await onSave(submitted, previous);
      if (!fieldIsEmpty(localValue.current.fields[fieldId]))
        throw new Error(
          "Você preencheu este campo enquanto o pedido era salvo. Seu texto foi preservado.",
        );
    });
  }
  return (
    <Modal title={`Editar · ${template.title}`} close={leave}>
      <div className="template-editor">
        <p className="small">
          Esta edição pertence apenas ao próximo pedido. Versão de origem{" "}
          {template.version}. O catálogo original é preservado.
        </p>
        <form
          className="form template-editor-form"
          ref={form}
          onSubmit={async (event) => {
            event.preventDefault();
            if (busy) return;
            setBusy(true);
            setError("");
            try {
              await onSave(value, saved.current);
              saved.current = value;
              close();
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : String(cause));
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="template-editor-heading">
            <h3 ref={heading} tabIndex={-1}>
              Campos do template
            </h3>
            <p className="small">
              Você pode salvar incompleto e continuar depois. Campos
              obrigatórios precisam estar resolvidos antes de construir.
            </p>
            {fieldGeneration && (
              <p className="small muted template-generation-consumption">
                Gerar usa o pedido atual salvo e pode consumir seu uso de IA.
                Revise as sugestões antes de construir.
              </p>
            )}
            {eligible.length > 0 && (
              <button
                type="button"
                className="button"
                disabled={!canAccept || busy}
                onClick={() => void accept(eligible)}
              >
                <Sparkles size={16} aria-hidden="true" />
                Usar sugestões válidas ({eligible.length})
              </button>
            )}
          </div>
          {orderedFields(template).map((field) => {
            const suggestion = preparation?.suggested_fields.find(
              (item) => item.field_id === field.key,
            );
            const provenance = value.field_values?.[field.key];
            const knownProvenance =
              provenance &&
              sameField(value.fields[field.key], provenance.value);
            const problem = fieldProblem(field, value.fields[field.key]);
            const displayProblem =
              problem &&
              (touched.has(field.key) ||
                focusField === field.key ||
                value.validation_errors?.some(
                  (issue) => issue.field_id === field.key,
                ));
            const id = `template-field-${field.key}`;
            return (
              <section className="template-editor-field" key={field.key}>
                <div className="template-editor-field-heading">
                  <label htmlFor={id}>
                    {field.label}
                    {field.required ? " *" : ""}
                  </label>
                  {fieldGeneration && (
                    <TemplateFieldGenerationAction
                      field={field}
                      empty={fieldIsEmpty(value.fields[field.key])}
                      status={fieldGeneration.status(
                        liveAttachment || value,
                        field.key,
                      )}
                      blockedReason={
                        busy
                          ? "Aguarde o salvamento da edição."
                          : fieldGeneration.reason(
                              liveAttachment || value,
                              field.key,
                            )
                      }
                      onGenerate={() => void generate(field.key)}
                    />
                  )}
                </div>
                {(field.description || field.type === "string_list") && (
                  <p id={`${id}-help`} className="small muted">
                    {field.description}
                    {field.type === "string_list"
                      ? " Informe um item por linha."
                      : ""}
                  </p>
                )}
                {value.fill_questions
                  ?.filter(
                    (question) =>
                      question.field_ids.includes(field.key) &&
                      fieldIsEmpty(value.fields[field.key]),
                  )
                  .map((question) => (
                    <p className="notice" key={question.id}>
                      {question.question}
                    </p>
                  ))}
                <TemplateFieldInput
                  field={field}
                  value={value.fields[field.key]}
                  id={id}
                  disabled={busy}
                  invalid={!!displayProblem}
                  describedBy={
                    [
                      field.description || field.type === "string_list"
                        ? `${id}-help`
                        : "",
                      displayProblem ? `${id}-error` : "",
                    ]
                      .filter(Boolean)
                      .join(" ") || undefined
                  }
                  onChange={(next) => change(field.key, next)}
                />
                {displayProblem && (
                  <p id={`${id}-error`} className="preparation-field-error">
                    {problem}
                  </p>
                )}
                {knownProvenance && (
                  <p className="small muted">
                    {provenance.source_type === "policy_suggestion" &&
                    !provenance.edited_by_user
                      ? "Sugestão da IA · revise"
                      : ["confirmed", "applied"].includes(provenance.state)
                        ? "Preenchido pela IA"
                        : provenance.state === "suggested"
                          ? "Sugestão pendente de revisão"
                          : provenance.state === "user_edited"
                            ? "Edição manual"
                            : "Confira este campo"}
                    {provenance.source_type !== "policy_suggestion" && (
                      <>
                        {" "}
                        ·{" "}
                        {fieldSources[provenance.source_type] ||
                          "Origem registrada"}
                      </>
                    )}
                    {provenance.edited_by_user ? " · Editado por você" : ""}
                  </p>
                )}
                {touched.has(field.key) && (
                  <p className="small muted">
                    Editado por você neste rascunho.
                  </p>
                )}
                {suggestion && (
                  <div className="template-field-suggestion">
                    <strong>Sugestão para revisar</strong>
                    <p className="small">
                      Origem:{" "}
                      {fieldSources[suggestion.source_type] ||
                        "Origem não informada"}
                    </p>
                    <pre>
                      {fieldText(suggestion.value) || "Ainda não informado"}
                    </pre>
                    {suggestion.reason && (
                      <p className="small">{suggestion.reason}</p>
                    )}
                    {eligible.some((item) => item.field_id === field.key) ? (
                      <button
                        type="button"
                        className="text-button"
                        disabled={!canAccept || busy}
                        onClick={() =>
                          void accept([
                            {
                              attachment_id: attachment.template_id,
                              field_id: field.key,
                            },
                          ])
                        }
                      >
                        Aceitar sugestão · {field.label}
                      </button>
                    ) : sameField(value.fields[field.key], suggestion.value) &&
                      knownProvenance &&
                      ["confirmed", "applied"].includes(provenance.state) ? (
                      <p className="small preparation-confirmed">
                        <Check size={14} aria-hidden="true" />
                        Sugestão confirmada
                      </p>
                    ) : (
                      <p className="small muted">
                        O valor atual foi preservado. Confira edições e
                        pendências antes de usar esta sugestão.
                      </p>
                    )}
                  </div>
                )}
              </section>
            );
          })}
          {manual && template.fields.length > 0 && (
            <div className="notice template-manual-notice">
              <p className="small">
                O texto foi personalizado. Alterar campos não substitui esse
                texto automaticamente.
              </p>
              <button
                type="button"
                className="text-button"
                disabled={busy}
                onClick={() => {
                  if (
                    confirm(
                      "Substituir o texto personalizado pelo conteúdo-base com os campos preenchidos?",
                    )
                  ) {
                    replaceValue({
                      ...value,
                      edited_content: renderTemplate(template, value.fields),
                    });
                    setManual(false);
                  }
                }}
              >
                Atualizar texto a partir dos campos
              </button>
            </div>
          )}
          <label htmlFor="template-content">Conteúdo que será enviado</label>
          <textarea
            id="template-content"
            rows={8}
            disabled={busy}
            value={value.edited_content}
            aria-invalid={value.edited_content.length > limit}
            onChange={(event) => {
              setManual(true);
              replaceValue({
                ...value,
                edited_content: event.target.value,
                validation_errors: [],
              });
            }}
          />
          <p className="small muted">
            {value.edited_content.length}/{limit} caracteres
          </p>
          <details className="template-content-comparison">
            <summary>Comparar com a versão do catálogo</summary>
            <div className="preparation-comparison">
              <div>
                <strong>Conteúdo-base do catálogo</strong>
                <pre>{template.content}</pre>
              </div>
              <div>
                <strong>Conteúdo atual do anexo</strong>
                <pre>{value.edited_content}</pre>
              </div>
            </div>
          </details>
          {notice && (
            <p className="notice" role="status">
              {notice}
            </p>
          )}
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <div className="actions template-editor-actions">
            <button
              type="button"
              className="button"
              onClick={leave}
              disabled={busy}
            >
              Cancelar edição
            </button>
            <button
              className="button primary"
              disabled={busy || value.edited_content.length > limit}
            >
              {busy ? "Salvando…" : "Salvar edição"}
            </button>
          </div>
        </form>
      </div>
    </Modal>
  );
}
