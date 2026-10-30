import type { Step } from "../src/cloudflare-snapshot";

export const managementConnection = {
  id: "configuration-test",
  connection_id: "connection-test",
  name: "Cloudflare produção",
  is_default: true,
  version: 7,
  state: "active",
  settings: { account_id: "a".repeat(32), environment: "production" },
  validation: { ready: true },
  has_token: true,
  validated_at: "2026-10-30T11:51:56Z",
};

const observed = "2026-10-30T12:40:00Z";
const profileIds = ["hono-typescript-d1", "fastapi-python-d1"];

export function gate(allowed = false, state = allowed ? "allowed" : "blocked") {
  return {
    allowed,
    state,
    reason_code: allowed ? null : "CAPABILITY_UNVALIDATED",
    reason_text: allowed ? null : "Verificação operacional pendente",
    evaluated_at: observed,
    applicable_profiles: [...profileIds],
    reasons: allowed
      ? []
      : [{ code: "CAPABILITY_UNVALIDATED", message: "Verificação operacional pendente" }],
  };
}

export function verificationRun(state = "running", id = "run-current-008") {
  const current = state === "running" || state === "queued";
  const passed = state === "succeeded";
  return {
    id,
    configuration_id: "configuration-test",
    configuration_version: 7,
    configuration_fingerprint: "configuration-fingerprint-7",
    executor_fingerprint: "executor-fingerprint-7",
    configuration_current: true,
    state,
    raw_state: state,
    profile_ids: [...profileIds],
    active_profile_ids: current && state !== "queued" ? [profileIds[0]] : [],
    current_step_ids: current && state !== "queued" ? ["verification_remote:hono-typescript-d1"] : [],
    started_at: current ? "2026-10-30T12:38:00Z" : "2026-10-30T11:48:00Z",
    finished_at: current ? null : "2026-10-30T11:51:56Z",
    last_event_at: current ? "2026-10-30T12:39:50Z" : "2026-10-30T11:51:56Z",
    duration_ms: current ? null : 236000,
    error_code: state === "failed" ? "PUBLICATION_API_NOT_READY" : null,
    error_summary: state === "failed" ? "API não respondeu como esperado" : null,
    failed_stage: state === "failed" ? "verification_remote" : null,
    failed_profile: state === "failed" ? profileIds[0] : null,
    correlation_id: `correlation-${id}`,
    steps: [
      {
        id: "verification_initializing",
        order: 0,
        label: "Preparar ambiente",
        state: state === "queued" ? "not_started" : "succeeded",
        profile_id: null,
        started_at: "2026-10-30T12:38:00Z",
        finished_at: "2026-10-30T12:38:10Z",
        message: "Ambiente conferido pelo executor",
        probes: [],
      },
      {
        id: "verification_uploading:hono-typescript-d1",
        order: 1,
        label: "Publicar aplicação de teste",
        state: state === "queued" ? "not_started" : "succeeded",
        profile_id: profileIds[0],
        started_at: "2026-10-30T12:38:11Z",
        finished_at: "2026-10-30T12:39:40Z",
        message: "Publicação confirmada",
        probes: [],
      },
      {
        id: "verification_remote:hono-typescript-d1",
        order: 2,
        label: "Validar HTTP e JSON",
        state: state === "queued" ? "not_started" : state,
        profile_id: profileIds[0],
        started_at: "2026-10-30T12:39:41Z",
        finished_at: current ? null : "2026-10-30T11:51:56Z",
        message: current ? "Testando a aplicação publicada" : passed ? "Contrato HTTP e JSON validado" : "Resposta JSON incompatível",
        probes: state === "failed"
          ? [{ key: "api_contract", state: "failed", observed_http_status: 200, expected_summary: "JSON com status válido", observed_summary: "Campo de status incompatível", error_code: "PUBLICATION_API_NOT_READY", observed_at: "2026-10-30T11:51:56Z", duration_ms: 301 }]
          : [],
      },
      {
        id: "verification_complete",
        order: 3,
        label: "Consolidar resultado",
        state: passed ? "succeeded" : "not_started",
        profile_id: null,
        started_at: null,
        finished_at: passed ? "2026-10-30T11:51:56Z" : null,
        message: null,
        probes: [],
      },
    ] as Step[],
    events: [
      { id: `${id}:1`, order: 0, stage: "verification_initializing", label: "Ambiente conferido", state: "succeeded", profile_id: null, at: "2026-10-30T12:38:10Z", error_code: null },
      { id: `${id}:2`, order: 1, stage: "verification_remote", label: "Testando aplicação publicada", state, profile_id: profileIds[0], at: "2026-10-30T12:39:50Z", error_code: state === "failed" ? "PUBLICATION_API_NOT_READY" : null },
    ],
    stack_results: current
      ? []
      : profileIds.map((stack) => ({ stack, passed, failure: passed ? null : { code: "PUBLICATION_API_NOT_READY", message: "API não respondeu como esperado", stage: "verification_remote", stack } })),
    remote_verification: state === "failed" ? { passed: false } : null,
    resources: [
      { stack: profileIds[0], worker_name: "synthetic-readiness-worker", database_id: "database-test", url: "https://readiness-fixture.workers.dev", state: "retained", worker_confirmed: true, deployment_id: "deployment-test" },
    ],
    cleanup: { state: "retained", worker_removed: false, database_removed: false },
  };
}

export function managementSnapshot(mode = "running") {
  const active = ["running", "queued", "parallel"].includes(mode);
  const allowed = ["ready", "partial"].includes(mode);
  const never = mode === "never";
  const previous = never ? null : verificationRun(allowed ? "succeeded" : "failed", "run-previous-007");
  const current = active ? verificationRun(mode === "queued" ? "queued" : "running") : null;
  const snapshot = {
    schema_version: 1,
    revision: "opaque-revision-8",
    revision_order: "request_generation",
    observed_at: observed,
    server_time: observed,
    connection: {
      id: managementConnection.connection_id,
      name: managementConnection.name,
      active: mode !== "inactive",
      is_default: true,
      state: mode === "inactive" ? "disabled" : "active",
      environment: "production",
      account_masked: "aaaa…aaaa",
      configuration_id: "configuration-test",
      configuration_version: 7,
      configuration_fingerprint: "configuration-fingerprint-7",
      executor_fingerprint: "executor-fingerprint-7",
    },
    credential: { state: "valid", checked_at: "2026-10-30T11:51:56Z", valid_until: null, reason_code: null },
    qualification: { state: "unknown", checked_at: null, valid_until: null, reason_code: null },
    publication: gate(allowed, mode === "partial" ? "partial" : never ? "not_verified" : mode === "unknown" ? "unknown" : allowed ? "allowed" : "blocked"),
    current_run: current,
    previous_result: previous,
    profiles: profileIds.map((id, index) => ({
      id,
      label: id,
      publication: gate(allowed && (mode !== "partial" || index === 0)),
      verification_state: current ? index === 0 ? current.state : "not_started" : "not_started",
      evidence_run_id: previous?.id ?? null,
      evidence_valid_until: allowed ? "2026-10-31T12:00:00Z" : null,
      evidence_state: allowed ? "valid" : never ? "missing" : "invalid",
    })),
    retry: {
      scheduled: mode === "retry" || mode === "overdue",
      next_attempt_at: mode === "retry" ? "2026-10-30T12:51:56Z" : mode === "overdue" ? "2026-10-30T12:30:00Z" : null,
      failure_count: allowed || never ? 0 : 17,
      count_scope: "consecutive_failures",
      reason_code: allowed || never ? null : "PUBLICATION_API_NOT_READY",
      reason_text: "A verificação automática será repetida após o intervalo de recuperação.",
      state: active ? "running" : mode === "retry" || mode === "overdue" ? "retry_wait" : mode === "inactive" ? "disabled" : "idle",
    },
    retention: { enabled: true, summary: "Retenção habilitada. Os recursos de teste são preservados, inclusive em caso de falha.", resource_count: 1 },
    actions: [
      { key: "refresh", label: "Atualizar estado", enabled: true, disabled_reason: null, kind: "read" },
      { key: "verify", label: "Verificar publicação", enabled: !active, disabled_reason: active ? "Verificação em andamento" : null, kind: "command" },
    ],
    polling: { active: active || ["retry", "overdue"].includes(mode), interval_ms: active || ["retry", "overdue"].includes(mode) ? 4000 : null },
  };
  if (mode === "changed" && snapshot.previous_result) {
    snapshot.previous_result.configuration_version = 6;
    snapshot.previous_result.configuration_current = false;
    snapshot.previous_result.configuration_id = "configuration-old";
    snapshot.publication.reason_code = "CONFIGURATION_CHANGED";
    snapshot.publication.reason_text = "Verificação anterior não se aplica à configuração atual";
  }
  if (mode === "parallel" && snapshot.current_run) {
    snapshot.current_run.active_profile_ids = [...profileIds];
    snapshot.current_run.current_step_ids.push("verification_remote:fastapi-python-d1");
    snapshot.current_run.steps.push({ ...snapshot.current_run.steps[2], id: "verification_remote:fastapi-python-d1", order: 3, profile_id: profileIds[1] });
    snapshot.profiles[1].verification_state = "running";
  }
  return snapshot;
}

export function managementDiagnostics(mode = "running") {
  const snapshot = managementSnapshot(mode);
  const selected = snapshot.current_run || snapshot.previous_result;
  return {
    can_publish: snapshot.publication.allowed,
    can_verify: true,
    reasons: snapshot.publication.reasons,
    verification_reasons: [],
    workers: [{ id: "worker:test", available: true, code: "READY", message: "Executor pronto", checked_at: observed }],
    stack_readiness: snapshot.profiles.map((profile) => ({ stack: profile.id, can_publish: profile.publication.allowed, reasons: profile.publication.reasons })),
    verification: selected ? { ...selected, stage: snapshot.current_run ? "verification_remote" : "failed", error_code: selected.error_code, stack: selected.failed_profile } : null,
    automatic_verification: { enabled: mode !== "inactive", state: snapshot.retry.state, message: snapshot.retry.reason_text, next_attempt_at: snapshot.retry.next_attempt_at, last_attempt_at: selected?.started_at ?? null, last_succeeded_at: mode === "ready" ? selected?.finished_at : null, last_error_code: snapshot.retry.reason_code, consecutive_failures: snapshot.retry.failure_count, operation_id: selected?.id ?? null },
    snapshot,
    poll_after_ms: 4000,
  };
}
