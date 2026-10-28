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
  stack_results?: { stack: string; passed: boolean; failure?: VerificationFailure | null }[];
  temporary_resources?: boolean;
  cleanup?: { state: string; worker_removed?: boolean; database_removed?: boolean } | null;
  resources?: { stack: string; worker_name: string; database_id?: string | null; url?: string | null; state: string; worker_confirmed?: boolean; deployment_id?: string | null }[];
};

function resourceUrl(value?: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

export function PublicationVerificationStatus({ verification }: { verification: Verification }) {
  const failure = verification.failure;
  const failedStage = failure?.stage || verification.failed_stage;
  return <section aria-label="Resultado da verificação">
    <p>Verificação: <strong>{verification.state}</strong> · {verification.stage} {verification.error_code && `· ${verification.error_code}`}<br /><small>Operação: {verification.id}</small></p>
    {(failure || failedStage) && <div className="notice error" role="alert">
      <div>
        <strong>A verificação encontrou uma falha{failure?.stack || verification.stack ? ` no perfil ${failure?.stack || verification.stack}` : ""}.</strong>
        {failedStage && <p>Etapa: <code>{failedStage}</code></p>}
        {failure?.message && <p>{failure.message}</p>}
      </div>
    </div>}
    {!!verification.stack_results?.length && <div>
      <strong>Resultado por perfil</strong>
      <ul aria-label="Resultado por perfil">{verification.stack_results.map((result) => <li key={result.stack}>
        <code>{result.stack}</code>: <strong>{result.passed ? "Aprovado" : "Não aprovado"}</strong>
        {!result.passed && result.failure?.stage && <> · <code>{result.failure.stage}</code></>}
        {!result.passed && result.failure?.message && <p>{result.failure.message}</p>}
      </li>)}</ul>
    </div>}
    {verification.cleanup?.state === "retained" && <p className="notice" role="status">A retenção está habilitada. Os recursos criados por esta verificação não são excluídos automaticamente, inclusive quando um perfil não foi aprovado.</p>}
    {verification.temporary_resources && verification.cleanup?.state === "succeeded" && (verification.cleanup.worker_removed || verification.cleanup.database_removed) && <p className="notice">Esta verificação antiga removeu os recursos temporários ao terminar. As novas verificações preservam os recursos na Cloudflare.</p>}
    {!!verification.resources?.length && <div>
      <strong>Recursos da verificação na Cloudflare</strong>
      <ul aria-label="Recursos da verificação">{verification.resources.map((resource) => {
        const workerConfirmed = resource.worker_confirmed ?? !!resource.deployment_id;
        const url = workerConfirmed ? resourceUrl(resource.url) : null;
        return <li key={`${resource.stack}:${resource.worker_name}`}>
          <strong>{resource.stack}</strong>{resource.state === "retained" && " · Retenção habilitada"}
          <p>{workerConfirmed ? "Worker" : "Nome previsto do Worker"}: <code>{resource.worker_name}</code>
            {!workerConfirmed && <><br /><small>Criação/publicação ainda não confirmada.</small></>}
            <br />D1: <code>{resource.database_id || "Provisionamento ainda não confirmado"}</code></p>
          {url && <a href={url} target="_blank" rel="noopener noreferrer">Abrir Worker de {resource.stack}</a>}
        </li>;
      })}</ul>
    </div>}
  </section>;
}
