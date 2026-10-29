import type { Page, Request } from "@playwright/test";
import type { TraceMetadata, TracePayload } from "../src/observability-types";

export const event = {
  id: 10,
  at: "2026-10-28T12:00:00Z",
  service: "jev",
  level: "info",
  account_id: "account-fixture",
  project_id: "project-fixture",
  correlation_id: "journey-fixture",
  stage: "recommendation_ready",
  state: "ready",
  error_code: null,
  duration_ms: 3822,
  message: "Jev: sugestões recebidas.",
};
export const feed = {
  items: [event],
  next_cursor: null,
  server_time: "2026-10-28T12:00:00Z",
  retention_days: 7,
};
export const trace: TraceMetadata = {
  id: "trace-preparation",
  trace_id: "journey-fixture",
  span_id: "span-preparation",
  parent_span_id: "span-recommendation",
  sequence: 2,
  operation_id: "operation-preparation",
  project_id: "project-fixture",
  account_id: "account-fixture",
  service: "agents",
  level: "error",
  stage: "preparation",
  provider: "openai",
  direction: "exchange",
  attempt: 1,
  at: "2026-10-28T12:00:00Z",
  duration_ms: 3822,
  status: "failed",
  http_status: 200,
  provider_request_id: "request-provider",
  model: "model-fixture",
  connection_id: "connection-fixture",
  draft_revision: 4,
  preparation_id: "preparation-fixture",
  error_code: "MODEL_OUTPUT_INVALID",
  capture_state: "protected",
  available_sections: ["request", "response", "normalized", "fields"],
  redacted: true,
  truncated: false,
  expires_at: "2026-11-04T12:00:00Z",
};
export const timeline: TraceMetadata[] = [
  {
    ...trace,
    id: "trace-recommendation",
    sequence: 1,
    stage: "recommendation",
    operation_id: "operation-recommendation",
    span_id: "span-recommendation",
    parent_span_id: null,
    provider: "jev",
    status: "ready",
    level: "info",
    error_code: undefined,
  },
  trace,
  {
    ...trace,
    id: "trace-repair",
    sequence: 3,
    span_id: "span-repair",
    parent_span_id: trace.span_id,
    attempt: 2,
    status: "ready",
    error_code: undefined,
  },
  {
    ...trace,
    id: "trace-review",
    sequence: 4,
    stage: "review",
    operation_id: "operation-review",
    provider: "platform",
    direction: "fields",
    status: "applied",
    available_sections: ["fields"],
    error_code: undefined,
  },
  {
    ...trace,
    id: "trace-construction",
    sequence: 5,
    stage: "construction",
    operation_id: "operation-construction",
    provider: "platform",
    status: "queued",
    snapshot_id: "snapshot-fixture",
    available_sections: ["fields"],
    error_code: undefined,
  },
];
export const contents: Record<string, unknown> = {
  request: {
    model: "model-fixture",
    messages: [{ role: "user", content: "[CONTEÚDO PROTEGIDO]" }],
    field_ids: ["intent", "auth_contract"],
  },
  response: {
    output: [{ field_id: "intent", value: "[CONTEÚDO PROTEGIDO]" }],
    usage: { input_tokens: 42, output_tokens: 24 },
  },
  normalized: {
    validation: "failed",
    fallback: "not_applied",
    error_code: "MODEL_OUTPUT_INVALID",
  },
  fields: {
    items: [
      {
        attachment_id: "auth",
        field_id: "intent",
        original_value: null,
        suggested_value: "[CONTEÚDO PROTEGIDO]",
        final_value: "[CONTEÚDO PROTEGIDO]",
        source_type: "request",
        state: "applied",
        attachment_revision: 4,
      },
      {
        attachment_id: "auth",
        field_id: "auth_contract",
        original_value: "[CONTEÚDO PROTEGIDO]",
        suggested_value: "[CONTEÚDO PROTEGIDO]",
        edited_value: "[CONTEÚDO PROTEGIDO]",
        final_value: "[CONTEÚDO PROTEGIDO]",
        source_type: "user_answer",
        state: "user_edited",
        attachment_revision: 5,
        snapshot_id: "snapshot-fixture",
      },
    ],
  },
};
export const capturePolicy = {
  version: 3,
  settings: {
    capture_enabled: false,
    retention_days: 7,
    max_payload_bytes: 32768,
    max_operation_bytes: 262144,
    max_total_bytes: 16777216,
    allowed_categories: [
      "technical_metadata",
      "schemas",
      "field_identifiers",
      "usage",
    ],
  },
  can_inspect_payloads: true,
  updated_at: "2026-10-28T12:00:00Z",
};
export const accessPolicy = {
  version: 2,
  admins: [
    {
      id: "admin-fixture",
      name: "Operador de teste",
      login: "operator-fixture",
      allowed: false,
    },
    {
      id: "another-admin",
      name: "Outro administrador",
      login: "another-fixture",
      allowed: false,
    },
  ],
  can_inspect_payloads: false,
};
export const preparationPolicy = {
  version: 7,
  settings: {
    enabled: true,
    model_id: null as string | null,
    max_context_bytes: 32000,
    max_output_tokens: 4000,
    max_repairs: 0,
  },
  available_models: [
    {
      id: "model-ready",
      connection_id: "connection-fixture",
      provider: "openai",
      model: "model-fixture",
      name: "Modelo autorizado",
      ready: true,
    },
    {
      id: "model-disabled",
      connection_id: "connection-other",
      provider: "gemini",
      model: "model-unavailable",
      name: "Modelo indisponível",
      ready: false,
    },
  ],
  updated_at: "2026-10-28T12:00:00Z",
};
export function payload(section: string, id = trace.id): TracePayload {
  return {
    trace_id: id,
    section: section as TracePayload["section"],
    payload: contents[section],
    capture_state: "protected",
    redacted_paths: [section === "request" ? "messages[0].content" : "output"],
    truncated_paths: [],
    policy_version: 3,
    expires_at: trace.expires_at || null,
  };
}
export async function monitorMocks(
  page: Page,
  canInspect = true,
  selected: TraceMetadata = trace,
) {
  const calls: Request[] = [];
  await page.route("**/admin-api/v1/**", async (route) => {
    const request = route.request();
    calls.push(request);
    const url = new URL(request.url());
    const path = url.pathname.replace("/admin-api/v1", "");
    if (path === "/observability/events") return route.fulfill({ json: feed });
    if (path === "/observability/traces")
      return route.fulfill({
        json: { ...feed, items: [selected], can_inspect_payloads: canInspect },
      });
    if (path.endsWith("/timeline"))
      return route.fulfill({
        json: { items: timeline, can_inspect_payloads: canInspect },
      });
    if (path.endsWith("/payload"))
      return route.fulfill({
        json: payload(url.searchParams.get("section") || "request"),
      });
    if (path.endsWith("/copy"))
      return route.fulfill({ json: payload(request.postDataJSON().section) });
    if (path === "/observability/policy")
      return route.fulfill({ json: capturePolicy });
    if (path === "/observability/payload-access")
      return route.fulfill({ json: accessPolicy });
    if (path === "/template-preparation-policy")
      return route.fulfill({ json: preparationPolicy });
    if (path === "/ai/configuration-schema")
      return route.fulfill({
        json: {
          model: {
            schema: { properties: {} },
            defaults: {},
            execution_schema: { properties: {} },
          },
          construction: { schema: { properties: {} }, defaults: {} },
          preparation: {
            schema: {
              properties: {
                max_context_bytes: {
                  type: "integer",
                  minimum: 4000,
                  maximum: 60000,
                },
                max_output_tokens: {
                  type: "integer",
                  minimum: 256,
                  maximum: 65536,
                },
                max_repairs: { type: "integer", minimum: 0, maximum: 3 },
              },
            },
            defaults: preparationPolicy.settings,
          },
          available_models: preparationPolicy.available_models.map((model) => ({
            ...model,
            max_output_tokens: 16000,
          })),
          provider_capabilities: {},
        },
      });
    return route.fulfill({
      status: 404,
      json: { error: { message: "Rota não prevista no teste." } },
    });
  });
  return calls;
}
export async function openTrace(page: Page) {
  await page.goto("/tests/observability.fixture.html");
  await page
    .getByRole("tab", { name: "Integrações e payloads", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Ver detalhes da integração" })
    .click();
}
