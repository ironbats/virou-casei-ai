import { useRef, type Dispatch, type SetStateAction } from "react";
import { command } from "../shared/api";
import { ErrorNotice, Modal } from "../shared/ui";
import type { Project, ProjectDraft, Capabilities } from "../shared/types";
import type { useDraft } from "./useDraft";
import { DraftConflict } from "./DraftConflict";
import { ProposalReview } from "./ProposalReview";
import type { ReviewSelection, ReviewView } from "./proposalTypes";

type Setter<T> = Dispatch<SetStateAction<T>>;
type Review = ReviewSelection;
type Remote = { project: Project; files: Record<string, string> } | null;
type Props = {
  review: Review;
  setReview: Setter<Review>;
  remote: Remote;
  setRemote: Setter<Remote>;
  exporting: boolean;
  setExporting: Setter<boolean>;
  newFile: boolean;
  setNewFile: Setter<boolean>;
  editing: boolean;
  setEditing: Setter<boolean>;
  baseFiles: Record<string, string>;
  setBaseFiles: Setter<Record<string, string> | null>;
  files: Record<string, string>;
  error: unknown;
  setError: Setter<unknown>;
  blocked: boolean;
  dirty: boolean;
  busy: boolean;
  accept: () => Promise<void>;
  discard: () => Promise<void>;
  refreshReview: () => Promise<void>;
  compareCurrent: () => Promise<void>;
  project: Project;
  setProject: Setter<Project | null>;
  draft: ReturnType<typeof useDraft<ProjectDraft>>;
  caps: Capabilities;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  save: () => Promise<string | undefined>;
  setMessage: Setter<string>;
  exportProject: () => Promise<void>;
  setSelected: Setter<string>;
  pid: string;
};
export function WorkspaceDialogs({
  review,
  setReview,
  remote,
  setRemote,
  exporting,
  setExporting,
  newFile,
  setNewFile,
  editing,
  setEditing,
  baseFiles,
  setBaseFiles,
  files,
  error,
  setError,
  blocked,
  dirty,
  busy,
  accept,
  discard,
  refreshReview,
  compareCurrent,
  project,
  setProject,
  draft,
  caps,
  act,
  save,
  setMessage,
  exportProject,
  setSelected,
  pid,
}: Props) {
  const reviewMemory = useRef(new Map<string, ReviewView>());
  return (
    <>
      {review && (
        <ProposalReview
          key={review.proposal.proposal_version}
          pid={pid}
          proposal={review.proposal}
          busy={busy}
          blocked={blocked}
          dirty={dirty}
          error={error}
          memory={reviewMemory.current}
          onClose={() => setReview(null)}
          onApply={accept}
          onDiscard={discard}
          onRefresh={refreshReview}
          onCompare={compareCurrent}
        />
      )}
      {remote && (
        <Modal
          title="Compare a revisão mais recente"
          close={() => setRemote(null)}
        >
          <p>
            Reaplicar transporta os arquivos que você alterou para a nova base.
            Revise colisões antes de salvar.
          </p>
          <div className="compare-grid">
            <div>
              <h3>Seu rascunho</h3>
              <pre className="source-preview">
                {JSON.stringify(files, null, 2)}
              </pre>
            </div>
            <div>
              <h3>Revisão salva</h3>
              <pre className="source-preview">
                {JSON.stringify(remote.files, null, 2)}
              </pre>
            </div>
          </div>
          <div className="actions">
            <button
              className="button"
              onClick={() => {
                setProject(remote.project);
                setBaseFiles(remote.files);
                draft.setData((d) => ({
                  ...d,
                  base_revision_id: remote.project.current_revision_id,
                  files: null,
                }));
                setRemote(null);
                setError(null);
              }}
            >
              Carregar revisão salva
            </button>
            <button
              className="button primary"
              onClick={() => {
                const merged = { ...remote.files };
                for (const path of new Set([
                  ...Object.keys(baseFiles),
                  ...Object.keys(files),
                ])) {
                  if (baseFiles[path] !== files[path]) {
                    if (path in files) merged[path] = files[path];
                    else delete merged[path];
                  }
                }
                setProject(remote.project);
                setBaseFiles(remote.files);
                draft.setData((d) => ({
                  ...d,
                  base_revision_id: remote.project.current_revision_id,
                  files: merged,
                }));
                setRemote(null);
                setError(null);
              }}
            >
              Reaplicar minhas alterações
            </button>
          </div>
        </Modal>
      )}
      {draft.conflict && (
        <DraftConflict
          local={draft.data}
          remote={draft.conflict.data}
          onResolve={draft.resolve}
        />
      )}
      {exporting && (
        <Modal title="Baixar seu projeto" close={() => setExporting(false)}>
          <p>
            O ZIP contém a revisão salva{" "}
            {project.current_revision_id?.slice(0, 8)}.{" "}
            {dirty
              ? "Seu rascunho tem alterações que ainda não fazem parte dessa revisão."
              : ""}
          </p>
          <div className="actions">
            {dirty && (
              <button
                className="button"
                disabled={blocked}
                onClick={() =>
                  void act(async () => {
                    await save();
                    setExporting(false);
                    setMessage(
                      "Revisão salva. Você já pode baixar o projeto atualizado.",
                    );
                  })
                }
              >
                Salvar primeiro
              </button>
            )}
            <button
              className="button primary"
              disabled={busy}
              onClick={exportProject}
            >
              Baixar revisão salva
            </button>
          </div>
        </Modal>
      )}
      {newFile && (
        <Modal title="Novo arquivo" close={() => setNewFile(false)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const path = String(new FormData(e.currentTarget).get("path"));
              if (
                path in files ||
                Object.keys(files).length >= caps.limits.max_files
              ) {
                setError(
                  new Error(
                    "Arquivo existente ou limite de arquivos atingido.",
                  ),
                );
                return;
              }
              draft.setData((d) => ({ ...d, files: { ...files, [path]: "" } }));
              setSelected(path);
              setNewFile(false);
            }}
          >
            <label>
              Caminho relativo
              <input
                name="path"
                required
                maxLength={caps.limits.max_path_length}
                pattern="(?!/)(?!.*\.\.)(?!.*\\\\).+"
                placeholder="public/novo-arquivo.js"
              />
            </label>
            <button className="button primary">Adicionar arquivo</button>
          </form>
        </Modal>
      )}
      {editing && (
        <Modal title="Editar projeto" close={() => setEditing(false)}>
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              void act(async () => {
                setProject(
                  await command<Project>(
                    `/projects/${pid}`,
                    {
                      name: form.get("name"),
                      objective: form.get("objective"),
                    },
                    "PATCH",
                    project.version,
                  ),
                );
                setEditing(false);
              });
            }}
          >
            <label>
              Nome
              <input
                name="name"
                defaultValue={project.name}
                required
                maxLength={100}
              />
            </label>
            <label>
              Objetivo
              <textarea
                name="objective"
                defaultValue={project.objective}
                required
                minLength={10}
                maxLength={3000}
              />
            </label>
            <ErrorNotice error={error} />
            <button className="button primary" disabled={busy}>
              Salvar
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
