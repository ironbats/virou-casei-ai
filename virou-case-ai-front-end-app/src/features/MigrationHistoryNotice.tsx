import { ApiError } from "../shared/api";
import type { MigrationFailure } from "../shared/types";

const operations: Record<string, string> = {
  modify: "Alteração de conteúdo",
  delete: "Exclusão",
  rename_or_alias: "Renomeação ou caminho equivalente",
  duplicate_path: "Caminho duplicado",
  insert_before_history: "Nova migration fora da ordem incremental",
};

export function MigrationHistoryNotice({
  error,
  failure,
}: {
  error?: unknown;
  failure?: MigrationFailure;
}) {
  const code = error instanceof ApiError ? error.code : failure?.code;
  if (
    !code ||
    !["MIGRATION_HISTORY_IMMUTABLE", "MIGRATION_ORDER_INVALID"].includes(code)
  )
    return null;
  const details = (error instanceof ApiError ? error.details : failure) as
    MigrationFailure | undefined;
  const history = details?.migration_history;
  if (history?.format_version !== 1 || !Array.isArray(history.issues))
    return null;
  return (
    <div className="migration-history-error" role="status">
      <strong>
        Arquivos protegidos da revisão{" "}
        {history.base_revision_id || "informada pelo servidor"}
      </strong>
      <ul>
        {history.issues.map((issue, index) => (
          <li key={`${index}:${issue.path}`}>
            <code>{issue.path || "Caminho não informado"}</code>
            {" · "}
            {operations[issue.operation || ""] || "Operação recusada"}
            {issue.after_path ? ` → ${issue.after_path}` : ""}
          </li>
        ))}
      </ul>
      {!!history.issue_count && history.issue_count > history.issues.length && (
        <p>
          Exibindo {history.issues.length} de {history.issue_count} conflitos.
        </p>
      )}
      <p>
        A revisão salva permanece intacta. Preserve os arquivos históricos e
        crie uma nova migration incremental para evoluir o banco.
      </p>
    </div>
  );
}
