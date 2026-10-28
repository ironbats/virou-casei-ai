import { FileText } from "lucide-react";
import type { Attachment, Template } from "./guidedTypes";
export function BuildAttachments({
  selected,
  catalog,
  disable,
  open,
  edit,
  remove,
}: {
  selected: Attachment[];
  catalog: Template[] | null;
  disable: boolean;
  open: (attachment: Attachment) => void;
  edit: (attachment: Attachment) => void;
  remove: (attachment: Attachment) => void;
}) {
  if (!selected.length) return null;
  return (
    <div className="attachment-list" aria-label="Templates anexados">
      <strong>Templates anexados ({selected.length})</strong>
      {selected.map((a) => (
        <article className="attachment-card" key={a.template_id}>
          <FileText size={20} />
          <div>
            <strong>
              {a.title ||
                catalog?.find((t) => t.id === a.template_id)?.title ||
                a.template_id}
            </strong>
            <p className="small">
              v{a.version || 1}
              {a.edited ? " · Editado por você" : ""}
            </p>
          </div>
          <div className="actions">
            <button className="text-button" onClick={() => open(a)}>
              Abrir
            </button>
            <button
              className="text-button"
              disabled={disable}
              onClick={() => edit(a)}
            >
              Editar
            </button>
            <button
              className="text-button"
              disabled={disable}
              onClick={() => remove(a)}
            >
              Remover
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}
