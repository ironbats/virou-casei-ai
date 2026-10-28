import { useState } from "react";
import { Modal } from "../shared/ui";
import type { Attachment, Template } from "./guidedTypes";

export function TemplateEditor({
  template,
  attachment,
  limit,
  onSave,
  close,
}: {
  template: Template;
  attachment: Attachment;
  limit: number;
  onSave: (value: Attachment) => Promise<void>;
  close: () => void;
}) {
  const [value, setValue] = useState(attachment),
    [manual, setManual] = useState(
      attachment.edited_content !== template.content,
    );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const dirty = JSON.stringify(value) !== JSON.stringify(attachment);
  function leave() {
    if (
      !dirty ||
      confirm("Descartar as alterações ainda não salvas deste anexo?")
    )
      close();
  }
  function interpolate(fields: Record<string, string>) {
    return template.content.replace(
      /\{\{(\w+)\}\}/g,
      (all, key) => fields[key]?.trim() || all,
    );
  }
  return (
    <Modal title={`Editar · ${template.title}`} close={leave}>
      <p>
        Esta edição pertence apenas ao próximo pedido. Versão de origem{" "}
        {template.version}.
      </p>
      <details>
        <summary>Conteúdo-base do catálogo</summary>
        <pre className="source-preview">{template.content}</pre>
      </details>
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            await onSave(value);
            close();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {template.fields.map((field) => (
          <label key={field.key}>
            {field.label}
            {field.required ? " *" : ""}
            <textarea
              value={value.fields[field.key] || ""}
              maxLength={field.max_length}
              rows={2}
              onChange={(e) => {
                const fields = { ...value.fields, [field.key]: e.target.value };
                setValue({
                  ...value,
                  fields,
                  edited_content: manual
                    ? value.edited_content
                    : interpolate(fields),
                });
              }}
            />
          </label>
        ))}
        {manual && template.fields.length > 0 && (
          <button
            type="button"
            className="text-button"
            onClick={() => {
              if (
                confirm(
                  "Substituir o texto personalizado pelo conteúdo-base com os campos preenchidos?",
                )
              ) {
                setValue({
                  ...value,
                  edited_content: interpolate(value.fields),
                });
                setManual(false);
              }
            }}
          >
            Atualizar texto a partir dos campos
          </button>
        )}
        <label>
          Conteúdo que será enviado
          <textarea
            rows={8}
            value={value.edited_content}
            aria-invalid={value.edited_content.length > limit}
            onChange={(e) => {
              setManual(true);
              setValue({ ...value, edited_content: e.target.value });
            }}
          />
        </label>
        <p className="small muted">
          {value.edited_content.length}/{limit} caracteres
        </p>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
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
    </Modal>
  );
}
