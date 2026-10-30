import type { Connection } from "./integration-types";
import type { PublicationDiagnostic } from "./use-publication-diagnostics";
import type { Verification } from "./publication-verification-status";

export type EvidenceState =
  | "unknown"
  | "not_started"
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped"
  | "cancelled"
  | string;
export type Tone = "neutral" | "success" | "warning" | "danger" | "running";
export type Gate = {
  allowed: boolean;
  state: string;
  reason_code: string | null;
  reason_text: string | null;
  evaluated_at?: string | null;
  applicable_profiles: string[];
  reasons: { code: string; message: string }[];
};
export type Probe = {
  key: string;
  state: EvidenceState;
  observed_http_status: number | null;
  expected_summary: string | null;
  observed_summary: string | null;
  error_code: string | null;
  observed_at: string | null;
  duration_ms?: number | null;
};
export type Step = {
  id: string;
  order: number;
  phase_id?: string;
  stage?: string;
  label: string;
  state: EvidenceState;
  profile_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
  probes: Probe[];
};
export type RunEvent = {
  id: string;
  order: number;
  stage: string;
  label: string;
  state: EvidenceState;
  profile_id: string | null;
  at: string | null;
  error_code: string | null;
};
export type Run = {
  id: string;
  operation_id?: string;
  interrupted?: boolean;
  configuration_id: string | null;
  configuration_version: number | string | null;
  configuration_fingerprint: string | null;
  executor_fingerprint: string | null;
  configuration_current: boolean | null;
  state: EvidenceState;
  raw_state: string;
  profile_ids: string[];
  active_profile_ids: string[];
  current_step_ids: string[];
  started_at: string | null;
  finished_at: string | null;
  last_event_at: string | null;
  duration_ms: number | null;
  steps: Step[];
  error_code: string | null;
  error_summary: string | null;
  failed_stage: string | null;
  failed_profile: string | null;
  correlation_id: string | null;
  events: RunEvent[];
  stack_results: {
    stack: string;
    passed: boolean | null;
    state?: string;
    stage?: string | null;
    observed_at?: string | null;
    configuration_current?: boolean;
    failure?: {
      code?: string;
      message?: string;
      stage?: string;
      stack?: string | null;
    } | null;
    remote?: unknown;
    bindings?: unknown;
    revision_id?: string | null;
  }[];
  failure?: unknown;
  remote_verification?: unknown;
  resources: NonNullable<Verification["resources"]>;
  cleanup: Verification["cleanup"];
};
export type Snapshot = {
  schema_version: string | number;
  revision: string;
  revision_order?: string;
  observed_at: string | null;
  server_time: string | null;
  connection: {
    id: string;
    name: string;
    active: boolean;
    is_default: boolean;
    state: string;
    environment: string;
    account_masked: string;
    configuration_id: string;
    configuration_version: number | string;
    configuration_fingerprint: string | null;
    executor_fingerprint: string | null;
  };
  credential: {
    state: string;
    checked_at: string | null;
    valid_until?: string | null;
    reason_code?: string | null;
    type?: string;
  };
  qualification: {
    state: string;
    checked_at: string | null;
    valid_until?: string | null;
    reason_code?: string | null;
  };
  publication: Gate;
  current_run: Run | null;
  previous_result: Run | null;
  profiles: {
    id: string;
    label: string;
    publication: Gate;
    verification_state: EvidenceState;
    evidence_run_id: string | null;
    evidence_valid_until: string | null;
    evidence_state: string;
  }[];
  retry: {
    scheduled: boolean;
    next_attempt_at: string | null;
    failure_count: number | null;
    count_scope: string | null;
    reason_code: string | null;
    reason_text: string | null;
    state: string;
  };
  retention: {
    enabled: boolean | null;
    summary: string;
    resource_count: number | null;
  };
  actions: {
    key: string;
    label: string;
    enabled: boolean;
    disabled_reason: string | null;
    kind: "read" | "command";
  }[];
  poll_after_ms?: number | null;
  polling?: { active: boolean; interval_ms: number | null };
};

export const stateLabel = (state?: string | null): string =>
  ({
    unknown: "Não informado",
    not_started: "Não iniciada",
    queued: "Na fila",
    running: "Em execução",
    succeeded: "Concluída",
    failed: "Falhou",
    skipped: "Não aplicável",
    cancelled: "Cancelada",
    reconciling: "Aguardando retomada",
  })[state || "unknown"] || "Estado não reconhecido";

export const stateTone = (state?: string | null): Tone =>
  (({
    queued: "running",
    running: "running",
    succeeded: "success",
    failed: "danger",
    reconciling: "warning",
    cancelled: "warning",
  })[state || ""] as Tone | undefined) || "neutral";

export function shortId(value?: string | number | null) {
  if (value == null || value === "") return "Não informado";
  const text = String(value);
  return text.length > 22 ? `${text.slice(0, 10)}…${text.slice(-6)}` : text;
}

export function timestamp(value?: string | null) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Não informado";
  return new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

export function duration(value?: number | null) {
  if (value == null || !Number.isFinite(value) || value < 0)
    return "Não informada";
  const seconds = Math.floor(value / 1000);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}min ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}min`;
}

export function maskedAccount(value: unknown) {
  const text = typeof value === "string" ? value : "";
  return text
    ? text.length > 8
      ? `${text.slice(0, 4)}…${text.slice(-4)}`
      : "••••"
    : "Não informada";
}

function legacyRun(value: Verification): Run {
  return {
    id: value.id,
    configuration_id: null,
    configuration_version: null,
    configuration_fingerprint: null,
    executor_fingerprint: null,
    configuration_current: null,
    state: value.state,
    raw_state: value.state,
    profile_ids: value.stack_results?.map((x) => x.stack) || [],
    active_profile_ids: value.stack ? [value.stack] : [],
    current_step_ids: [],
    started_at: null,
    finished_at: null,
    last_event_at: null,
    duration_ms: null,
    steps: [],
    error_code: value.error_code,
    error_summary: value.failure?.message || null,
    failed_stage: value.failure?.stage || value.failed_stage || null,
    failed_profile: value.failure?.stack || value.stack || null,
    correlation_id: null,
    events: [],
    stack_results: value.stack_results || [],
    resources: value.resources || [],
    cleanup: value.cleanup,
  };
}

export function managementSnapshot(
  data: PublicationDiagnostic | null,
  config: Connection,
): Snapshot | null {
  if (!data) return null;
  if (data.snapshot) return data.snapshot;
  const legacy = data.verification ? legacyRun(data.verification) : null;
  const active =
    legacy && ["queued", "running", "reconciling"].includes(legacy.state)
      ? legacy
      : null;
  const automatic = data.automatic_verification;
  const gate = (
    allowed: boolean,
    reasons: { code: string; message: string }[],
    ids: string[],
  ): Gate => ({
    allowed,
    state: allowed ? "allowed" : "blocked",
    reason_code: reasons[0]?.code || null,
    reason_text: reasons[0]?.message || null,
    applicable_profiles: ids,
    reasons,
  });
  const profiles = (data.stack_readiness || []).map((profile) => ({
    id: profile.stack,
    label: profile.stack,
    publication: gate(profile.can_publish, profile.reasons, [profile.stack]),
    verification_state: active?.active_profile_ids.includes(profile.stack)
      ? active.state
      : "unknown",
    evidence_run_id: null,
    evidence_valid_until: null,
    evidence_state: "unknown",
  }));
  return {
    schema_version: "legacy",
    revision: "",
    revision_order: "request_generation",
    observed_at: null,
    server_time: null,
    connection: {
      id: config.connection_id,
      name: config.name,
      active: config.state === "active",
      is_default: config.is_default,
      state: config.state,
      environment: String(config.settings.environment || "Não informado"),
      account_masked: maskedAccount(config.settings.account_id),
      configuration_id: config.id,
      configuration_version: config.version,
      configuration_fingerprint: config.configuration_digest || null,
      executor_fingerprint: config.executor_digest || null,
    },
    credential: { state: "unknown", checked_at: config.validated_at || null },
    qualification: { state: "unknown", checked_at: null },
    publication: {
      ...gate(
        data.can_publish,
        data.reasons,
        profiles.map((x) => x.id),
      ),
      state: data.can_publish
        ? profiles.some((x) => !x.publication.allowed)
          ? "partial"
          : "allowed"
        : "blocked",
    },
    current_run: active,
    previous_result: active ? null : legacy,
    profiles,
    retry: {
      scheduled:
        !active &&
        !!automatic?.next_attempt_at &&
        ["scheduled", "retry_wait"].includes(automatic.state),
      next_attempt_at: automatic?.next_attempt_at || null,
      failure_count: automatic?.consecutive_failures ?? null,
      count_scope: "consecutive_failures",
      reason_code: automatic?.last_error_code || null,
      reason_text: automatic?.message || null,
      state: automatic?.state || "unknown",
    },
    retention: {
      enabled: legacy?.cleanup?.state === "retained" ? true : null,
      summary:
        legacy?.cleanup?.state === "retained"
          ? "Retenção habilitada. Os recursos de teste são preservados, inclusive em caso de falha."
          : "Consulte a política e os recursos da verificação no diagnóstico.",
      resource_count: legacy?.resources.length ?? null,
    },
    actions: [],
    poll_after_ms: active || automatic?.next_attempt_at ? 4000 : null,
  };
}

export function gateSummary(gate: Gate) {
  const messages: Record<string, string> = {
    CONFIGURATION_PENDING: "Configuração ainda não cadastrada",
    CONFIGURATION_INVALID: "Validação da configuração pendente",
    CAPABILITY_UNVALIDATED: "Verificação operacional pendente",
    EXECUTOR_UNAVAILABLE: "Executor indisponível",
    UNSUPPORTED_STACK: "Perfil não suportado pelo executor",
    LIFECYCLE_LOCKED: "Operação bloqueada pela política atual",
  };
  return gate.reason_code && messages[gate.reason_code]
    ? messages[gate.reason_code]
    : gate.reason_text && gate.reason_text.length <= 110
      ? gate.reason_text
      : "Consulte o motivo no diagnóstico.";
}

export function publicationView(snapshot: Snapshot | null, stale: boolean) {
  if (stale)
    return {
      title: "Não foi possível confirmar",
      detail:
        "Atualização indisponível. Consulte o último estado observado abaixo.",
      tone: "neutral" as Tone,
    };
  if (!snapshot)
    return {
      title: "Não verificada",
      detail: "Aguardando a leitura do backend.",
      tone: "neutral" as Tone,
    };
  const gate = snapshot.publication;
  if (gate.allowed)
    return {
      title: gate.state === "partial" ? "Parcialmente liberada" : "Liberada",
      detail:
        gate.state === "partial"
          ? "Consulte os perfis aprovados na aba Verificação."
          : "Sujeita às regras próprias de cada projeto.",
      tone:
        gate.state === "partial" ? ("warning" as Tone) : ("success" as Tone),
    };
  const label =
    (
      {
        unknown: "Não foi possível confirmar",
        unavailable: "Indisponível",
        not_verified: "Não verificada",
        unverified: "Não verificada",
      } as Record<string, string>
    )[gate.state] || "Bloqueada";
  return {
    title: label,
    detail: gateSummary(gate),
    tone: ["unknown", "unavailable", "not_verified", "unverified"].includes(
      gate.state,
    )
      ? ("neutral" as Tone)
      : ("warning" as Tone),
  };
}

export function runLabel(run: Run | null) {
  if (!run) return "Ainda não verificado";
  if (run.interrupted) return "Tentativa anterior interrompida";
  if (run.raw_state === "reconciling") return "Verificação aguardando retomada";
  if (run.state === "running") return "Verificação em andamento";
  if (run.state === "queued") return "Verificação na fila";
  if (run.state === "failed" && run.stack_results.some((x) => x.passed))
    return "Concluída com aprovação parcial";
  return stateLabel(run.state);
}

export function failureSummary(code?: string | null, message?: string | null) {
  const labels: Record<string, string> = {
    PUBLICATION_API_NOT_READY: "API não respondeu como esperado",
    PUBLICATION_WEB_NOT_READY:
      "Aplicação publicada não respondeu como esperado",
    PUBLICATION_REMOTE_TIMEOUT:
      "Aplicação publicada excedeu o tempo de resposta",
    PUBLICATION_RESULT_UNKNOWN: "Resultado da publicação ainda não confirmado",
  };
  return code && labels[code]
    ? labels[code]
    : message ||
        "Não foi possível confirmar o resultado. Consulte o diagnóstico.";
}

export function attemptCount(snapshot: Snapshot) {
  const { failure_count: count, count_scope: scope } = snapshot.retry;
  if (count == null) return null;
  const label =
    (
      {
        consecutive_failures: "falhas consecutivas",
        configuration_attempts: "tentativas desta configuração",
        historical_attempts: "tentativas no histórico",
      } as Record<string, string>
    )[scope || ""] || "tentativas (escopo não informado)";
  return `${count} ${label}`;
}

function object(value: unknown): Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}
function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function strings(value: unknown): string[] {
  return list(value).filter((item): item is string => typeof item === "string");
}
function reasons(value: unknown): Gate["reasons"] {
  return list(value)
    .map(object)
    .filter((item) => text(item.code) || text(item.message))
    .map((item) => ({
      code: text(item.code) || "UNKNOWN",
      message: text(item.message) || "Motivo não informado",
    }));
}
function normalizedGate(value: unknown): Gate {
  const source = object(value);
  const state = text(source.state) || "unknown";
  const compatible = ["allowed", "partial"].includes(state);
  return {
    allowed: source.allowed === true && compatible,
    state:
      typeof source.allowed !== "boolean" ||
      (source.allowed === true && !compatible)
        ? "unknown"
        : state,
    reason_code: text(source.reason_code),
    reason_text: text(source.reason_text),
    evaluated_at: text(source.evaluated_at),
    applicable_profiles: strings(source.applicable_profiles),
    reasons: reasons(source.reasons),
  };
}
function normalizedRun(value: unknown): Run | null {
  const source = object(value);
  const id = text(source.id);
  if (!id) return null;
  const steps = list(source.steps)
    .map(object)
    .map(
      (step, index): Step => ({
        id: text(step.id) || `unknown-${index}`,
        order: number(step.order) ?? index,
        phase_id: text(step.phase_id) || undefined,
        stage: text(step.stage) || undefined,
        label: text(step.label) || "Etapa não informada",
        state: text(step.state) || "unknown",
        profile_id: text(step.profile_id),
        started_at: text(step.started_at),
        finished_at: text(step.finished_at),
        message: text(step.message),
        probes: list(step.probes)
          .map(object)
          .map((probe, probeIndex) => ({
            key: text(probe.key) || `Probe ${probeIndex + 1}`,
            state: text(probe.state) || "unknown",
            observed_http_status: number(probe.observed_http_status),
            expected_summary: text(probe.expected_summary),
            observed_summary: text(probe.observed_summary),
            error_code: text(probe.error_code),
            observed_at: text(probe.observed_at),
            duration_ms: number(probe.duration_ms),
          })),
      }),
    );
  return {
    id,
    operation_id: text(source.operation_id) || undefined,
    interrupted: source.interrupted === true,
    configuration_id: text(source.configuration_id),
    configuration_version:
      number(source.configuration_version) ??
      text(source.configuration_version),
    configuration_fingerprint: text(source.configuration_fingerprint),
    executor_fingerprint: text(source.executor_fingerprint),
    configuration_current:
      typeof source.configuration_current === "boolean"
        ? source.configuration_current
        : null,
    state: text(source.state) || "unknown",
    raw_state: text(source.raw_state) || text(source.state) || "unknown",
    profile_ids: strings(source.profile_ids),
    active_profile_ids: strings(source.active_profile_ids),
    current_step_ids: strings(source.current_step_ids),
    started_at: text(source.started_at),
    finished_at: text(source.finished_at),
    last_event_at: text(source.last_event_at),
    duration_ms: number(source.duration_ms),
    steps,
    error_code: text(source.error_code),
    error_summary: text(source.error_summary),
    failed_stage: text(source.failed_stage),
    failed_profile: text(source.failed_profile),
    correlation_id: text(source.correlation_id),
    failure: source.failure ?? null,
    remote_verification: source.remote_verification ?? null,
    events: list(source.events)
      .map(object)
      .map((event, index) => ({
        id: text(event.id) || `${id}-event-${index}`,
        order: number(event.order) ?? index,
        stage: text(event.stage) || "unknown",
        label: text(event.label) || "Evento não informado",
        state: text(event.state) || "unknown",
        profile_id: text(event.profile_id),
        at: text(event.at),
        error_code: text(event.error_code),
      })),
    stack_results: list(source.stack_results)
      .map(object)
      .filter((result) => text(result.stack))
      .map((result) => {
        const failure = object(result.failure);
        return {
          stack: text(result.stack)!,
          remote: result.remote ?? null,
          bindings: result.bindings ?? null,
          revision_id: text(result.revision_id),
          passed: typeof result.passed === "boolean" ? result.passed : null,
          state: text(result.state) || "unknown",
          stage: text(result.stage),
          observed_at: text(result.observed_at),
          configuration_current:
            typeof result.configuration_current === "boolean"
              ? result.configuration_current
              : undefined,
          failure: result.failure
            ? {
                code: text(failure.code) || undefined,
                message: text(failure.message) || undefined,
                stage: text(failure.stage) || undefined,
                stack: text(failure.stack),
              }
            : null,
        };
      }),
    resources: list(source.resources)
      .map(object)
      .filter((resource) => text(resource.stack))
      .map((resource) => ({
        stack: text(resource.stack)!,
        worker_name: text(resource.worker_name) || "Não informado",
        database_id: text(resource.database_id),
        url: text(resource.url),
        state: text(resource.state) || "unknown",
        worker_confirmed:
          typeof resource.worker_confirmed === "boolean"
            ? resource.worker_confirmed
            : undefined,
        deployment_id: text(resource.deployment_id),
      })),
    cleanup: source.cleanup
      ? {
          state: text(object(source.cleanup).state) || "unknown",
          worker_removed: object(source.cleanup).worker_removed === true,
          database_removed: object(source.cleanup).database_removed === true,
        }
      : null,
  };
}

export function normalizeSnapshot(value: unknown): Snapshot {
  const source = object(value);
  const connection = object(source.connection);
  const configurationVersion =
    number(connection.configuration_version) ??
    text(connection.configuration_version);
  if (
    String(source.schema_version) !== "1" ||
    !text(connection.id) ||
    configurationVersion == null
  ) {
    throw new Error(
      "O diagnóstico recebido é incompatível com esta conexão. Atualize o estado.",
    );
  }
  const credential = object(source.credential);
  const qualification = object(source.qualification);
  const retry = object(source.retry);
  const retention = object(source.retention);
  const polling = object(source.polling);
  const currentRun = normalizedRun(source.current_run);
  const prior = normalizedRun(source.previous_result);
  const previousResult = prior?.id === currentRun?.id ? null : prior;
  return {
    schema_version: String(source.schema_version),
    revision: text(source.revision) || "",
    revision_order: text(source.revision_order) || "request_generation",
    observed_at: text(source.observed_at),
    server_time: text(source.server_time),
    connection: {
      id: text(connection.id)!,
      name: text(connection.name) || "Conexão",
      active: connection.active === true,
      is_default: connection.is_default === true,
      state: text(connection.state) || "unknown",
      environment: text(connection.environment) || "Não informado",
      account_masked: text(connection.account_masked) || "Não informada",
      configuration_id: text(connection.configuration_id) || "",
      configuration_version: configurationVersion,
      configuration_fingerprint: text(connection.configuration_fingerprint),
      executor_fingerprint: text(connection.executor_fingerprint),
    },
    credential: {
      state: text(credential.state) || "unknown",
      checked_at: text(credential.checked_at),
      valid_until: text(credential.valid_until),
      reason_code: text(credential.reason_code),
      type: text(credential.type) || undefined,
    },
    qualification: {
      state: text(qualification.state) || "unknown",
      checked_at: text(qualification.checked_at),
      valid_until: text(qualification.valid_until),
      reason_code: text(qualification.reason_code),
    },
    publication: normalizedGate(source.publication),
    current_run: currentRun,
    previous_result: previousResult,
    profiles: list(source.profiles)
      .map(object)
      .filter((profile) => text(profile.id))
      .map((profile) => ({
        id: text(profile.id)!,
        label: text(profile.label) || text(profile.id)!,
        publication: normalizedGate(profile.publication),
        verification_state: text(profile.verification_state) || "unknown",
        evidence_run_id: text(profile.evidence_run_id),
        evidence_valid_until: text(profile.evidence_valid_until),
        evidence_state: text(profile.evidence_state) || "unknown",
      })),
    retry: {
      scheduled:
        !currentRun &&
        retry.scheduled === true &&
        !!text(retry.next_attempt_at),
      next_attempt_at: text(retry.next_attempt_at),
      failure_count: number(retry.failure_count),
      count_scope: text(retry.count_scope),
      reason_code: text(retry.reason_code),
      reason_text: text(retry.reason_text),
      state: text(retry.state) || "unknown",
    },
    retention: {
      enabled:
        typeof retention.enabled === "boolean" ? retention.enabled : null,
      summary: text(retention.summary) || "Política de retenção não informada.",
      resource_count: number(retention.resource_count),
    },
    actions: list(source.actions)
      .map(object)
      .filter((action) => text(action.key))
      .map((action) => ({
        key: text(action.key)!,
        label: text(action.label) || "Ação",
        enabled: action.enabled === true,
        disabled_reason: text(action.disabled_reason),
        kind: action.kind === "command" ? "command" : "read",
      })),
    poll_after_ms: number(source.poll_after_ms) ?? number(polling.interval_ms),
    polling: {
      active: polling.active === true,
      interval_ms: number(polling.interval_ms),
    },
  };
}
