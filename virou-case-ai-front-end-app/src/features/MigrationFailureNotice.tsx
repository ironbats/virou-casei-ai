import { ApiError } from "../shared/api";
import type { MigrationFailure } from "../shared/types";
import { MigrationHistoryNotice } from "./MigrationHistoryNotice";

const validationCodes = new Set([
  "MIGRATION_VALIDATION_FAILED",
  "MIGRATION_BASELINE_UNVERIFIABLE",
  "MIGRATION_BASELINE_INCONSISTENT",
  "MIGRATION_REVIEW_REQUIRED",
  "MIGRATION_POLICY_REVIEW_REQUIRED",
  "MIGRATION_CHECKSUM_CONFLICT",
]);

export function MigrationFailureNotice({
  error,
  failure,
  messageShown,
}: {
  error?: unknown;
  failure?: MigrationFailure;
  messageShown?: string;
}) {
  const code = error instanceof ApiError ? error.code : failure?.code;
  if (!code) return null;
  if (!validationCodes.has(code))
    return <MigrationHistoryNotice error={error} failure={failure} />;
  const details = (error instanceof ApiError ? error.details : failure) as
    MigrationFailure | undefined;
  const diagnostic = details?.migration;
  if (!diagnostic || !Array.isArray(diagnostic.issues)) return null;
  const message = error instanceof ApiError ? undefined : failure?.message;
  return (
    <div className="migration-history-error" role="status">
      <strong>Validação das migrations</strong>
      {message && message !== messageShown && <p>{message}</p>}
      {details?.base_revision_id && (
        <p>
          Revisão-base: <code>{details.base_revision_id}</code>
        </p>
      )}
      <ul>
        {diagnostic.issues.map((issue, index) => (
          <li key={`${index}:${issue.path}`}>
            <code>{issue.path || "Migration não informada"}</code>
            {issue.statement_index != null &&
              ` · Instrução ${issue.statement_index}`}
            {issue.line != null && ` · Linha ${issue.line}`}
            {issue.column != null && ` · Coluna ${issue.column}`}
            {issue.message && <p>{issue.message}</p>}
            {issue.hint && <p>{issue.hint}</p>}
          </li>
        ))}
      </ul>
      {!!diagnostic.issue_count &&
        diagnostic.issue_count > diagnostic.issues.length && (
          <p>
            Exibindo {diagnostic.issues.length} de {diagnostic.issue_count}{" "}
            ocorrências.
          </p>
        )}
      <p>
        A revisão salva permanece intacta. Corrija o plano incremental antes de
        solicitar a aplicação das alterações.
      </p>
    </div>
  );
}
