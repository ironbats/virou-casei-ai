import { Eye, FileText, Pencil, Trash2 } from "lucide-react";
import type { Attachment, AttachmentIssue, Template } from "./guidedTypes";
import { attachmentState } from "./GuidedTemplateCard";

export function BuildAttachments({
  selected,
  catalog,
  disable,
  open,
  edit,
  remove,
  issues = {},
}: {
  selected: Attachment[];
  catalog: Template[] | null;
  disable: boolean;
  open: (attachment: Attachment) => void;
  edit: (attachment: Attachment, field?: string) => void;
  remove: (attachment: Attachment) => void;
  issues?: Record<string, AttachmentIssue[]>;
}) {
  return (
    <div className="attachment-list" aria-label="Anexos deste pedido">
      <h3>Anexos deste pedido</h3>
      {!selected.length && (
        <p className="guided-empty">
          Nenhum template aplicável. Apenas o pedido e o contexto revisado serão
          enviados.
        </p>
      )}
      {selected.map((attachment) => (
        <article className="attachment-card" key={attachment.template_id}>
          <div className="attachment-card-heading">
            <FileText size={20} aria-hidden="true" />
            <strong>
              {attachment.title ||
                catalog?.find(
                  (template) => template.id === attachment.template_id,
                )?.title ||
                attachment.template_id}{" "}
              · v{attachment.version ?? "—"}
            </strong>
            <span className="template-fill-state">
              {attachmentState(
                attachment,
                issues[attachment.template_id],
                catalog?.find(
                  (template) =>
                    template.version_id === attachment.source_version_id,
                ),
              )}
            </span>
          </div>
          <p className="small muted">
            Revisão do anexo {attachment.attachment_revision ?? "—"}
          </p>
          {attachment.category === "quality" && (
            <p className="small muted">
              Critérios propostos, ainda não testados
            </p>
          )}
          <details className="attachment-content">
            <summary>Conteúdo completo do anexo</summary>
            <pre>{attachment.edited_content}</pre>
          </details>
          {!!issues[attachment.template_id]?.length && (
            <ul className="attachment-pending">
              {issues[attachment.template_id].map((issue, index) => (
                <li key={`${issue.field_id || "content"}-${index}`}>
                  <button
                    className="text-button"
                    disabled={disable}
                    onClick={() => edit(attachment, issue.field_id)}
                  >
                    {issue.message}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="actions">
            <button className="text-button" onClick={() => open(attachment)}>
              <Eye size={16} aria-hidden="true" />
              Abrir
            </button>
            <button
              className="text-button"
              disabled={disable}
              onClick={() => edit(attachment)}
            >
              <Pencil size={15} aria-hidden="true" />
              Editar
            </button>
            <button
              className="text-button"
              disabled={disable}
              onClick={() => remove(attachment)}
            >
              <Trash2 size={15} aria-hidden="true" />
              Remover
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}
