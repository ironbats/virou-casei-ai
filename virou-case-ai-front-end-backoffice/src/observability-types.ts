export type RuntimeEvent = {
  id: number;
  at: string;
  service: string;
  level: string;
  account_id: string | null;
  project_id: string | null;
  correlation_id: string;
  stage: string;
  state: string;
  error_code: string | null;
  duration_ms: number | null;
  message: string;
};
export type TraceMetadata = {
  id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  sequence: number;
  operation_id: string | null;
  project_id: string | null;
  account_id: string | null;
  service: string;
  level: string;
  stage: string;
  provider: string | null;
  direction: string;
  attempt?: number | null;
  at: string;
  duration_ms?: number | null;
  status: string;
  http_status?: number | null;
  provider_request_id?: string | null;
  model?: string | null;
  connection_id?: string | null;
  draft_revision?: number | null;
  preparation_id?: string | null;
  snapshot_id?: string | null;
  error_code?: string | null;
  retry_reason?: string | null;
  capture_state: string;
  capture_reason?: string | null;
  section_states?: Partial<Record<PayloadSection, string>>;
  available_sections: string[];
  redacted: boolean;
  truncated: boolean;
  expires_at?: string | null;
  endpoint?: string | null;
  project_revision?: string | null;
  schema_version?: number | null;
  template_versions?:
    | {
        attachment_id: string;
        source_version_id: string;
        attachment_revision: number | null;
      }[]
    | null;
};
export type MonitorFeed<T> = {
  items: T[];
  next_cursor: string | number | null;
  server_time: string;
  retention_days: number;
  can_inspect_payloads?: boolean;
};
export type PayloadSection = "request" | "response" | "normalized" | "fields";
export type TracePayload = {
  trace_id: string;
  section: PayloadSection;
  payload: unknown;
  capture_state: string;
  capture_reason?: string | null;
  redacted_paths: string[];
  truncated_paths: string[];
  policy_version: number | null;
  expires_at: string | null;
};
export type FieldComparison = {
  attachment_id: string;
  field_id: string;
  original_value?: unknown;
  suggested_value?: unknown;
  edited_value?: unknown;
  final_value?: unknown;
  source_type?: string;
  state?: string;
  attachment_revision?: number | null;
  snapshot_id?: string | null;
};
export const phaseLabels: Record<string, string> = {
  recommendation: "Recomendação Jev",
  preparation: "Preenchimento",
  review: "Revisão",
  construction: "Construção",
};
export const captureLabels: Record<string, string> = {
  captured: "Capturado",
  protected: "Dados protegidos",
  truncated: "Captura truncada",
  disabled: "Captura desabilitada",
  not_captured: "Payload não capturado",
  expired: "Payload expirado",
  unavailable: "Payload indisponível",
};
export const sectionLabels: Record<PayloadSection, string> = {
  request: "Request",
  response: "Response",
  normalized: "Resultado mapeado",
  fields: "Campos",
};

export function captureStateForSection(
  trace: TraceMetadata,
  section: PayloadSection,
) {
  if (trace.available_sections.includes(section))
    return trace.section_states?.[section] || "captured";
  if (trace.capture_state === "expired") return "expired";
  if (trace.section_states?.[section] === "unavailable") return "unavailable";
  return ["disabled", "unavailable"].includes(trace.capture_state)
    ? trace.capture_state
    : "not_captured";
}
export const captureReasonLabels: Record<string, string> = {
  NON_JSON_REQUEST:
    "A requisição observada não pôde ser interpretada como JSON. Somente os metadados foram preservados.",
  NON_JSON_RESPONSE:
    "A resposta recebida não pôde ser interpretada como JSON. Somente os metadados foram preservados.",
  CAPTURE_QUOTA_EXCEEDED:
    "A captura não foi armazenada porque atingiu o limite configurado para a operação ou o armazenamento total.",
  RETENTION_PURGED:
    "O conteúdo foi eliminado pela política de retenção ou exclusão do projeto.",
};
