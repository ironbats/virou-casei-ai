import { useState } from "react";
import {
  Check,
  CircleHelp,
  LoaderCircle,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { ErrorNotice } from "../shared/ui";
import type {
  Attachment,
  AttachmentIssue,
  BuildDraft,
  Template,
} from "./guidedTypes";
import { fieldProblem, fieldText, sameField } from "./templateFields";
import {
  fieldSources,
  preparationActive,
  preparationStates,
  type AcceptedField,
  type PreparationItem,
  type TemplatePreparation,
} from "./templatePreparationTypes";

export function acceptableFields(
  item: PreparationItem,
  attachment: Attachment | undefined,
  template: Template | undefined,
): AcceptedField[] {
  if (
    !attachment?.enabled ||
    !template ||
    attachment.source_version_id !== item.source_version_id
  )
    return [];
  return item.suggested_fields.flatMap((suggestion) => {
    const field = template.fields.find(
      (field) => field.key === suggestion.field_id,
    );
    if (
      !field ||
      suggestion.state !== "suggested" ||
      !suggestion.compatible ||
      fieldProblem(field, suggestion.value) ||
      !sameField(attachment.fields[field.key], suggestion.original_value) ||
      sameField(attachment.fields[field.key], suggestion.value)
    )
      return [];
    return [{ attachment_id: item.attachment_id, field_id: field.key }];
  });
}

export function TemplatePreparationPanel({
  preparation,
  attachments,
  templates,
  disabled,
  enabled,
  busy,
  loading,
  awaiting,
  error,
  issues,
  statusUnavailable,
  contextChanged,
  onStart,
  onRefresh,
  onCancel,
  onApply,
  onAnswers,
  onEdit,
  history = false,
}: {
  preparation: TemplatePreparation | null;
  attachments: Attachment[];
  templates: Record<string, Template>;
  disabled: boolean;
  enabled: boolean;
  busy: string;
  loading: boolean;
  awaiting: boolean;
  error: unknown;
  issues: AttachmentIssue[];
  statusUnavailable: boolean;
  contextChanged: boolean;
  onStart: () => void;
  onRefresh: () => void;
  onCancel: () => void;
  onApply: (fields: AcceptedField[]) => Promise<BuildDraft>;
  onAnswers: (
    answers: { question_id: string; value: string }[],
  ) => Promise<void>;
  onEdit: (attachment: Attachment, field?: string) => void;
  history?: boolean;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [actionError, setActionError] = useState<unknown>(null);
  const selected = attachments.filter((attachment) => attachment.enabled);
  const running = preparationActive(preparation);
  const currentIndex =
    preparation?.items.findIndex((item) => item.state === "filling") ?? -1;
  const activeCount =
    preparation?.items.filter((item) => item.state === "filling").length ?? 0;
  const title =
    busy === "start"
      ? "Enviando solicitação de preparação"
      : preparation?.state === "filling" && currentIndex >= 0
        ? activeCount === 1
          ? `Preparando ${currentIndex + 1} de ${preparation.progress.total} templates`
          : `Preparando ${activeCount} templates`
        : preparation
          ? preparationStates[preparation.state]
          : "Prepare seus templates";
  const valid =
    preparation?.items.flatMap((item) =>
      acceptableFields(
        item,
        attachments.find(
          (attachment) => attachment.template_id === item.attachment_id,
        ),
        templates[item.source_version_id],
      ),
    ) || [];
  const canApply =
    !!preparation?.can_apply &&
    !contextChanged &&
    !disabled &&
    !busy &&
    !awaiting &&
    !statusUnavailable &&
    !history;
  const questionEntries =
    preparation?.items
      .filter((item) =>
        selected.some(
          (attachment) =>
            attachment.template_id === item.attachment_id &&
            attachment.source_version_id === item.source_version_id,
        ),
      )
      .flatMap((item) =>
        item.questions.map((question) => ({ ...question, item })),
      ) || [];
  const questions = questionEntries.filter(
    (question, index) =>
      questionEntries.findIndex((other) => other.id === question.id) === index,
  );
  const unanswered = questions.filter((question) => !question.answer);
  async function apply(fields: AcceptedField[]) {
    setActionError(null);
    try {
      await onApply(fields);
    } catch (cause) {
      setActionError(cause);
    }
  }
  return (
    <section
      className="template-preparation"
      aria-label="Preparação de templates"
      data-state={preparation?.state || "idle"}
    >
      <div className="preparation-heading">
        <Sparkles size={21} aria-hidden="true" />
        <div>
          <h4>Preparação de templates</h4>
          <p className="small">
            Preenche os templates com seu pedido e contexto. Você revisa antes
            de construir. Consome seu uso de IA.
          </p>
        </div>
      </div>
      <p className="preparation-status" role="status" aria-live="polite">
        {(running || busy === "start") && (
          <LoaderCircle
            size={16}
            className="preparation-spinner"
            aria-hidden="true"
          />
        )}
        {title}
      </p>
      {preparation && <p className="small muted">{preparation.message}</p>}
      {preparation?.context_summary?.reason === "CONTEXT_PARTIAL" && (
        <p className="notice small">
          A preparação usou parte do contexto:{" "}
          {preparation.context_summary.included_files} arquivo(s) incluído(s) e{" "}
          {preparation.context_summary.omitted_files} omitido(s) por limite ou
          proteção.
          {preparation.context_summary.omitted_conversation_messages > 0 && (
            <>
              {" "}
              {preparation.context_summary.included_conversation_messages}{" "}
              mensagem(ns) da conversa incluída(s) e{" "}
              {preparation.context_summary.omitted_conversation_messages}{" "}
              omitida(s).
            </>
          )}{" "}
          Revise as sugestões.
        </p>
      )}
      {!!preparation?.progress.total && running && (
        <p className="small">
          {preparation.progress.completed} de {preparation.progress.total}{" "}
          templates processados.
        </p>
      )}
      {awaiting && (
        <p className="notice">
          A confirmação do envio não chegou. Estamos consultando o resultado
          antes de liberar outra preparação.
        </p>
      )}
      {(contextChanged || preparation?.state === "stale") && (
        <p className="notice">
          O pedido ou a revisão salva mudou. Suas edições estão preservadas.
          Prepare novamente o contexto atual antes de aceitar estas sugestões.
        </p>
      )}
      {preparation?.state === "reconciling" && (
        <p className="notice">
          A chamada precisa ser conferida. Aguarde o resultado; uma nova chamada
          não será enviada automaticamente.
        </p>
      )}
      {preparation?.state === "cancellation_requested" && (
        <p className="notice">
          O cancelamento foi solicitado. A chamada pode continuar no provedor e
          consumir uso; o resultado não será aplicado ao rascunho.
        </p>
      )}
      <ErrorNotice error={error || actionError} />
      {!!issues.length && (
        <ul className="attachment-pending">
          {issues.map((issue, index) => {
            const attachment = attachments.find(
              (item) => item.template_id === issue.attachment_id,
            );
            const template =
              attachment && templates[attachment.source_version_id];
            const label = template?.fields.find(
              (field) => field.key === issue.field_id,
            )?.label;
            return (
              <li
                key={`${issue.attachment_id}-${issue.field_id || "content"}-${index}`}
              >
                {attachment ? (
                  <button
                    className="text-button"
                    disabled={disabled || !!busy}
                    onClick={() => onEdit(attachment, issue.field_id)}
                  >
                    {label ? `${label}: ` : ""}
                    {issue.message}
                  </button>
                ) : (
                  issue.message
                )}
              </li>
            );
          })}
        </ul>
      )}
      {preparation && (
        <ol className="preparation-items">
          {preparation.items.map((item) => {
            const attachment = attachments.find(
              (attachment) => attachment.template_id === item.attachment_id,
            );
            const template = templates[item.source_version_id];
            const accepted = acceptableFields(item, attachment, template);
            const selectedHere =
              attachment?.enabled &&
              attachment.source_version_id === item.source_version_id;
            return (
              <li
                key={item.attachment_id}
                className="preparation-item"
                data-state={item.state}
              >
                <div className="preparation-item-header">
                  <strong>{item.title}</strong>
                  <span className="badge soft">
                    {preparationStates[item.state]}
                  </span>
                </div>
                {!selectedHere && (
                  <p className="small muted">
                    Este anexo não faz parte da seleção atual.
                  </p>
                )}
                {item.errors.map((issue, index) => (
                  <p
                    className="preparation-field-error"
                    key={`${issue.field_id || "item"}-${index}`}
                  >
                    {issue.message}
                  </p>
                ))}
                {!!item.suggested_fields.length && (
                  <details className="preparation-suggestions">
                    <summary>
                      Comparar {item.suggested_fields.length} sugestões com os
                      valores atuais
                    </summary>
                    {item.suggested_fields.map((suggestion) => {
                      const field = template?.fields.find(
                        (field) => field.key === suggestion.field_id,
                      );
                      const eligible = accepted.some(
                        (value) => value.field_id === suggestion.field_id,
                      );
                      const confirmed =
                        ["confirmed", "applied"].includes(
                          attachment?.field_values?.[suggestion.field_id]
                            ?.state || "",
                        ) &&
                        sameField(
                          attachment?.fields[suggestion.field_id],
                          suggestion.value,
                        );
                      return (
                        <div
                          key={suggestion.field_id}
                          className="preparation-suggestion"
                        >
                          <strong>{field?.label || suggestion.field_id}</strong>
                          <p className="small">
                            Origem:{" "}
                            {fieldSources[suggestion.source_type] ||
                              "Origem não informada"}
                          </p>
                          <div className="preparation-comparison">
                            <div>
                              <span>Valor atual</span>
                              <pre>
                                {fieldText(
                                  attachment?.fields[suggestion.field_id],
                                ) || "Ainda não informado"}
                              </pre>
                            </div>
                            <div>
                              <span>Sugestão para revisar</span>
                              <pre>
                                {fieldText(suggestion.value) ||
                                  "Ainda não informado"}
                              </pre>
                            </div>
                          </div>
                          {suggestion.reason && (
                            <p className="small">{suggestion.reason}</p>
                          )}
                          {confirmed ? (
                            <p className="small preparation-confirmed">
                              <Check size={15} aria-hidden="true" />
                              Sugestão confirmada
                            </p>
                          ) : (
                            !eligible &&
                            selectedHere && (
                              <p className="small muted">
                                Confira o campo: uma edição atual ou uma
                                pendência impede aplicar esta sugestão.
                              </p>
                            )
                          )}
                          {eligible && (
                            <button
                              className="button"
                              disabled={!canApply}
                              onClick={() =>
                                void apply([
                                  {
                                    attachment_id: item.attachment_id,
                                    field_id: suggestion.field_id,
                                  },
                                ])
                              }
                            >
                              Aceitar sugestão ·{" "}
                              {field?.label || suggestion.field_id}
                            </button>
                          )}
                        </div>
                      );
                    })}
                  </details>
                )}
                {selectedHere && !history && (
                  <div className="actions">
                    {attachment && (
                      <button
                        className="text-button"
                        disabled={disabled || !!busy}
                        onClick={() => onEdit(attachment)}
                      >
                        Editar campos
                      </button>
                    )}
                    {!!accepted.length && (
                      <button
                        className="button"
                        disabled={!canApply}
                        onClick={() => void apply(accepted)}
                      >
                        Usar sugestões válidas deste template ({accepted.length}
                        )
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
      {!history &&
        !!unanswered.length &&
        !contextChanged &&
        ["needs_input", "ready"].includes(preparation?.state || "") && (
          <form
            className="preparation-questions"
            onSubmit={async (event) => {
              event.preventDefault();
              setActionError(null);
              try {
                await onAnswers(
                  unanswered
                    .filter((question) => answers[question.id]?.trim())
                    .map((question) => ({
                      question_id: question.id,
                      value: answers[question.id].trim(),
                    })),
                );
                setAnswers({});
              } catch (cause) {
                setActionError(cause);
              }
            }}
          >
            <h4>
              <CircleHelp size={18} aria-hidden="true" />
              Precisamos de uma informação
            </h4>
            <p className="small">
              Sua resposta será usada nos campos relacionados. Isso prepara
              novas sugestões para revisão e consome uso de IA.
            </p>
            {unanswered.map((question) => (
              <div key={question.id}>
                <label htmlFor={`preparation-question-${question.id}`}>
                  {question.question}
                  {question.required ? " *" : ""}
                </label>
                <textarea
                  id={`preparation-question-${question.id}`}
                  rows={2}
                  required={question.required}
                  maxLength={4000}
                  disabled={disabled || !!busy || running}
                  value={answers[question.id] || ""}
                  onChange={(event) =>
                    setAnswers((old) => ({
                      ...old,
                      [question.id]: event.target.value,
                    }))
                  }
                  aria-describedby={`preparation-question-help-${question.id}`}
                />
                <p
                  className="small muted"
                  id={`preparation-question-help-${question.id}`}
                >
                  {question.item.title} ·{" "}
                  {question.field_ids
                    .map(
                      (id) =>
                        templates[question.item.source_version_id]?.fields.find(
                          (field) => field.key === id,
                        )?.label || id,
                    )
                    .join(", ")}
                </p>
              </div>
            ))}
            <button
              className="button primary"
              disabled={
                disabled ||
                !!busy ||
                awaiting ||
                statusUnavailable ||
                unanswered.some(
                  (question) =>
                    question.required && !answers[question.id]?.trim(),
                ) ||
                !unanswered.some((question) => answers[question.id]?.trim())
              }
            >
              {busy === "answers"
                ? "Enviando resposta…"
                : "Responder e preparar campos"}
            </button>
          </form>
        )}
      {!history && (
        <div className="guided-actions">
          {!running && (
            <button
              className="button primary"
              disabled={
                disabled ||
                !!busy ||
                awaiting ||
                !enabled ||
                !selected.length ||
                statusUnavailable
              }
              onClick={onStart}
            >
              <Sparkles size={16} aria-hidden="true" />
              {busy === "start"
                ? "Solicitando preparação…"
                : "Preparar templates com IA"}
            </button>
          )}
          {!!valid.length && (
            <button
              className="button"
              disabled={!canApply}
              onClick={() => void apply(valid)}
            >
              Usar sugestões válidas ({valid.length})
            </button>
          )}
          {running &&
            !["cancellation_requested", "reconciling"].includes(
              preparation?.state || "",
            ) && (
              <button
                className="button"
                disabled={disabled || !!busy}
                onClick={onCancel}
              >
                {busy === "cancel"
                  ? "Solicitando cancelamento…"
                  : "Cancelar preparação"}
              </button>
            )}
          <button
            className="text-button"
            disabled={loading || !!busy}
            onClick={() => {
              setActionError(null);
              onRefresh();
            }}
          >
            <RefreshCw size={15} aria-hidden="true" />
            Atualizar preparação
          </button>
        </div>
      )}
      {!history && !enabled && (
        <p className="notice">
          A preparação com IA está indisponível. Você pode preencher manualmente
          ou continuar sem templates.
        </p>
      )}
      {!history && !selected.length && (
        <p className="small muted">
          Selecione os templates que deseja preparar. Você também pode continuar
          sem anexos.
        </p>
      )}
      {!history && !!selected.length && (
        <div className="preparation-manual">
          <span className="small">Prefere adaptar por conta própria?</span>
          <button
            className="text-button"
            disabled={disabled || !!busy}
            onClick={() => onEdit(selected[0])}
          >
            Preencher manualmente
          </button>
        </div>
      )}
      <p className="small muted">
        Preparar usa a revisão salva do projeto. Esta operação não gera nem
        aplica código.
      </p>
    </section>
  );
}
