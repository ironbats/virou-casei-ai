type VerificationFailure = {
  code?: string;
  message?: string;
  stage?: string;
  stack?: string | null;
};

export type Verification = {
  id: string;
  state: string;
  stage: string;
  error_code: string | null;
  failed_stage?: string | null;
  failure?: VerificationFailure | null;
  stack?: string | null;
  stack_results?: {
    stack: string;
    passed: boolean;
    failure?: VerificationFailure | null;
  }[];
  temporary_resources?: boolean;
  cleanup?: {
    state: string;
    worker_removed?: boolean;
    database_removed?: boolean;
  } | null;
  resources?: {
    stack: string;
    worker_name: string;
    database_id?: string | null;
    url?: string | null;
    state: string;
    worker_confirmed?: boolean;
    deployment_id?: string | null;
  }[];
};

function resourceUrl(value?: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

const verificationStates: Record<string, string> = {
  queued: "Na fila de verificação",
  running: "Verificação em andamento",
  reconciling: "Verificação aguardando retomada",
  succeeded: "Verificação concluída",
  failed: "Verificação com falha",
  cancelled: "Verificação cancelada",
};

const verificationStages: Record<string, string> = {
  verification_queued: "Aguardando o executor",
  verification_initializing: "Preparando a verificação",
  verification_isolation: "Conferindo o isolamento do executor",
  verification_configuration: "Conferindo a configuração",
  verification_cloudflare: "Conectando à Cloudflare",
  verification_d1: "Preparando o banco D1",
  verification_building: "Compilando o projeto de verificação",
  verification_uploading: "Enviando o Worker",
  verification_bindings: "Conferindo a conexão com o banco",
  verification_routing: "Configurando o endereço público",
  verification_remote: "Testando a aplicação publicada",
  verification_cleanup: "Conferindo recursos da verificação",
  verification_retained: "Recursos preservados; aguardando retomada",
  verification_partial: "Alguns perfis precisam de atenção",
  verification_complete: "Todos os testes concluídos",
  failed: "Verificação interrompida",
};

export function verificationStateLabel(verification: Verification) {
  if (
    verification.state === "failed" &&
    verification.stack_results?.some((result) => result.passed)
  ) {
    return "Verificação concluída com aprovação parcial";
  }
  return (
    verificationStates[verification.state] ||
    "Estado da verificação em atualização"
  );
}

export function verificationStageLabel(stage: string) {
  return verificationStages[stage] || "Conferindo o resultado da operação";
}

export function PublicationVerificationStatus({
  verification,
}: {
  verification: Verification;
}) {
  const failure = verification.failure;
  const failedStage = failure?.stage || verification.failed_stage;
  return (
    <section
      className="cloudflare-verification-result"
      aria-label="Resultado da verificação"
    >
      <p role="status">
        <strong>{verificationStateLabel(verification)}</strong>
        <br />
        <span>{verificationStageLabel(verification.stage)}</span>
      </p>
      {(failure || failedStage) && (
        <div className="notice error" role="alert">
          <div>
            <strong>
              A verificação encontrou uma falha
              {failure?.stack || verification.stack
                ? ` no perfil ${failure?.stack || verification.stack}`
                : ""}
              .
            </strong>
            {failedStage && (
              <p>
                Etapa: {verificationStageLabel(failedStage)}{" "}
                <small>({failedStage})</small>
              </p>
            )}
            {failure?.message && <p>{failure.message}</p>}
          </div>
        </div>
      )}
      {!!verification.stack_results?.length && (
        <details className="cloudflare-check-results">
          <summary>Resultado por perfil da última verificação</summary>
          <ul aria-label="Resultado por perfil">
            {verification.stack_results.map((result) => (
              <li key={result.stack}>
                <code>{result.stack}</code>:{" "}
                <strong>{result.passed ? "Aprovado" : "Não aprovado"}</strong>
                {!result.passed && result.failure?.stage && (
                  <> · {verificationStageLabel(result.failure.stage)}</>
                )}
                {!result.passed && result.failure?.message && (
                  <p>{result.failure.message}</p>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      {verification.cleanup?.state === "retained" && (
        <p className="notice" role="status">
          A retenção está habilitada. Os recursos criados por esta verificação
          não são excluídos automaticamente, inclusive quando um perfil não foi
          aprovado.
        </p>
      )}
      {verification.temporary_resources &&
        verification.cleanup?.state === "succeeded" &&
        (verification.cleanup.worker_removed ||
          verification.cleanup.database_removed) && (
          <p className="notice">
            Esta verificação antiga removeu os recursos temporários ao terminar.
            As novas verificações preservam os recursos na Cloudflare.
          </p>
        )}
      <details className="cloudflare-check-resources">
        <summary>Recursos e detalhes da verificação</summary>
        <p className="small">
          Operação: <code>{verification.id}</code>
          <br />
          Estado: <code>{verification.state}</code> · Etapa:{" "}
          <code>{verification.stage}</code>
          {verification.error_code && (
            <>
              <br />
              Código: <code>{verification.error_code}</code>
            </>
          )}
        </p>
        {!!verification.resources?.length && (
          <div>
            <strong>Recursos da verificação na Cloudflare</strong>
            <ul aria-label="Recursos da verificação">
              {verification.resources.map((resource) => {
                const workerConfirmed =
                  resource.worker_confirmed ?? !!resource.deployment_id;
                const url = workerConfirmed ? resourceUrl(resource.url) : null;
                return (
                  <li key={`${resource.stack}:${resource.worker_name}`}>
                    <strong>{resource.stack}</strong>
                    {resource.state === "retained" && " · Retenção habilitada"}
                    <p>
                      {workerConfirmed ? "Worker" : "Nome previsto do Worker"}:{" "}
                      <code>{resource.worker_name}</code>
                      {!workerConfirmed && (
                        <>
                          <br />
                          <small>
                            Criação/publicação ainda não confirmada.
                          </small>
                        </>
                      )}
                      <br />
                      D1:{" "}
                      <code>
                        {resource.database_id ||
                          "Provisionamento ainda não confirmado"}
                      </code>
                    </p>
                    {url && (
                      <a href={url} target="_blank" rel="noopener noreferrer">
                        Abrir Worker de {resource.stack}
                      </a>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </details>
    </section>
  );
}
