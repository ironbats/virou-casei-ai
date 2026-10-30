import { useEffect, useId, useRef, type ReactNode } from "react";
import {
  Activity,
  ArrowRight,
  CalendarDays,
  Check,
  CheckCircle2,
  Circle,
  Clock3,
  Database,
  Info,
  LockKeyhole,
  Minus,
  ShieldCheck,
  UserRound,
  X,
  XCircle,
} from "lucide-react";
import { verificationStageLabel } from "./publication-verification-status";
import {
  attemptCount,
  duration,
  failureSummary,
  gateSummary,
  publicationView,
  runLabel,
  shortId,
  stateLabel,
  stateTone,
  timestamp,
  type Run,
  type Snapshot,
  type Tone,
} from "./cloudflare-snapshot";

export function StateIcon({
  tone,
  active = false,
}: {
  tone: Tone;
  active?: boolean;
}) {
  const Icon =
    tone === "success"
      ? Check
      : tone === "danger"
        ? X
        : tone === "warning"
          ? Clock3
          : tone === "running"
            ? Activity
            : Minus;
  return (
    <span
      className={`cf-state-icon cf-${tone}${active ? " cf-activity" : ""}`}
      aria-hidden="true"
    >
      <Icon size={23} />
    </span>
  );
}

export function StatusBadge({
  state,
  label,
  tone,
}: {
  state?: string;
  label?: string;
  tone?: Tone;
}) {
  const color = tone || stateTone(state);
  const Icon =
    color === "success"
      ? CheckCircle2
      : color === "danger"
        ? XCircle
        : color === "warning"
          ? LockKeyhole
          : color === "running"
            ? Activity
            : Circle;
  return (
    <span className={`cf-badge cf-${color}`}>
      <Icon size={16} aria-hidden="true" />
      {label || stateLabel(state)}
    </span>
  );
}

export function LoadingState() {
  return (
    <div
      className="cf-skeleton"
      aria-label="Carregando estado da conexão"
      role="status"
    >
      <span />
      <span />
      <span />
      <p>Consultando o estado da conexão…</p>
    </div>
  );
}

export function StaleNotice({
  snapshot,
  receivedAt,
}: {
  snapshot: Snapshot | null;
  receivedAt: string | null;
}) {
  return (
    <div className="cf-callout cf-warning">
      <Info size={20} aria-hidden="true" />
      <div>
        <strong>Atualização indisponível</strong>
        <p>
          {snapshot
            ? `Dados de ${timestamp(snapshot.observed_at || receivedAt)}; atualização indisponível. O último estado foi preservado para consulta e não confirma a prontidão atual.`
            : "Não foi possível confirmar o estado desta conexão."}
        </p>
      </div>
    </div>
  );
}

export function RefreshButton({
  loading,
  disabled = false,
  refresh,
}: {
  loading: boolean;
  disabled?: boolean;
  refresh: () => void;
}) {
  return (
    <button
      type="button"
      className="button cf-refresh"
      onClick={refresh}
      disabled={loading || disabled}
      aria-busy={loading}
    >
      {loading ? "Atualizando…" : "Atualizar estado"}
    </button>
  );
}

function NextActionPanel({
  snapshot,
  stale,
  navigate,
  chooseAction,
}: {
  snapshot: Snapshot;
  stale: boolean;
  navigate: () => void;
  chooseAction: (key: string) => void;
}) {
  const run = snapshot.current_run;
  const retry = snapshot.retry;
  const gate = snapshot.publication;
  let title = "Confira a liberação da publicação";
  let description =
    gate.reason_text ||
    "Consulte o diagnóstico e as ações disponíveis para esta conexão.";
  let label = "Revisar verificação";
  let action = navigate;
  let tone: Tone = "warning";
  let active = false;
  let schedule = false;
  if (stale) {
    title = "Acompanhamento temporariamente interrompido";
    description =
      "O último diagnóstico permanece disponível. Atualize o estado para confirmar a situação atual.";
    tone = "neutral";
  } else if (run) {
    title = runLabel(run);
    description = run.current_step_ids.length
      ? run.steps
          .filter((step) => run.current_step_ids.includes(step.id))
          .map((step) => step.label)
          .join(" · ")
      : run.state === "queued"
        ? "Aguardando o executor; nenhuma etapa em execução foi confirmada."
        : "Acompanhe os eventos observados pelo executor.";
    label = ["queued", "running"].includes(run.state)
      ? "Acompanhar verificação"
      : "Revisar verificação";
    tone = stateTone(
      run.raw_state === "reconciling" ? "reconciling" : run.state,
    );
    active = run.state === "running";
  } else if (retry.scheduled && retry.next_attempt_at) {
    title =
      retry.state === "retry_wait"
        ? "Nova tentativa automática programada"
        : "Verificação automática programada";
    description =
      retry.state === "retry_wait"
        ? "A próxima tentativa aguarda o intervalo de recuperação."
        : "O horário da verificação é informado pelo servidor.";
    label = "Acompanhar verificação";
    schedule = true;
  } else if (
    !gate.allowed &&
    snapshot.previous_result?.configuration_current === false
  ) {
    title = "Verificação anterior não se aplica à configuração atual";
    description =
      "O resultado histórico permanece disponível. Consulte a verificação da versão atual.";
  } else if (!snapshot.connection.active) {
    if (snapshot.connection.state === "validated") {
      title = "Ative a conexão validada";
      description =
        "A ativação cadastral preserva os checks de publicação de cada perfil.";
      label = "Ativar conexão";
      action = () => chooseAction("activate");
    } else {
      title =
        snapshot.connection.state === "disabled"
          ? "Conexão inativa"
          : "Valide as credenciais desta versão";
      description =
        "Confira o acesso à conta e as permissões no fluxo de conexão existente.";
      label = "Validar conexão";
      action = () => chooseAction("validate");
      tone = "neutral";
    }
  } else if (gate.allowed) {
    title =
      gate.state === "partial"
        ? "Publicação liberada para os perfis aprovados"
        : "Conexão disponível para publicação";
    description =
      "Cada projeto continua sujeito aos próprios checks, permissões e regras de publicação.";
    tone = gate.state === "partial" ? "warning" : "success";
    label = "Ver resultados da verificação";
    if (!snapshot.connection.is_default) {
      description =
        "Para novos projetos, use a ação existente de definição da conexão padrão.";
      label = "Definir como padrão";
      action = () => chooseAction("default");
    }
  } else if (!snapshot.previous_result) {
    title = "Ainda não verificado";
    description =
      "Não há resultado concluído informado para esta conexão. Consulte os checks e as ações permitidas.";
    tone = "neutral";
  }
  const overdue =
    schedule &&
    retry.next_attempt_at &&
    snapshot.server_time &&
    Date.parse(retry.next_attempt_at) < Date.parse(snapshot.server_time);
  const count = attemptCount(snapshot);
  return (
    <section className={`cf-next-panel cf-${tone}`} aria-label="Próximo passo">
      <StateIcon tone={tone} active={active} />
      <div className="cf-next-copy">
        <h3>{title}</h3>
        <p>{description}</p>
        {run && run.active_profile_ids.length > 0 && (
          <p>
            Perfis atuais: <strong>{run.active_profile_ids.join(", ")}</strong>
          </p>
        )}
        {schedule && (
          <div className="cf-meta-line">
            <span>
              <CalendarDays size={17} aria-hidden="true" />
              {timestamp(retry.next_attempt_at)}
            </span>
            {count && <span>{count}</span>}
          </div>
        )}
        {overdue && (
          <p className="cf-overdue">
            Horário previsto ultrapassado; aguardando atualização.
          </p>
        )}
      </div>
      <button type="button" className="button primary" onClick={action}>
        {label}
        <ArrowRight size={17} aria-hidden="true" />
      </button>
    </section>
  );
}

function DiagnosticSummary({
  snapshot,
  stale,
  navigate,
}: {
  snapshot: Snapshot;
  stale: boolean;
  navigate: () => void;
}) {
  const gate = snapshot.publication;
  const historical = snapshot.previous_result;
  const failed =
    historical && (historical.error_code || historical.state === "failed")
      ? historical
      : null;
  const summary = stale
    ? "O último estado foi preservado; a prontidão atual não pôde ser confirmada."
    : gate.allowed
      ? gate.state === "partial"
        ? "Consulte os motivos dos perfis bloqueados na aba Verificação."
        : "Os checks exigidos para esta conexão estão atendidos."
      : failed
        ? failureSummary(failed.error_code, failed.error_summary)
        : gate.reason_text ||
          "Não há evidência suficiente para confirmar a publicação.";
  return (
    <article className="cf-card cf-blocking-card">
      <h3>
        {gate.allowed ? "Situação da publicação" : "O que impede a publicação?"}
      </h3>
      <div
        className={`cf-inline-notice cf-${stale ? "neutral" : gate.allowed ? (gate.state === "partial" ? "warning" : "success") : failed ? "danger" : "warning"}`}
      >
        {stale ? (
          <Info size={23} aria-hidden="true" />
        ) : gate.allowed && gate.state !== "partial" ? (
          <CheckCircle2 size={23} aria-hidden="true" />
        ) : failed ? (
          <XCircle size={23} aria-hidden="true" />
        ) : (
          <LockKeyhole size={23} aria-hidden="true" />
        )}
        <strong>{summary}</strong>
      </div>
      {failed && !gate.allowed && (
        <>
          <p className="cf-context-note">
            {failed.interrupted
              ? "Tentativa anterior interrompida"
              : "Último resultado concluído"}{" "}
            · {shortId(failed.id)}
            {failed.configuration_current === false
              ? " · Configuração anterior"
              : ""}
          </p>
          <dl className="cf-inline-definition">
            <div>
              <dt>Perfil</dt>
              <dd>{failed.failed_profile || "Não informado"}</dd>
            </div>
            <div>
              <dt>Etapa</dt>
              <dd>
                {failed.failed_stage
                  ? verificationStageLabel(failed.failed_stage)
                  : "Não informada"}
              </dd>
            </div>
          </dl>
          {snapshot.current_run && (
            <p className="cf-context-note">
              Esta falha pertence ao histórico; a tentativa atual está
              identificada separadamente.
            </p>
          )}
        </>
      )}
      {failed && !gate.allowed && !stale && (
        <p>Bloqueio atual: {gateSummary(gate)}.</p>
      )}
      <button type="button" className="cf-text-button" onClick={navigate}>
        Ver diagnóstico na aba Verificação
        <ArrowRight size={16} aria-hidden="true" />
      </button>
      <details className="cf-details">
        <summary>Detalhes técnicos</summary>
        <dl className="cf-technical-definition">
          <div>
            <dt>Bloqueio da publicação</dt>
            <dd>
              <code>{gate.reason_code || "Não informado"}</code>
            </dd>
          </div>
          <div>
            <dt>Motivo original do bloqueio</dt>
            <dd>{gate.reason_text || "Não informado"}</dd>
          </div>
          {failed && (
            <>
              <div>
                <dt>Código do resultado anterior</dt>
                <dd>
                  <code>{failed.error_code || "Não informado"}</code>
                </dd>
              </div>
              <div>
                <dt>Correlação</dt>
                <dd>
                  <code>{failed.correlation_id || "Não informada"}</code>
                </dd>
              </div>
              <div>
                <dt>Mensagem sanitizada</dt>
                <dd>{failed.error_summary || "Não informada"}</dd>
              </div>
              <div>
                <dt>Execução / versão</dt>
                <dd>
                  <code>{failed.id}</code> ·{" "}
                  {failed.configuration_version ?? "Não informada"}
                </dd>
              </div>
            </>
          )}
          {gate.reasons.length > 1 && (
            <div>
              <dt>Demais impedimentos</dt>
              <dd>
                <ul>
                  {gate.reasons.slice(1).map((reason, index) => (
                    <li key={`${reason.code}-${index}`}>
                      {reason.message} <code>{reason.code}</code>
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          )}
        </dl>
      </details>
    </article>
  );
}

export function ReadinessOverview({
  snapshot,
  stale,
  loading,
  disabled,
  refresh,
  navigate,
  diagnose,
  chooseAction,
}: {
  snapshot: Snapshot | null;
  stale: boolean;
  loading: boolean;
  disabled: boolean;
  refresh: () => void;
  navigate: () => void;
  diagnose: () => void;
  chooseAction: (action: string) => void;
}) {
  const publication = publicationView(snapshot, stale);
  const run = snapshot?.current_run || snapshot?.previous_result || null;
  return (
    <>
      <div className="cloudflare-section-heading">
        <div>
          <h3>Prontidão da publicação</h3>
          <p className="field-help">Veja o estado atual e o próximo passo.</p>
        </div>
        <RefreshButton
          loading={loading}
          disabled={disabled}
          refresh={refresh}
        />
      </div>
      {!snapshot ? (
        loading ? (
          <LoadingState />
        ) : (
          <div className="cf-callout cf-neutral">
            <Info size={20} aria-hidden="true" />
            <span>
              Não foi possível confirmar. Atualize o estado para consultar esta
              conexão.
            </span>
          </div>
        )
      ) : (
        <>
          <div className="cloudflare-readiness-grid">
            <article className="cloudflare-readiness-card">
              <h4>Conexão</h4>
              <div>
                <StateIcon
                  tone={snapshot.connection.active ? "success" : "neutral"}
                />
                <div>
                  <strong>
                    {snapshot.connection.active
                      ? "Cadastro ativo"
                      : "Conexão inativa"}
                  </strong>
                  <p>Não confirma publicação</p>
                </div>
              </div>
            </article>
            <article className="cloudflare-readiness-card">
              <h4>Publicação</h4>
              <div>
                <StateIcon tone={publication.tone} />
                <div>
                  <strong>{publication.title}</strong>
                  <p>{publication.detail}</p>
                </div>
              </div>
            </article>
            <article className="cloudflare-readiness-card">
              <h4>
                {snapshot.current_run
                  ? "Verificação atual"
                  : "Última verificação"}
              </h4>
              <div>
                <StateIcon
                  tone={
                    stale || run?.configuration_current === false
                      ? "neutral"
                      : stateTone(run?.state)
                  }
                  active={!stale && run?.state === "running"}
                />
                <div>
                  <strong>{runLabel(run)}</strong>
                  <p>
                    {run?.configuration_current === false
                      ? `Histórico da versão ${run.configuration_version ?? "não informada"}`
                      : run?.failed_stage
                        ? verificationStageLabel(run.failed_stage)
                        : run?.finished_at
                          ? timestamp(run.finished_at)
                          : run
                            ? "Consulte as etapas e o histórico"
                            : "Sem resultado concluído"}
                  </p>
                </div>
              </div>
            </article>
          </div>
          <NextActionPanel
            snapshot={snapshot}
            stale={stale}
            navigate={navigate}
            chooseAction={chooseAction}
          />
          <div className="cf-overview-columns">
            <DiagnosticSummary
              snapshot={snapshot}
              stale={stale}
              navigate={diagnose}
            />
            <article className="cf-card cf-connection-info">
              <h3>Informações da conexão</h3>
              <dl>
                <div>
                  <dt>Última validação de credencial e conta</dt>
                  <dd>
                    <CalendarDays size={18} aria-hidden="true" />
                    {timestamp(snapshot.credential.checked_at)}
                  </dd>
                </div>
                <div>
                  <dt>Última verificação operacional concluída</dt>
                  <dd>
                    <Clock3 size={18} aria-hidden="true" />
                    {timestamp(snapshot.previous_result?.finished_at)}
                  </dd>
                </div>
                <div>
                  <dt>Conta Cloudflare</dt>
                  <dd>
                    <UserRound size={18} aria-hidden="true" />
                    <code>{snapshot.connection.account_masked}</code>
                  </dd>
                </div>
                <div>
                  <dt>Ambiente</dt>
                  <dd>
                    <Database size={18} aria-hidden="true" />
                    {snapshot.connection.environment || "Não informado"}
                  </dd>
                </div>
                <div>
                  <dt>Versão da configuração</dt>
                  <dd>{snapshot.connection.configuration_version}</dd>
                </div>
              </dl>
            </article>
          </div>
          <div className="cf-footnote">
            <Info size={21} aria-hidden="true" />
            <p>
              Projetos existentes continuam vinculados à conta original. A
              conexão padrão recebe novos projetos.
            </p>
          </div>
        </>
      )}
    </>
  );
}

function Timeline({
  run,
  historical = false,
}: {
  run: Run | null;
  historical?: boolean;
}) {
  const phases = new Map<string, { label: string; steps: Run["steps"] }>();
  const phaseLabels: Record<string, string> = {
    prepare: "Preparar ambiente",
    publish: "Publicar aplicação de teste",
    validate: "Validar HTTP e JSON",
    consolidate: "Consolidar resultado",
  };
  for (const step of [...(run?.steps || [])].sort(
    (a, b) => a.order - b.order,
  )) {
    const key = step.phase_id || step.id;
    if (!phases.has(key))
      phases.set(key, { label: phaseLabels[key] || step.label, steps: [] });
    phases.get(key)!.steps.push(step);
  }
  const phaseState = (steps: Run["steps"]) =>
    steps.some((step) => step.state === "running")
      ? "running"
      : steps.some((step) => step.state === "failed")
        ? "failed"
        : steps.every((step) => step.state === "succeeded")
          ? "succeeded"
          : steps.every((step) => step.state === "skipped")
            ? "skipped"
            : steps.some((step) => step.state === "queued")
              ? "queued"
              : steps.every((step) => step.state === "not_started")
                ? "not_started"
                : "unknown";
  return (
    <article className="cf-card">
      <h3>
        {historical ? "Etapas do último resultado" : "Etapas da verificação"}
      </h3>
      {historical && run && <RunIdentity run={run} />}
      {!run ? (
        <p className="cf-empty">
          Nenhuma execução informada. Uma etapa sem evidência não recebe
          aprovação.
        </p>
      ) : !run.steps.length ? (
        <p className="cf-empty">
          As etapas desta execução não foram informadas pelo backend. Nenhuma
          conclusão foi presumida.
        </p>
      ) : (
        <>
          <ol className="cf-timeline" aria-label="Etapas observadas">
            {[...phases].map(([key, phase]) => {
              const state = phaseState(phase.steps);
              const runningProfiles = [
                ...new Set(
                  phase.steps
                    .filter(
                      (step) => step.state === "running" && step.profile_id,
                    )
                    .map((step) => step.profile_id),
                ),
              ];
              const failedCount = phase.steps.filter(
                (step) => step.state === "failed",
              ).length;
              return (
                <li key={key} className={`cf-step cf-${stateTone(state)}`}>
                  <span className="cf-step-icon" aria-hidden="true">
                    {state === "succeeded" ? (
                      <Check size={18} />
                    ) : state === "failed" ? (
                      <X size={18} />
                    ) : state === "running" ? (
                      <Activity size={18} />
                    ) : state === "skipped" ? (
                      <Minus size={18} />
                    ) : (
                      <Circle size={18} />
                    )}
                  </span>
                  <div>
                    <div className="cf-step-heading">
                      <strong>{phase.label}</strong>
                      <StatusBadge state={state} />
                    </div>
                    {runningProfiles.length > 0 && (
                      <p>
                        Perfis em execução:{" "}
                        <strong>{runningProfiles.join(", ")}</strong>
                      </p>
                    )}
                    {failedCount > 0 && state === "running" && (
                      <p>
                        {failedCount} etapa(s) com falha registrada nesta fase.
                      </p>
                    )}
                    <details className="cf-phase-details">
                      <summary>
                        {phase.steps.length === 1
                          ? "Evidência da etapa"
                          : `${phase.steps.length} etapas por perfil`}
                      </summary>
                      <ul>
                        {phase.steps.map((step) => (
                          <li key={step.id}>
                            <div className="cf-step-heading">
                              <strong>{step.label}</strong>
                              <StatusBadge state={step.state} />
                            </div>
                            <p>Perfil: {step.profile_id || "Conexão"}</p>
                            {step.message && <p>{step.message}</p>}
                            <p className="cf-step-time">
                              Início: {timestamp(step.started_at)} · Fim:{" "}
                              {timestamp(step.finished_at)}
                            </p>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </div>
                </li>
              );
            })}
          </ol>
          <p className="cf-context-note">
            Fases agrupadas por tipo de check. As etapas por perfil e os eventos
            preservam a sequência observada.
          </p>
        </>
      )}
      <div className="cf-inline-help">
        <Info size={19} aria-hidden="true" />
        <p>
          A publicação depende da conclusão dos checks operacionais aplicáveis.
        </p>
      </div>
    </article>
  );
}

function RunIdentity({ run }: { run: Run }) {
  return (
    <div className="cf-run-identity">
      <span>
        Execução <code>{shortId(run.id)}</code>
      </span>
      <span>Versão {run.configuration_version ?? "não informada"}</span>
      {run.configuration_current === false && (
        <StatusBadge label="Configuração anterior" tone="warning" />
      )}
      {run.configuration_current == null && (
        <span>Vínculo de versão não informado</span>
      )}
    </div>
  );
}

function PreviousResult({
  snapshot,
  openDiagnostic,
}: {
  snapshot: Snapshot;
  openDiagnostic: () => void;
}) {
  const run = snapshot.previous_result;
  const count = attemptCount(snapshot);
  return (
    <article className="cf-card cf-previous-result">
      <h3>Resultado anterior</h3>
      {!run ? (
        <p className="cf-empty">
          Nenhum resultado concluído informado. Uma execução iniciada não
          representa aprovação.
        </p>
      ) : (
        <>
          <div className={`cf-inline-notice cf-${stateTone(run.state)}`}>
            <StateIcon tone={stateTone(run.state)} />
            <strong>
              {run.state === "failed"
                ? failureSummary(run.error_code, run.error_summary)
                : runLabel(run)}
            </strong>
          </div>
          <p className="cf-context-note">
            {run.interrupted
              ? "Tentativa anterior interrompida · histórico"
              : "Última tentativa concluída · histórico"}
          </p>
          <RunIdentity run={run} />
          <dl className="cf-inline-definition">
            <div>
              <dt>{run.interrupted ? "Último evento" : "Conclusão"}</dt>
              <dd>
                {timestamp(
                  run.interrupted ? run.last_event_at : run.finished_at,
                )}
              </dd>
            </div>
            <div>
              <dt>Perfil</dt>
              <dd>
                {run.failed_profile ||
                  run.profile_ids.join(", ") ||
                  "Não informado"}
              </dd>
            </div>
            <div>
              <dt>Etapa</dt>
              <dd>
                {run.failed_stage
                  ? verificationStageLabel(run.failed_stage)
                  : runLabel(run)}
              </dd>
            </div>
            {run.error_code && (
              <div>
                <dt>Erro</dt>
                <dd>
                  <code>{run.error_code}</code>
                </dd>
              </div>
            )}
          </dl>
          <button type="button" className="button" onClick={openDiagnostic}>
            Ver diagnóstico
          </button>
        </>
      )}
      {count && (
        <div className="cf-history-count">
          <Clock3 size={19} aria-hidden="true" />
          Histórico: {count}
        </div>
      )}
    </article>
  );
}

function ProfileReadiness({
  snapshot,
  stale,
}: {
  snapshot: Snapshot;
  stale: boolean;
}) {
  return (
    <div className="cf-profile-readiness">
      <h3>Prontidão por perfil</h3>
      {!snapshot.profiles.length ? (
        <p className="cf-empty">Nenhum perfil foi informado pelo backend.</p>
      ) : (
        <table className="cf-profile-table">
          <caption className="cf-sr-only">
            Prontidão de publicação e verificação por perfil
          </caption>
          <thead>
            <tr>
              <th scope="col">Perfil</th>
              <th scope="col">Publicação</th>
              <th scope="col">Verificação atual</th>
            </tr>
          </thead>
          <tbody>
            {snapshot.profiles.map((profile) => (
              <tr key={profile.id}>
                <th scope="row">{profile.label || profile.id}</th>
                <td data-label="Publicação">
                  <StatusBadge
                    label={
                      stale
                        ? "Não confirmada"
                        : profile.publication.allowed
                          ? "Liberada"
                          : ["unknown", "not_verified", "unverified"].includes(
                                profile.publication.state,
                              )
                            ? "Não verificada"
                            : "Bloqueada"
                    }
                    tone={
                      stale
                        ? "neutral"
                        : profile.publication.allowed
                          ? "success"
                          : "warning"
                    }
                  />
                </td>
                <td data-label="Verificação atual">
                  <StatusBadge state={profile.verification_state} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function ProfileResults({
  snapshot,
  stale,
}: {
  snapshot: Snapshot;
  stale: boolean;
}) {
  const run = snapshot.previous_result;
  return (
    <details className="cf-details">
      <summary>Resultado por perfil da última verificação</summary>
      {run ? (
        <>
          <RunIdentity run={run} />
          <ul className="cf-profile-results" aria-label="Resultado por perfil">
            {run.stack_results.map((result) => {
              const evidence = snapshot.profiles.find(
                (profile) => profile.id === result.stack,
              );
              const current =
                !stale &&
                run.configuration_current === true &&
                result.configuration_current !== false &&
                evidence?.evidence_run_id === run.id &&
                evidence.evidence_state === "valid";
              const validity =
                (
                  {
                    valid: "Válida segundo os checks atuais",
                    expired: "Vencida",
                    invalid: "Invalidada",
                    not_started: "Ainda não verificada",
                    unknown: "Não informada",
                  } as Record<string, string>
                )[evidence?.evidence_state || "unknown"] ||
                "Estado não reconhecido";
              return (
                <li key={result.stack}>
                  <div className="cf-step-heading">
                    <strong>{result.stack}</strong>
                    <StatusBadge
                      label={
                        result.passed
                          ? current
                            ? "Checks aprovados"
                            : "Checks aprovados no histórico"
                          : result.passed === false
                            ? "Não aprovado"
                            : "Não informado"
                      }
                      tone={
                        result.passed && current
                          ? "success"
                          : result.passed === false
                            ? "danger"
                            : "neutral"
                      }
                    />
                  </div>
                  <dl className="cf-inline-definition">
                    <div>
                      <dt>Etapa</dt>
                      <dd>
                        {result.failure?.stage || result.stage
                          ? verificationStageLabel(
                              result.failure?.stage || result.stage || "",
                            )
                          : "Não informada"}
                      </dd>
                    </div>
                    <div>
                      <dt>Observação</dt>
                      <dd>
                        {result.failure?.message ||
                          (result.passed
                            ? "O executor registrou os checks deste perfil como aprovados."
                            : "Não informada")}
                      </dd>
                    </div>
                    {result.failure?.code && (
                      <div>
                        <dt>Código</dt>
                        <dd>
                          <code>{result.failure.code}</code>
                        </dd>
                      </div>
                    )}
                    <div>
                      <dt>Resultado observado em</dt>
                      <dd>
                        {timestamp(result.observed_at || run.finished_at)}
                      </dd>
                    </div>
                    <div>
                      <dt>Validade da evidência atual</dt>
                      <dd>
                        {stale
                          ? "Não confirmada; dados desatualizados"
                          : validity}
                      </dd>
                    </div>
                    <div>
                      <dt>Evidência válida até</dt>
                      <dd>
                        {evidence?.evidence_run_id === run.id
                          ? timestamp(evidence.evidence_valid_until)
                          : "Não informada para esta execução"}
                      </dd>
                    </div>
                    <div>
                      <dt>Vínculo com configuração atual</dt>
                      <dd>
                        {run.configuration_current === true
                          ? "Corresponde à configuração"
                          : run.configuration_current === false
                            ? "Histórico; não se aplica à configuração atual"
                            : "Não informado"}
                      </dd>
                    </div>
                  </dl>
                </li>
              );
            })}
          </ul>
          {!run.stack_results.length && (
            <p>
              O backend não informou resultados por perfil para esta execução.
            </p>
          )}
        </>
      ) : (
        <p>Nenhuma verificação concluída disponível.</p>
      )}
      {snapshot.profiles.some(
        (profile) => profile.publication.reasons.length > 0,
      ) && (
        <div className="cf-profile-blocks">
          <h4>Impedimentos atuais por perfil</h4>
          {snapshot.profiles
            .filter((profile) => profile.publication.reasons.length)
            .map((profile) => (
              <div key={profile.id}>
                <strong>{profile.label}</strong>
                <ul>
                  {profile.publication.reasons.map((reason, index) => (
                    <li key={`${reason.code}-${index}`}>
                      {reason.message} <code>{reason.code}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
        </div>
      )}
    </details>
  );
}

function safeResourceUrl(value?: string | null) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      return null;
    return url.href;
  } catch {
    return null;
  }
}

function RunTechnical({ run, heading }: { run: Run; heading: string }) {
  return (
    <section className="cf-run-technical" aria-label={heading}>
      <h4>{heading}</h4>
      <RunIdentity run={run} />
      <dl className="cf-technical-definition">
        <div>
          <dt>Identificador completo</dt>
          <dd>
            <code>{run.id}</code>
          </dd>
        </div>
        <div>
          <dt>Correlação</dt>
          <dd>
            <code>{run.correlation_id || "Não informada"}</code>
          </dd>
        </div>
        <div>
          <dt>Início</dt>
          <dd>{timestamp(run.started_at)}</dd>
        </div>
        <div>
          <dt>Último evento</dt>
          <dd>{timestamp(run.last_event_at)}</dd>
        </div>
        <div>
          <dt>Término</dt>
          <dd>{timestamp(run.finished_at)}</dd>
        </div>
        <div>
          <dt>
            {run.finished_at
              ? "Duração observada"
              : "Tempo decorrido informado pelo servidor"}
          </dt>
          <dd>{duration(run.duration_ms)}</dd>
        </div>
        <div>
          <dt>Fingerprint da configuração</dt>
          <dd>
            <code>{shortId(run.configuration_fingerprint)}</code>
          </dd>
        </div>
        <div>
          <dt>Fingerprint do executor</dt>
          <dd>
            <code>{shortId(run.executor_fingerprint)}</code>
          </dd>
        </div>
        <div>
          <dt>Estado original</dt>
          <dd>
            <code>{run.raw_state}</code>
          </dd>
        </div>
        {run.error_code && (
          <div>
            <dt>Código original</dt>
            <dd>
              <code>{run.error_code}</code>
            </dd>
          </div>
        )}
        {run.error_summary && (
          <div>
            <dt>Mensagem sanitizada</dt>
            <dd>{run.error_summary}</dd>
          </div>
        )}
      </dl>
      {(run.failure ||
        run.remote_verification ||
        run.stack_results.some(
          (result) => result.remote || result.bindings,
        )) && (
        <details className="cf-details">
          <summary>Evidências técnicas completas desta execução</summary>
          <pre aria-label={`Evidências sanitizadas da execução ${run.id}`}>
            {JSON.stringify(
              {
                failure: run.failure || null,
                remote_verification: run.remote_verification || null,
                stack_results: run.stack_results,
              },
              null,
              2,
            )}
          </pre>
        </details>
      )}
      <h4>Probes HTTP e JSON</h4>
      {run.steps.some((step) => step.probes.length) ? (
        <ul className="cf-probes">
          {run.steps.flatMap((step) =>
            step.probes.map((probe) => (
              <li key={`${step.id}-${probe.key}`}>
                <div className="cf-step-heading">
                  <strong>{probe.key}</strong>
                  <StatusBadge state={probe.state} />
                </div>
                <p>
                  {step.label}
                  {step.profile_id ? ` · ${step.profile_id}` : ""}
                </p>
                <dl className="cf-inline-definition">
                  <div>
                    <dt>Status HTTP observado</dt>
                    <dd>{probe.observed_http_status ?? "Não informado"}</dd>
                  </div>
                  <div>
                    <dt>Esperado</dt>
                    <dd>{probe.expected_summary || "Não informado"}</dd>
                  </div>
                  <div>
                    <dt>Observado</dt>
                    <dd>{probe.observed_summary || "Não informado"}</dd>
                  </div>
                  <div>
                    <dt>Erro / transporte</dt>
                    <dd>
                      <code>{probe.error_code || "Não informado"}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Observação em</dt>
                    <dd>{timestamp(probe.observed_at)}</dd>
                  </div>
                </dl>
              </li>
            )),
          )}
        </ul>
      ) : (
        <p>
          Nenhum probe detalhado foi informado para esta execução. HTTP 200
          isolado não comprova os demais checks.
        </p>
      )}
      <h4>Eventos observados</h4>
      {run.events.length ? (
        <ol className="cf-events">
          {[...run.events]
            .sort((a, b) => a.order - b.order)
            .map((event) => (
              <li key={event.id}>
                <div className="cf-step-heading">
                  <strong>{event.label}</strong>
                  <StatusBadge state={event.state} />
                </div>
                <p>
                  {timestamp(event.at)}
                  {event.profile_id ? ` · ${event.profile_id}` : ""}
                </p>
                <code>{event.stage}</code>
                {event.error_code && <code>{event.error_code}</code>}
              </li>
            ))}
        </ol>
      ) : (
        <p>Nenhum evento detalhado informado.</p>
      )}
      <details className="cf-details">
        <summary>Recursos e detalhes da verificação</summary>
        {run.resources.length ? (
          <ul className="cf-resources" aria-label="Recursos da verificação">
            {run.resources.map((resource) => {
              const confirmed =
                resource.worker_confirmed ?? !!resource.deployment_id;
              const url = confirmed ? safeResourceUrl(resource.url) : null;
              return (
                <li key={`${resource.stack}-${resource.worker_name}`}>
                  <strong>{resource.stack}</strong>
                  {resource.state === "retained" && <p>Retenção habilitada</p>}
                  <p>
                    {confirmed ? "Worker" : "Nome previsto do Worker"}:{" "}
                    <code>{resource.worker_name}</code>
                  </p>
                  {!confirmed && (
                    <p>Criação/publicação ainda não confirmada.</p>
                  )}
                  <p>
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
        ) : (
          <p>Nenhum recurso informado.</p>
        )}
        {run.cleanup?.state === "succeeded" &&
          (run.cleanup.worker_removed || run.cleanup.database_removed) && (
            <p>
              Esta verificação antiga removeu recursos temporários ao terminar.
              A política atual é indicada separadamente.
            </p>
          )}
      </details>
    </section>
  );
}

export function VerificationReadiness({
  snapshot,
  stale,
  loading,
  refresh,
  technicalChildren,
  commandChildren,
  diagnosticTarget,
}: {
  snapshot: Snapshot | null;
  stale: boolean;
  loading: boolean;
  refresh: () => void;
  technicalChildren: ReactNode;
  commandChildren: ReactNode;
  diagnosticTarget?: number;
}) {
  const id = useId();
  const technical = useRef<HTMLDetailsElement>(null);
  const previous = useRef<HTMLDivElement>(null);
  const run = snapshot?.current_run || null;
  const openTechnical = (target: "previous" | "events") => {
    if (technical.current) technical.current.open = true;
    const element =
      target === "previous" ? previous.current : technical.current;
    window.requestAnimationFrame(() => {
      element?.scrollIntoView({ block: "nearest", behavior: "instant" });
      element?.focus({ preventScroll: true });
    });
  };
  useEffect(() => {
    if (diagnosticTarget) openTechnical("previous");
  }, [diagnosticTarget]);
  const tone = stale
    ? "neutral"
    : run
      ? stateTone(run.raw_state === "reconciling" ? "reconciling" : run.state)
      : snapshot?.retry.scheduled
        ? "warning"
        : stateTone(snapshot?.previous_result?.state);
  const heading = stale
    ? "Atualização interrompida"
    : run
      ? runLabel(run)
      : snapshot?.retry.scheduled
        ? snapshot.retry.state === "retry_wait"
          ? "Nova tentativa automática programada"
          : "Verificação automática programada"
        : snapshot?.previous_result
          ? "Verificação finalizada"
          : "Ainda não verificado";
  return (
    <>
      <div className="cloudflare-section-heading">
        <div>
          <h3>Verificação operacional</h3>
          <p className="field-help">
            Execução atual, resultados e evidências por perfil.
          </p>
        </div>
        <RefreshButton loading={loading} refresh={refresh} />
      </div>
      {!snapshot ? (
        loading ? (
          <LoadingState />
        ) : (
          <p className="cf-empty">
            Não foi possível consultar a verificação. Atualize o estado.
          </p>
        )
      ) : (
        <>
          <section
            className={`cf-verification-banner cf-${tone}`}
            aria-label="Acompanhamento da verificação"
          >
            <StateIcon
              tone={tone}
              active={!stale && run?.state === "running"}
            />
            <div className="cf-next-copy">
              <h3>{heading}</h3>
              <p>
                {run
                  ? run.steps
                      .filter((step) => run.current_step_ids.includes(step.id))
                      .map((step) => step.label)
                      .join(" · ") ||
                    (run.state === "queued"
                      ? "Aguardando o executor"
                      : "Acompanhe os eventos reais da execução")
                  : snapshot.retry.scheduled
                    ? timestamp(snapshot.retry.next_attempt_at)
                    : "O último resultado está identificado no histórico abaixo."}
              </p>
              {run?.active_profile_ids.length ? (
                <p>
                  Perfis atuais:{" "}
                  <strong>{run.active_profile_ids.join(", ")}</strong>
                </p>
              ) : null}
              {run && <RunIdentity run={run} />}
              {snapshot.retry.scheduled &&
                !run &&
                snapshot.server_time &&
                snapshot.retry.next_attempt_at &&
                Date.parse(snapshot.retry.next_attempt_at) <
                  Date.parse(snapshot.server_time) && (
                  <p>Horário previsto ultrapassado; aguardando atualização.</p>
                )}
            </div>
            <div className="cf-banner-actions">
              <span>
                <Activity size={18} aria-hidden="true" />
                {stale
                  ? "Atualização indisponível"
                  : snapshot.poll_after_ms || snapshot.polling?.active
                    ? "Atualização automática"
                    : "Consulta concluída"}
              </span>
              <button
                type="button"
                className="button"
                onClick={() => openTechnical("events")}
              >
                Ver eventos
                <ArrowRight size={16} aria-hidden="true" />
              </button>
            </div>
          </section>
          <div className="cf-verification-columns">
            <Timeline
              run={run || snapshot.previous_result}
              historical={!run && !!snapshot.previous_result}
            />
            <PreviousResult
              snapshot={snapshot}
              openDiagnostic={() => openTechnical("previous")}
            />
          </div>
          <section className="cf-card cf-profile-section">
            {snapshot.publication.reasons.length > 0 && (
              <div className="cf-gate-reasons">
                <h4>
                  {stale
                    ? "Impedimentos na última leitura"
                    : "Impedimentos atuais da conexão"}
                </h4>
                <ul>
                  {snapshot.publication.reasons.map((reason, index) => (
                    <li key={`${reason.code}-${index}`}>
                      {reason.message} <code>{reason.code}</code>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <ProfileReadiness snapshot={snapshot} stale={stale} />
            <ProfileResults snapshot={snapshot} stale={stale} />
            <details
              ref={technical}
              className="cf-details cf-technical-details"
              tabIndex={-1}
              id={`${id}-events`}
            >
              <summary>Detalhes técnicos e eventos</summary>
              <dl className="cf-technical-definition">
                <div>
                  <dt>Observado em</dt>
                  <dd>{timestamp(snapshot.observed_at)}</dd>
                </div>
                <div>
                  <dt>Revisão do snapshot</dt>
                  <dd>
                    <code>{shortId(snapshot.revision)}</code>
                  </dd>
                </div>
                <div>
                  <dt>Qualificação formal</dt>
                  <dd>
                    {(
                      {
                        valid: "Válida",
                        invalid: "Inválida",
                        expired: "Vencida",
                        not_started: "Não iniciada",
                        unknown: "Não informada",
                      } as Record<string, string>
                    )[snapshot.qualification.state] ||
                      "Estado não reconhecido"}{" "}
                    · validade: {timestamp(snapshot.qualification.valid_until)}
                  </dd>
                </div>
              </dl>
              {run && (
                <RunTechnical key={run.id} run={run} heading="Execução atual" />
              )}
              <div ref={previous} tabIndex={-1}>
                {snapshot.previous_result && (
                  <RunTechnical
                    key={snapshot.previous_result.id}
                    run={snapshot.previous_result}
                    heading="Diagnóstico do resultado anterior"
                  />
                )}
              </div>
              {technicalChildren}
            </details>
          </section>
          <div className="cf-footnote">
            <Info size={21} aria-hidden="true" />
            <p>
              {snapshot.retention.summary}
              {snapshot.retention.resource_count != null
                ? ` Recursos informados: ${snapshot.retention.resource_count}.`
                : ""}
            </p>
          </div>
          <p className="cf-context-note">
            Esta verificação cobre conexão e perfis. Cada projeto mantém suas
            próprias regras de código, migrations e publicação.
          </p>
        </>
      )}
      {commandChildren}
    </>
  );
}
