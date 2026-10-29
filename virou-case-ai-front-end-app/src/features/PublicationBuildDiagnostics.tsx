import { useId, useState } from "react";
import { Copy, FileCode2 } from "lucide-react";
import { Link } from "react-router-dom";
import type {
  Operation,
  PublicationBuildDiagnostic,
  PublicationMigrationDiagnostic,
} from "../shared/types";

const categories: Record<PublicationBuildDiagnostic["category"], string> = {
  source: "Código do projeto",
  dependency: "Dependência do projeto",
  executor: "Executor de publicação",
  output: "Artefato de publicação",
  unknown: "Compilação",
};
const tools: Record<PublicationBuildDiagnostic["tool"], string> = {
  esbuild: "esbuild",
  python: "Python",
  sandbox: "Ambiente isolado",
  executor: "Executor",
};

function issueLocation(issue: PublicationBuildDiagnostic["issues"][number]) {
  if (!issue.path) return "Arquivo não identificado";
  return `${issue.path}${issue.line ? `:${issue.line}` : ""}${issue.line && issue.column ? `:${issue.column}` : ""}`;
}

function diagnosticReport(
  operation: Operation,
  build: PublicationBuildDiagnostic,
) {
  return [
    "Diagnóstico da compilação",
    `Operação: ${operation.id}`,
    `Correlação: ${build.correlation_id}`,
    `Revisão: ${build.revision_id}`,
    `Etapa: ${operation.result_ref.publication_failure?.stage || "publication_building"}`,
    `Categoria: ${categories[build.category] || "Compilação"}`,
    `Ferramenta: ${tools[build.tool] || "Executor"}`,
    `Código: ${operation.error_code || "BUILD_FAILED"}`,
    `Saída do processo: ${build.exit_code ?? "não informado"}`,
    ...build.issues.flatMap((issue, index) => [
      "",
      `${index + 1}. ${issue.code} · ${issueLocation(issue)}`,
      issue.message,
      issue.hint,
    ]),
    ...(build.output_truncated
      ? [
          "",
          "A saída excedeu o limite de captura. Corrija os problemas apresentados e publique a nova revisão para conferir os demais.",
        ]
      : []),
  ].join("\n");
}

function migrationReport(
  operation: Operation,
  diagnostic: PublicationMigrationDiagnostic,
) {
  return [
    "Diagnóstico das migrations",
    `Operação: ${operation.id}`,
    `Revisão: ${operation.result_ref.revision_id || "não informada"}`,
    `Etapa: ${operation.result_ref.publication_failure?.stage || "não informada"}`,
    `Código: ${diagnostic.code}`,
    ...(diagnostic.analysis_method === "sql_lexical_v2"
      ? ["Método: conferência estática dos comandos SQL e de suas posições."]
      : []),
    ...(diagnostic.published_revision_id
      ? [`Revisão publicada de referência: ${diagnostic.published_revision_id}`]
      : []),
    ...diagnostic.issues.flatMap((issue, index) => [
      "",
      `${index + 1}. ${issue.code} · ${issue.path || "Arquivo não identificado"}`,
      ...(issue.line
        ? [
            `Localização: linha ${issue.line}${issue.column ? `, coluna ${issue.column}` : ""}`,
          ]
        : []),
      ...(issue.statement_type ? [`Comando SQL: ${issue.statement_type}`] : []),
      ...(issue.operation ? [`Operação detectada: ${issue.operation}`] : []),
      issue.message,
      ...(issue.explanation ? [issue.explanation] : []),
      issue.hint,
    ]),
    ...(diagnostic.issue_count > diagnostic.issues.length
      ? [
          "",
          `Exibindo ${diagnostic.issues.length} de ${diagnostic.issue_count} problemas.`,
        ]
      : []),
  ].join("\n");
}

export function PublicationBuildDiagnostics({
  operation,
  projectId,
  currentRevisionId,
}: {
  operation: Operation;
  projectId: string;
  currentRevisionId: string | null;
}) {
  const titleId = useId();
  const [copyStatus, setCopyStatus] = useState("");
  const failure = operation.result_ref.publication_failure;
  const candidate = failure?.build;
  const build =
    candidate?.format_version === 1 && Array.isArray(candidate.issues)
      ? candidate
      : null;
  const migrationCandidate = failure?.migration;
  const migration =
    migrationCandidate?.format_version === 1 &&
    migrationCandidate.category === "migration" &&
    Array.isArray(migrationCandidate.issues)
      ? migrationCandidate
      : null;
  const isBuildFailure =
    operation.state === "failed" &&
    (!!candidate ||
      operation.error_code?.startsWith("BUILD_") ||
      failure?.stage === "publication_building");
  const isMigrationFailure =
    ["failed", "reconciling"].includes(operation.state) &&
    (!!migrationCandidate || operation.error_code?.startsWith("MIGRATION_"));
  if (!isBuildFailure && !isMigrationFailure) return null;
  const title = isMigrationFailure
    ? "Diagnóstico das migrations"
    : "Diagnóstico da compilação";
  const projectPath = `/app/projetos/${encodeURIComponent(projectId)}`;
  const diagnosticRevision = isMigrationFailure
    ? operation.result_ref.revision_id
    : build?.revision_id;
  const revisionChanged =
    diagnosticRevision &&
    currentRevisionId &&
    diagnosticRevision !== currentRevisionId;
  async function copyReport() {
    if (!build && !migration) return;
    try {
      await navigator.clipboard.writeText(
        migration
          ? migrationReport(operation, migration)
          : diagnosticReport(operation, build!),
      );
      setCopyStatus("Diagnóstico copiado.");
    } catch {
      setCopyStatus(
        "Não foi possível copiar. Selecione os detalhes exibidos nesta seção.",
      );
    }
  }
  return (
    <section
      className="panel publication-build-diagnostics"
      aria-labelledby={titleId}
    >
      <div className="publication-build-heading">
        <h2 id={titleId}>
          <FileCode2 size={20} aria-hidden="true" />
          {title}
        </h2>
        {(build || migration) && (
          <button className="button" onClick={() => void copyReport()}>
            <Copy size={16} aria-hidden="true" /> Copiar diagnóstico
          </button>
        )}
      </div>
      {migration ? (
        <>
          <p>
            Confira os arquivos e as orientações da conferência do banco. Os
            registros de migrations aplicadas precisam ser preservados.
          </p>
          {migration.issues.some((issue) => issue.reason === "unsafe_sql") && (
            <p className="notice publication-migration-method">
              Este bloqueio vem de regras automáticas de segurança sobre o SQL
              das migrations pendentes. A conferência identifica comandos que
              exigem revisão antes da execução no banco. A Análise IA abaixo
              ajuda a investigar e preparar uma correção; a publicação continua
              sujeita à conferência do SQL e dos registros já aplicados.
            </p>
          )}
          {migration.analysis_method === "sql_lexical_v2" && (
            <p className="small muted">
              A conferência lê cada comando SQL e sua posição. Comentários e
              valores de texto entre aspas não são tratados como comandos.
            </p>
          )}
          <dl className="publication-facts publication-build-facts">
            <div>
              <dt>Origem do problema</dt>
              <dd>Migrations do banco</dd>
            </div>
            <div>
              <dt>Revisão analisada</dt>
              <dd>{diagnosticRevision || "Não informada"}</dd>
            </div>
            {migration.published_revision_id && (
              <div>
                <dt>Revisão publicada de referência</dt>
                <dd>{migration.published_revision_id}</dd>
              </div>
            )}
          </dl>
          {revisionChanged && (
            <p className="notice">
              Este diagnóstico pertence a uma revisão anterior. Confira os
              arquivos da revisão indicada antes de alterar a revisão atual.
            </p>
          )}
          <ol className="publication-build-issues">
            {migration.issues.map((issue, index) => (
              <li key={`${issue.code}:${issue.path || ""}:${index}`}>
                <div className="publication-build-issue-heading">
                  <strong>
                    {issue.path || "Arquivo não identificado"}
                    {issue.line ? `:${issue.line}` : ""}
                    {issue.line && issue.column ? `:${issue.column}` : ""}
                  </strong>
                  <code>{issue.code}</code>
                </div>
                <p className="publication-build-message">{issue.message}</p>
                {(issue.statement_type || issue.operation || issue.line) && (
                  <dl className="publication-migration-evidence">
                    {issue.statement_type && (
                      <div>
                        <dt>Comando SQL</dt>
                        <dd>{issue.statement_type}</dd>
                      </div>
                    )}
                    {issue.operation && (
                      <div>
                        <dt>Operação detectada</dt>
                        <dd>{issue.operation}</dd>
                      </div>
                    )}
                    {issue.line && (
                      <div>
                        <dt>Localização</dt>
                        <dd>
                          Linha {issue.line}
                          {issue.column ? `, coluna ${issue.column}` : ""}
                        </dd>
                      </div>
                    )}
                  </dl>
                )}
                {issue.explanation && (
                  <p className="publication-build-message">
                    {issue.explanation}
                  </p>
                )}
                {issue.reason === "unsafe_sql" && !issue.statement_type && (
                  <p className="small muted">
                    Esta tentativa foi registrada sem a posição e o comando
                      detectados. Confira o SQL da revisão indicada e o contexto
                      disponível na Análise IA. O nome do arquivo, sozinho, não
                      comprova perda de dados.
                  </p>
                )}
                <p className="small muted">{issue.hint}</p>
              </li>
            ))}
          </ol>
          {migration.issue_count > migration.issues.length && (
            <p className="notice">
              Exibindo {migration.issues.length} de {migration.issue_count}{" "}
              problemas. Confira também os demais arquivos de migrations desta
              revisão.
            </p>
          )}
          <details className="publication-build-execution">
            <summary>Informações da conferência</summary>
            <dl className="publication-facts publication-build-facts">
              <div>
                <dt>Operação</dt>
                <dd>{operation.id}</dd>
              </div>
              <div>
                <dt>Código para suporte</dt>
                <dd>{migration.code}</dd>
              </div>
              {migration.issues.map((issue, index) => (
                <div key={`${issue.path || ""}:${index}`}>
                  <dt>{issue.path || `Problema ${index + 1}`}</dt>
                  <dd>
                    Origem do registro:{" "}
                    {issue.receipt_source === "remote"
                      ? "Banco remoto"
                      : issue.receipt_source === "inventory"
                        ? "Histórico da publicação"
                        : "Revisão candidata"}
                  </dd>
                  {issue.expected_sha256 && (
                    <dd>SHA-256 registrado: {issue.expected_sha256}</dd>
                  )}
                  {issue.actual_sha256 && (
                    <dd>
                      {[
                        "receipt_conflict",
                        "pending_receipt_conflict",
                      ].includes(issue.reason)
                        ? "SHA-256 do recibo remoto"
                        : "SHA-256 da revisão"}
                      : {issue.actual_sha256}
                    </dd>
                  )}
                </div>
              ))}
            </dl>
          </details>
        </>
      ) : build ? (
        <>
          <p>
            A revisão não pôde ser preparada para publicação.{" "}
            {build.category === "source" || build.category === "dependency"
              ? "Confira os problemas encontrados e salve uma revisão corrigida antes de publicar novamente."
              : "Confira as orientações do diagnóstico antes de tentar publicar novamente."}
          </p>
          <dl className="publication-facts publication-build-facts">
            <div>
              <dt>Origem do problema</dt>
              <dd>{categories[build.category] || "Compilação"}</dd>
            </div>
            <div>
              <dt>Ferramenta</dt>
              <dd>{tools[build.tool] || "Executor"}</dd>
            </div>
            <div>
              <dt>Revisão analisada</dt>
              <dd>{build.revision_id}</dd>
            </div>
            <div>
              <dt>Referência para os logs</dt>
              <dd>{build.correlation_id}</dd>
            </div>
          </dl>
          {revisionChanged && (
            <p className="notice">
              Este diagnóstico pertence a uma revisão anterior. O código atual
              do projeto já está em outra revisão; confira o histórico antes de
              relacionar as posições indicadas ao conteúdo atual.
            </p>
          )}
          {!!build.issues.length ? (
            <ol className="publication-build-issues">
              {build.issues.map((issue, index) => (
                <li key={`${issue.code}:${issue.path || ""}:${index}`}>
                  <div className="publication-build-issue-heading">
                    <strong>{issueLocation(issue)}</strong>
                    <code>{issue.code}</code>
                  </div>
                  <p className="publication-build-message">{issue.message}</p>
                  <p className="small muted">{issue.hint}</p>
                </li>
              ))}
            </ol>
          ) : (
            <p className="notice">
              O executor não informou uma posição específica no código. Use a
              referência da operação para conferir o diagnóstico nos logs.
            </p>
          )}
          {build.output_truncated && (
            <p className="notice">
              A saída excedeu o limite de captura. Corrija os problemas
              apresentados e publique a nova revisão para conferir os demais.
            </p>
          )}
          <details className="publication-build-execution">
            <summary>Informações da execução</summary>
            <dl className="publication-facts publication-build-facts">
              <div>
                <dt>Código de saída</dt>
                <dd>{build.exit_code ?? "Não informado"}</dd>
              </div>
              <div>
                <dt>Duração</dt>
                <dd>
                  {(build.duration_ms / 1000).toLocaleString("pt-BR", {
                    maximumFractionDigits: 2,
                  })}{" "}
                  s
                </dd>
              </div>
              <div>
                <dt>Saída padrão capturada</dt>
                <dd>{build.stdout_bytes} bytes</dd>
              </div>
              <div>
                <dt>Saída de erro capturada</dt>
                <dd>{build.stderr_bytes} bytes</dd>
              </div>
            </dl>
          </details>
          <p className="small muted">
            As mensagens são sanitizadas no servidor. Trechos do código e
            credenciais não fazem parte deste relatório.
          </p>
        </>
      ) : (
        <p className="notice">
          {isMigrationFailure
            ? "Esta tentativa não possui um diagnóstico detalhado de migrations armazenado. Consulte a mensagem da operação e confira os registros antes de continuar."
            : "Esta tentativa não possui um diagnóstico detalhado de compilação armazenado. Uma nova publicação registrará os detalhes disponíveis do compilador."}{" "}
          Referência da tentativa: {operation.id}.
        </p>
      )}
      <div className="actions">
        <Link className="button primary" to={projectPath}>
          Revisar código do projeto
        </Link>
        <Link className="button" to={`${projectPath}/historico`}>
          Consultar revisões
        </Link>
      </div>
      <p className="small" role="status">
        {copyStatus}
      </p>
    </section>
  );
}
