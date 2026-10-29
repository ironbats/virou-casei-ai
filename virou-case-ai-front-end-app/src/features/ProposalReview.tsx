import { MigrationFailureNotice } from "./MigrationFailureNotice";
import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  FileCode2,
  Maximize2,
  Minimize2,
  Search,
  X,
} from "lucide-react";
import { api } from "../shared/api";
import { ErrorNotice, Loading } from "../shared/ui";
import { ProposalDiffView } from "./ProposalDiffView";
import type { Proposal, ProposalContent, ReviewView } from "./proposalTypes";
import "./proposalReview.css";

const changes = {
  created: "Novo arquivo",
  modified: "Alterado",
  deleted: "Excluído",
};
const initialView = (file: string): ReviewView => ({
  file,
  page: 0,
  scroll: 0,
  wrap: false,
  expanded: false,
  mode: "unified",
});

export function ProposalReview({
  pid,
  proposal,
  busy,
  blocked,
  dirty,
  error,
  onClose,
  onApply,
  onDiscard,
  onRefresh,
  onCompare,
  memory,
}: {
  pid: string;
  proposal: Proposal;
  busy: boolean;
  blocked: boolean;
  dirty: boolean;
  error: unknown;
  onClose: () => void;
  onApply: () => Promise<void>;
  onDiscard: () => Promise<void>;
  onRefresh: () => Promise<void>;
  onCompare: () => Promise<void>;
  memory: Map<string, ReviewView>;
}) {
  const identity = `${pid}:${proposal.proposal_id}:${proposal.proposal_version}`;
  const [selected, setSelected] = useState(
      () => memory.get(identity)?.file ?? proposal.files[0]?.id ?? "",
    ),
    [filter, setFilter] = useState(""),
    [content, setContent] = useState<ProposalContent | null>(null),
    [comparisonReady, setComparisonReady] = useState(false),
    [loadError, setLoadError] = useState<unknown>(null),
    [retry, setRetry] = useState(0),
    [expanded, setExpanded] = useState(false),
    [confirmDiscard, setConfirmDiscard] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null),
    heading = useRef<HTMLHeadingElement>(null),
    cache = useRef(new Map<string, ProposalContent>()),
    pending = useRef(false);
  const file =
    proposal.files.find((item) => item.id === selected) ?? proposal.files[0];
  const migrationHistory =
    proposal.migration_history?.format_version === 1
      ? proposal.migration_history
      : null;
  const filtered = proposal.files.filter((item) =>
    item.path.toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
  );
  const canApply =
    !blocked &&
    !dirty &&
    !busy &&
    !loadError &&
    comparisonReady &&
    proposal.state === "generated" &&
    proposal.files.length > 0;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    const element = dialog.current;
    document.body.style.overflow = "hidden";
    element?.showModal();
    heading.current?.focus();
    return () => {
      element?.close();
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  useEffect(() => {
    if (!file) return;
    setComparisonReady(false);
    memory.set(identity, {
      ...initialView(file.id),
      ...memory.get(identity),
      file: file.id,
    });
    const cached = cache.current.get(file.id);
    setContent(cached ?? null);
    setLoadError(null);
    if (cached) return;
    const controller = new AbortController();
    let alive = true;
    const timeout = setTimeout(() => controller.abort(), 15_000);
    void api<ProposalContent>(
      `/projects/${pid}/proposals/${proposal.proposal_id}/files/${file.id}?proposal_version=${encodeURIComponent(proposal.proposal_version)}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!alive) return;
        if (
          result.proposal_id !== proposal.proposal_id ||
          result.proposal_version !== proposal.proposal_version ||
          result.id !== file.id ||
          result.path !== file.path
        )
          throw new Error(
            "O arquivo recebido não corresponde à proposta em revisão. Atualize a proposta.",
          );
        cache.current.set(file.id, result);
        setContent(result);
      })
      .catch((failure) => {
        if (alive)
          setLoadError(
            failure instanceof TypeError || controller.signal.aborted
              ? new Error(
                  "Não foi possível carregar este arquivo. Sua proposta foi preservada; tente novamente.",
                )
              : failure,
          );
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      alive = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [identity, file?.id, retry]);
  async function action(run: () => Promise<void>) {
    if (pending.current || busy) return;
    pending.current = true;
    try {
      await run();
    } finally {
      pending.current = false;
    }
  }
  return (
    <dialog
      className={`proposal-review${expanded ? " expanded" : ""}`}
      ref={dialog}
      aria-labelledby="proposal-review-title"
      aria-describedby="proposal-review-scope"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy && !pending.current) onClose();
      }}
    >
      <header className="proposal-review-header">
        <div className="proposal-review-title">
          <div>
            <h2 id="proposal-review-title" ref={heading} tabIndex={-1}>
              Revise as alterações propostas
            </h2>
            <p className="small muted">
              Proposta {proposal.proposal_id.slice(0, 8)} · Base{" "}
              {proposal.base_project_revision.slice(0, 8)} ·{" "}
              {proposal.files.length} arquivo(s)
            </p>
          </div>
          <div className="actions">
            <button
              className="icon-button proposal-expand"
              aria-label={expanded ? "Reduzir revisão" : "Ampliar revisão"}
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? <Minimize2 size={19} /> : <Maximize2 size={19} />}
            </button>
            <button
              className="icon-button"
              aria-label="Fechar revisão"
              disabled={busy}
              onClick={onClose}
            >
              <X size={21} />
            </button>
          </div>
        </div>
        <div
          className="proposal-verifications"
          aria-label="Verificações da proposta"
        >
          {proposal.verification_results.map((check) => (
            <span
              key={check.kind}
              className={check.state === "passed" ? "checked" : "pending"}
            >
              {check.state === "passed" ? (
                <CheckCircle2 size={14} aria-hidden="true" />
              ) : (
                <AlertCircle size={14} aria-hidden="true" />
              )}
              {check.message}
            </span>
          ))}
        </div>
        {migrationHistory &&
          (migrationHistory.preserved.length > 0 ||
            migrationHistory.added.length > 0) && (
            <details className="proposal-migration-history">
              <summary>
                Histórico de migrations · {migrationHistory.preserved.length}{" "}
                preservada(s) · {migrationHistory.added.length} nova(s)
              </summary>
              <div className="proposal-migration-lists">
                <section aria-label="Migrations preservadas">
                  <h3>Preservadas sem alteração</h3>
                  {migrationHistory.preserved.length ? (
                    <ul>
                      {migrationHistory.preserved.map((migration) => (
                        <li
                          key={migration.path}
                          title={`SHA-256: ${migration.sha256}`}
                        >
                          {migration.path}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>Nenhuma migration na revisão-base.</p>
                  )}
                </section>
                <section aria-label="Novas migrations">
                  <h3>Novos arquivos incrementais</h3>
                  {migrationHistory.added.length ? (
                    <ul>
                      {migrationHistory.added.map((migration) => (
                        <li
                          key={migration.path}
                          title={`SHA-256: ${migration.sha256}`}
                        >
                          {migration.path}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>Nenhuma migration nova nesta proposta.</p>
                  )}
                </section>
              </div>
              <p>
                Aplicar consolida os novos arquivos e passa a protegê-los. O SQL
                só será enviado ao banco durante a publicação.
              </p>
            </details>
          )}
        {proposal.state === "conflict" && (
          <p className="proposal-alert" role="alert">
            O projeto ou rascunho mudou. A proposta e suas edições foram
            preservadas.
            {proposal.conflict_paths.length > 0 &&
              ` Arquivos: ${proposal.conflict_paths.join(", ")}.`}{" "}
            <button
              className="text-button"
              disabled={busy}
              onClick={() => void action(onCompare)}
            >
              Comparar revisão atual
            </button>
          </p>
        )}
        {proposal.state === "applied" && (
          <p className="proposal-alert" role="status">
            Aplicação confirmada pelo servidor na revisão{" "}
            {proposal.applied_revision_id?.slice(0, 8)}.
          </p>
        )}
        {proposal.state === "discarded" && (
          <p className="proposal-alert" role="status">
            Esta proposta foi descartada. Os arquivos atuais foram preservados.
          </p>
        )}
        {dirty && (
          <p className="proposal-alert">
            Salve ou revise seu rascunho antes de aplicar.
          </p>
        )}
        {!!error && (
          <div className="proposal-review-error">
            <ErrorNotice error={error} />
            <MigrationFailureNotice error={error} />
            <button
              className="text-button"
              disabled={busy}
              onClick={() => void action(onRefresh)}
            >
              Consultar estado da proposta
            </button>
          </div>
        )}
      </header>
      <div className="proposal-review-body">
        <aside className="proposal-file-list">
          <label className="proposal-file-search">
            <Search size={16} aria-hidden="true" />
            <span className="sr-only">Buscar arquivo da proposta</span>
            <input
              type="search"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Buscar arquivo"
            />
          </label>
          <nav aria-label="Arquivos da proposta">
            {filtered.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-current={item.id === file?.id ? "true" : undefined}
                onClick={() => setSelected(item.id)}
                title={item.path}
              >
                <FileCode2 size={17} aria-hidden="true" />
                <span>
                  {item.path}
                  <small>
                    {changes[item.change] ?? "Operação não textual"}
                  </small>
                </span>
                <b aria-hidden="true">
                  {item.change === "created"
                    ? "+"
                    : item.change === "deleted"
                      ? "−"
                      : "±"}
                </b>
              </button>
            ))}
          </nav>
          {!filtered.length && (
            <p className="small muted">Nenhum arquivo encontrado.</p>
          )}
        </aside>
        <section className="proposal-file-main" aria-label="Arquivo em revisão">
          <div className="proposal-file-heading">
            <div className="proposal-desktop-path">
              <strong>{file?.path ?? "Nenhuma alteração"}</strong>
              <span className="small muted">
                {file ? changes[file.change] : ""}
              </span>
            </div>
            <label className="proposal-mobile-files">
              Arquivo em revisão
              <select
                value={file?.id ?? ""}
                onChange={(event) => setSelected(event.target.value)}
              >
                {proposal.files.map((item) => (
                  <option value={item.id} key={item.id}>
                    {item.path} · {changes[item.change]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!file ? (
            <p className="proposal-empty">
              A proposta não contém arquivos alterados.
            </p>
          ) : loadError ? (
            <div className="proposal-file-error">
              <ErrorNotice error={loadError} />
              <button
                className="button"
                onClick={() => setRetry((value) => value + 1)}
              >
                Tentar carregar arquivo
              </button>
            </div>
          ) : !content ? (
            <div className="proposal-file-loading">
              <Loading />
            </div>
          ) : content.content_type !== "text" ? (
            <p className="proposal-empty">
              Este arquivo não tem comparação textual disponível. Operação:{" "}
              {changes[content.change] ?? content.change}. A seleção de arquivo
              não altera o conjunto da proposta.
            </p>
          ) : (
            <ProposalDiffView
              key={`${identity}:${content.id}`}
              content={content}
              initialView={
                memory.get(`${identity}:${content.id}`) ??
                initialView(content.id)
              }
              remember={(view) => memory.set(`${identity}:${content.id}`, view)}
              onReady={setComparisonReady}
            />
          )}
        </section>
      </div>
      <footer className="proposal-review-footer">
        {confirmDiscard ? (
          <>
            <p id="proposal-review-scope">
              Descartar esta proposta preserva a revisão atual e encerra a
              possibilidade de aplicar este conjunto.
            </p>
            <div className="proposal-footer-actions">
              <button
                className="button"
                disabled={busy}
                onClick={() => setConfirmDiscard(false)}
              >
                Continuar revisão
              </button>
              <button
                className="button danger"
                disabled={busy}
                onClick={() => void action(onDiscard)}
              >
                Confirmar descarte
              </button>
            </div>
          </>
        ) : (
          <>
            <p id="proposal-review-scope">
              Aplicar confirma o conjunto de{" "}
              <strong>{proposal.files.length} arquivo(s)</strong>, incluindo os
              que não estão abertos. A revisão humana não substitui testes.
            </p>
            <div className="proposal-footer-actions">
              <button className="button" disabled={busy} onClick={onClose}>
                Voltar
              </button>
              <button
                className="button"
                disabled={
                  busy ||
                  proposal.state === "applied" ||
                  proposal.state === "discarded"
                }
                onClick={() => setConfirmDiscard(true)}
              >
                Descartar proposta
              </button>
              <button
                className="button primary"
                disabled={!canApply}
                onClick={() => void action(onApply)}
              >
                {busy ? "Confirmando estado…" : "Aplicar alterações revisadas"}
              </button>
            </div>
          </>
        )}
      </footer>
    </dialog>
  );
}
