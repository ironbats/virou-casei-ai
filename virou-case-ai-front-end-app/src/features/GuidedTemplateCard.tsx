import type { Attachment, Template } from "./guidedTypes";
export function GuidedTemplateCard({
  template,
  attachment: a,
  category,
  recommended,
  disabled,
  atLimit,
  update,
  edit,
}: {
  template: Template;
  attachment: Attachment;
  category: string;
  recommended: boolean;
  disabled: boolean;
  atLimit: boolean;
  update: (value: Attachment) => void;
  edit: (value: Attachment) => void;
}) {
  const newer = template.version_id !== a.source_version_id;
  return (
    <article className={"template-card " + (a.enabled ? "selected" : "")}>
      <label className="template-check">
        <input
          type="checkbox"
          checked={a.enabled}
          disabled={disabled || (!a.enabled && atLimit)}
          onChange={(e) => update({ ...a, enabled: e.target.checked })}
        />
        <span className="sr-only">Selecionar {template.title}</span>
      </label>
      <div>
        <strong>{template.title}</strong>
        <div className="template-badges">
          <span>{category}</span>
          <span>v{a.version || template.version}</span>
          {recommended && <span className="badge soft">Recomendado</span>}
          {(a.edited || (!newer && a.edited_content !== template.content)) && (
            <span className="badge soft">Editado por você</span>
          )}
        </div>
        <p>{template.summary}</p>
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
                });
            }}
          >
            Usar versão {template.version} do catálogo
          </button>
        )}
      </div>
      <button className="button" disabled={disabled} onClick={() => edit(a)}>
        Editar
      </button>
    </article>
  );
}
