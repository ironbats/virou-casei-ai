import type { PublicationProviderDiagnostic } from "../shared/types";

export type RecoveryResolution = {
  mode: "developer_no_effect";
  action: "close_no_effect";
  eligibility:
    | "requires_evaluation"
    | "evaluating"
    | "eligible"
    | "ineligible"
    | "resolved";
  reason_code: string;
  message?: string;
  blocked_code?: string | null;
  blocked_reason?: string | null;
  cooldown_until?: string | null;
  policy_version?: string;
  evidence_hash?: string;
  plan_id?: string;
  missing_evidence_codes?: string[];
  proofs?: Record<string, boolean>;
  missing_evidence: string[];
  next_action: { type: string; label: string };
};

export type RecoveryRequest = {
  id: string;
  state: "queued" | "running" | "resolving" | "resolved" | "attention";
  requested_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  reason_code: string | null;
};

export function recoveryRequestActive(
  request: RecoveryRequest | null | undefined,
) {
  return (
    !!request && ["queued", "running", "resolving"].includes(request.state)
  );
}

export type RecoverySnapshot = {
  format_version: 1;
  operation_id: string;
  project_id: string;
  revision_id: string | null;
  operation_state: string;
  recovery_state: string | null;
  can_check: boolean;
  capabilities?: {
    can_check: boolean;
    can_check_and_resolve: boolean;
    can_close_now: boolean;
  };
  resolution?: RecoveryResolution;
  state_version?: string;
  checked_at?: string | null;
  recovery_request_id?: string | null;
  request?: RecoveryRequest | null;
  blocked_code: string | null;
  blocked_reason: string | null;
  requested_at: string | null;
  last_checked_at: string | null;
  cooldown_until: string | null;
  operator_required: boolean;
  classification:
    | "unassessed"
    | "confirmed"
    | "no_effect"
    | "schema_applied"
    | "schema_unchanged"
    | "unknown"
    | "conflict"
    | "resolved";
  summary: string;
  required_action: string;
  missing_evidence: string[];
  migration: {
    name: string;
    sha256: string;
    phase?: string;
    effect_outcome?: string;
    statement_count?: number;
  } | null;
  checks: Record<string, string>;
  diagnostic: {
    initial_error?: {
      code: string;
      message: string;
      provider?: PublicationProviderDiagnostic;
    };
    current_error?: { code: string; message: string };
    phase?: string;
    effect_status?: string;
  };
};

const classifications = new Set([
  "unassessed",
  "confirmed",
  "no_effect",
  "schema_applied",
  "schema_unchanged",
  "unknown",
  "conflict",
  "resolved",
]);
const states = new Set([
  "queued",
  "running",
  "reconciling",
  "succeeded",
  "failed",
  "cancelled",
]);
const checkValues: Record<string, ReadonlySet<string>> = {
  identity: new Set(["matched"]),
  source: new Set(["matched"]),
  history: new Set(["matched"]),
  receipt: new Set(["confirmed", "absent"]),
  execution: new Set(["durable_no_effect", "durable_confirmed", "unconfirmed"]),
  schema: new Set(["matches_expected", "matches_baseline", "mismatch"]),
};
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, limit = 2000): value is string =>
  typeof value === "string" && value.length <= limit;
const optionalText = (value: unknown, limit = 2000) =>
  text(value, limit) ? value : undefined;
const nullableText = (value: unknown, limit = 2000) =>
  value === null || value === undefined ? null : optionalText(value, limit);
const timestamp = (value: unknown) =>
  value === null || value === undefined
    ? null
    : text(value, 80) && Number.isFinite(Date.parse(value))
      ? value
      : undefined;

function provider(value: unknown): PublicationProviderDiagnostic | undefined {
  if (!object(value)) return undefined;
  return {
    method: optionalText(value.method, 12),
    status:
      Number.isInteger(value.status) &&
      Number(value.status) >= 100 &&
      Number(value.status) <= 599
        ? Number(value.status)
        : undefined,
    provider_error_codes: Array.isArray(value.provider_error_codes)
      ? value.provider_error_codes
          .filter((code): code is number => Number.isInteger(code) && code >= 0)
          .slice(0, 10)
      : undefined,
    reason: optionalText(value.reason, 100),
    statement_index:
      Number.isInteger(value.statement_index) &&
      Number(value.statement_index) >= 0
        ? Number(value.statement_index)
        : undefined,
    line:
      Number.isInteger(value.line) && Number(value.line) >= 0
        ? Number(value.line)
        : undefined,
    effect_outcome: optionalText(value.effect_outcome, 40),
  };
}

function error(value: unknown) {
  if (!object(value) || !text(value.code, 100) || !text(value.message, 1000))
    return undefined;
  return { code: value.code, message: value.message };
}

export function recoverySnapshot(
  value: unknown,
  operationId: string,
  projectId: string,
  revisionId?: string,
): RecoverySnapshot | null {
  if (
    !object(value) ||
    value.format_version !== 1 ||
    value.operation_id !== operationId ||
    value.project_id !== projectId ||
    (revisionId && value.revision_id !== revisionId) ||
    !text(value.operation_state, 30) ||
    !states.has(value.operation_state) ||
    !text(value.classification, 40) ||
    !classifications.has(value.classification) ||
    typeof value.can_check !== "boolean" ||
    typeof value.operator_required !== "boolean" ||
    !text(value.summary) ||
    !text(value.required_action) ||
    !Array.isArray(value.missing_evidence) ||
    value.missing_evidence.length > 30 ||
    !value.missing_evidence.every((item) => text(item, 1000)) ||
    !object(value.checks) ||
    Object.keys(value.checks).length > 50 ||
    !Object.entries(value.checks).every(
      ([key, item]) => text(key, 100) && text(item, 1000),
    ) ||
    !object(value.diagnostic)
  )
    return null;
  const revision = nullableText(value.revision_id, 120);
  const recoveryState = nullableText(value.recovery_state, 60);
  const blockedCode = nullableText(value.blocked_code, 100);
  const blockedReason = nullableText(value.blocked_reason);
  const requestedAt = timestamp(value.requested_at);
  const lastCheckedAt = timestamp(value.last_checked_at);
  const cooldownUntil = timestamp(value.cooldown_until);
  if (
    [
      revision,
      recoveryState,
      blockedCode,
      blockedReason,
      requestedAt,
      lastCheckedAt,
      cooldownUntil,
    ].includes(undefined)
  )
    return null;
  let migration: RecoverySnapshot["migration"] = null;
  if (value.migration !== null && value.migration !== undefined) {
    const entry = value.migration;
    if (
      !object(entry) ||
      !text(entry.name, 180) ||
      !entry.name.startsWith("migrations/") ||
      !entry.name.endsWith(".sql") ||
      !text(entry.sha256, 64) ||
      !/^[a-f0-9]{64}$/.test(entry.sha256)
    )
      return null;
    migration = {
      name: entry.name,
      sha256: entry.sha256,
      phase: optionalText(entry.phase, 60),
      effect_outcome: optionalText(entry.effect_outcome, 40),
      statement_count:
        Number.isInteger(entry.statement_count) &&
        Number(entry.statement_count) > 0 &&
        Number(entry.statement_count) <= 2000
          ? Number(entry.statement_count)
          : undefined,
    };
  }
  let capabilities: RecoverySnapshot["capabilities"];
  let resolution: RecoverySnapshot["resolution"];
  let request: RecoverySnapshot["request"];
  let stateVersion: string | undefined;
  let checkedAt: string | null | undefined;
  let requestId: string | null | undefined;
  if (value.capabilities !== undefined || value.resolution !== undefined) {
    const capability = value.capabilities;
    const decision = value.resolution;
    if (
      !object(capability) ||
      typeof capability.can_check !== "boolean" ||
      typeof capability.can_check_and_resolve !== "boolean" ||
      typeof capability.can_close_now !== "boolean" ||
      !object(decision) ||
      decision.mode !== "developer_no_effect" ||
      decision.action !== "close_no_effect" ||
      !text(decision.eligibility, 40) ||
      ![
        "requires_evaluation",
        "evaluating",
        "eligible",
        "ineligible",
        "resolved",
      ].includes(decision.eligibility) ||
      !text(decision.reason_code, 100) ||
      !Array.isArray(decision.missing_evidence) ||
      decision.missing_evidence.length > 50 ||
      !decision.missing_evidence.every((item) => text(item, 1000)) ||
      !object(decision.next_action) ||
      !text(decision.next_action.type, 100) ||
      !text(decision.next_action.label, 300) ||
      !text(value.state_version, 200) ||
      !value.state_version
    )
      return null;
    capabilities = {
      can_check: capability.can_check,
      can_check_and_resolve: capability.can_check_and_resolve,
      can_close_now: capability.can_close_now,
    };
    resolution = {
      mode: "developer_no_effect",
      action: "close_no_effect",
      eligibility: decision.eligibility as RecoveryResolution["eligibility"],
      reason_code: decision.reason_code,
      message: optionalText(decision.message, 1000),
      blocked_code: nullableText(decision.blocked_code, 100),
      blocked_reason: nullableText(decision.blocked_reason, 1000),
      cooldown_until: timestamp(decision.cooldown_until),
      policy_version: optionalText(decision.policy_version, 100),
      evidence_hash:
        text(decision.evidence_hash, 64) &&
        /^[a-f0-9]{64}$/.test(decision.evidence_hash)
          ? decision.evidence_hash
          : undefined,
      plan_id: optionalText(decision.plan_id, 120),
      missing_evidence_codes: Array.isArray(decision.missing_evidence_codes)
        ? decision.missing_evidence_codes
            .filter(
              (item): item is string =>
                text(item, 100) && /^[A-Za-z0-9_:-]+$/.test(item),
            )
            .slice(0, 50)
        : undefined,
      proofs: object(decision.proofs)
        ? (Object.fromEntries(
            Object.entries(decision.proofs)
              .filter(
                ([key, item]) =>
                  /^[a-z_]{1,80}$/.test(key) && typeof item === "boolean",
              )
              .slice(0, 50),
          ) as Record<string, boolean>)
        : undefined,
      missing_evidence: [...decision.missing_evidence],
      next_action: {
        type: decision.next_action.type,
        label: decision.next_action.label,
      },
    };
    stateVersion = value.state_version;
    checkedAt = timestamp(value.checked_at);
    requestId = nullableText(value.recovery_request_id, 120);
    if (checkedAt === undefined || requestId === undefined) return null;
    if (value.request === null || value.request === undefined) {
      request = null;
      if (requestId) return null;
    } else {
      const item = value.request;
      if (
        !object(item) ||
        !text(item.id, 120) ||
        !item.id ||
        requestId !== item.id ||
        !text(item.state, 40) ||
        !["queued", "running", "resolving", "resolved", "attention"].includes(
          item.state,
        )
      )
        return null;
      const requested = timestamp(item.requested_at);
      const started = timestamp(item.started_at);
      const completed = timestamp(item.completed_at);
      const reason = nullableText(item.reason_code, 100);
      if ([requested, started, completed, reason].includes(undefined))
        return null;
      request = {
        id: item.id,
        state: item.state as RecoveryRequest["state"],
        requested_at: requested ?? null,
        started_at: started ?? null,
        completed_at: completed ?? null,
        reason_code: reason ?? null,
      };
    }
  }
  const initial = error(value.diagnostic.initial_error);
  return {
    format_version: 1,
    operation_id: operationId,
    project_id: projectId,
    revision_id: revision ?? null,
    operation_state: value.operation_state,
    recovery_state: recoveryState ?? null,
    can_check: value.can_check,
    ...(capabilities
      ? {
          capabilities,
          resolution,
          state_version: stateVersion,
          checked_at: checkedAt,
          recovery_request_id: requestId,
          request,
        }
      : {}),
    blocked_code: blockedCode ?? null,
    blocked_reason: blockedReason ?? null,
    requested_at: requestedAt ?? null,
    last_checked_at: lastCheckedAt ?? null,
    cooldown_until: cooldownUntil ?? null,
    operator_required: value.operator_required,
    classification: value.classification as RecoverySnapshot["classification"],
    summary: value.summary,
    required_action: value.required_action,
    missing_evidence: [...value.missing_evidence],
    migration,
    checks: Object.fromEntries(
      Object.entries(value.checks).filter(
        ([key, item]) =>
          Object.hasOwn(checkValues, key) &&
          checkValues[key].has(item as string),
      ),
    ) as Record<string, string>,
    diagnostic: {
      initial_error: initial
        ? {
            ...initial,
            provider: object(value.diagnostic.initial_error)
              ? provider(value.diagnostic.initial_error.provider)
              : undefined,
          }
        : undefined,
      current_error: error(value.diagnostic.current_error),
      phase: optionalText(value.diagnostic.phase, 60),
      effect_status: optionalText(value.diagnostic.effect_status, 40),
    },
  };
}

export function recoveryExport(snapshot: RecoverySnapshot) {
  const safe = recoverySnapshot(
    snapshot,
    snapshot.operation_id,
    snapshot.project_id,
    snapshot.revision_id ?? undefined,
  );
  if (!safe)
    throw new Error("O diagnóstico de recuperação não pode ser exportado.");
  return JSON.stringify(
    {
      export_format: "viroucaseai_publication_recovery_v1",
      exported_at: new Date().toISOString(),
      ...safe,
    },
    null,
    2,
  );
}
