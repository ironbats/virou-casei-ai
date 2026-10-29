import { useId, useState } from "react";
import { Copy, FileCode2 } from "lucide-react";
import { Link } from "react-router-dom";
import type { Operation, PublicationBuildDiagnostic } from "../shared/types";

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
  const isBuildFailure =
    operation.state === "failed" &&
    (!!candidate ||
      operation.error_code?.startsWith("BUILD_") ||
      failure?.stage === "publication_building");
  if (!isBuildFailure) return null;
  const projectPath = `/app/projetos/${encodeURIComponent(projectId)}`;
  const revisionChanged =
    build?.revision_id &&
    currentRevisionId &&
    build.revision_id !== currentRevisionId;
  async function copyReport() {
    if (!build) return;
    try {
      await navigator.clipboard.writeText(diagnosticReport(operation, build));
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
          Diagnóstico da compilação
        </h2>
        {build && (
          <button className="button" onClick={() => void copyReport()}>
            <Copy size={16} aria-hidden="true" /> Copiar diagnóstico
          </button>
        )}
      </div>
      {build ? (
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
          Esta tentativa não possui um diagnóstico detalhado de compilação
          armazenado. Uma nova publicação registrará os detalhes disponíveis do
          compilador. Referência da tentativa: {operation.id}.
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
