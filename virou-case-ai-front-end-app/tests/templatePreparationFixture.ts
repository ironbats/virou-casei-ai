import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type {
  Attachment,
  BuildDraft,
  FieldValue,
  Template,
} from "../src/features/guidedTypes";
import {
  attachmentIssues,
  renderTemplate,
  sameField,
} from "../src/features/templateFields";

export const preparationProject = "template-preparation-project";
export const preparationRevision = "template-preparation-revision";
export const authTemplate: Template = {
  id: "autorizacao",
  version_id: "auth-v1",
  version: 1,
  title: "Autenticação, autorização e isolamento",
  category: "security",
  summary: "Defina a interação e preserve os comportamentos de acesso.",
  available: true,
  content:
    "Interação: {{interaction}}\nSessão expirada: {{field_8}}\nRecurso inexistente: {{field_9}}",
  fields: [
    {
      key: "interaction",
      id: "interaction",
      label: "Interação desejada",
      description: "Use o resultado descrito no seu pedido.",
      required: true,
      max_length: 1000,
      type: "text",
    },
    {
      key: "field_8",
      id: "field_8",
      semantic_id: "expired_session",
      label: "Resultado para sessão expirada",
      required: true,
      max_length: 1000,
      type: "text",
    },
    {
      key: "field_9",
      id: "field_9",
      semantic_id: "missing_resource",
      label: "Resultado para recurso inexistente",
      required: true,
      max_length: 1000,
      type: "text",
    },
  ],
};
export const manualTemplate: Template = {
  id: "interacao",
  version_id: "interaction-v1",
  version: 1,
  title: "Interação e acesso",
  category: "quality",
  summary: "Revise o comportamento com tipos definidos.",
  available: true,
  content:
    "Modal: {{modal}}\nEntrada: {{entry}}\nCritérios: {{criteria}}\nTexto literal: {{example_literal}}",
  fields: [
    {
      key: "modal",
      label: "Abrir modal?",
      required: true,
      max_length: 50,
      type: "boolean",
    },
    {
      key: "entry",
      label: "Onde está o botão?",
      required: true,
      max_length: 50,
      type: "enum",
      constraints: { enum: ["Página inicial", "Menu"] },
    },
    {
      key: "criteria",
      label: "Critérios de verificação",
      required: true,
      max_length: 1000,
      type: "string_list",
      constraints: { min_items: 1, max_items: 5 },
    },
  ],
};

export async function preparationWorkspace(
  page: Page,
  stage: BuildDraft["stage"] = "idea",
) {
  const state = {
    version: 1,
    contextRevision: preparationRevision as string | null,
    draft: {
      stage,
      objective: "Adicionar botão de login na home que abre um modal.",
      message_text: "Adicionar botão de login na home que abre um modal.",
      attachments: [] as Attachment[],
      recommendation_id: stage === "idea" ? null : "recommendation-1",
      recommendation_status: stage === "idea" ? null : "ready",
      context_revision: stage === "idea" ? null : preparationRevision,
      context_hash: stage === "idea" ? undefined : "saved-context",
      templates_completed: false,
      review_snapshot: null,
      can_review: stage === "templates",
      can_build: false,
      blocking_reasons: [],
    } as BuildDraft,
    projectDraft: {
      base_revision_id: preparationRevision,
      files: null as Record<string, string> | null,
      prompt: "",
    },
    catalog: [structuredClone(authTemplate), structuredClone(manualTemplate)],
    generation: [] as Record<string, unknown>[],
    requests: [] as { path: string; body: any; key: string; csrf: string }[],
    submitted: [] as BuildDraft[],
    fills: [] as { id: string; ids: string[]; objective: string }[],
    autoFill: true,
    fillDelay: 40,
    recommendationFails: false,
    listFailures: 0,
    loseNextSave: false,
    sendError: null as null | {
      attachment_id: string;
      field_id: string;
      code: string;
      message: string;
      action: string;
    },
    enabled: true,
  };
  const output: Record<string, FieldValue> = {
    interaction:
      "Adicionar botão de login na página inicial para abrir o modal.",
    field_8: "Solicitar autenticação novamente ao expirar a sessão.",
    field_9: "Exibir mensagem de recurso indisponível sem revelar dados.",
    modal: true,
    entry: "Página inicial",
    criteria: ["Botão abre o modal", "Fechar retorna à página inicial"],
  };
  const templateFor = (id: string) =>
    state.catalog.find((template) => template.id === id)!;
  const digest = (value: unknown) =>
    createHash("sha256").update(JSON.stringify(value)).digest("hex");
  function metadata() {
    const selected = state.draft.attachments.filter((item) => item.enabled);
    const prepared =
      !!state.draft.recommendation_status && !!state.draft.context_hash;
    const stale =
      prepared && state.draft.context_revision !== state.contextRevision;
    state.draft.blocking_reasons = stale
      ? [
          {
            code: "CONTEXT_STALE",
            message: "A revisão salva mudou. Atualize a preparação.",
          },
        ]
      : [];
    for (const item of selected)
      item.validation_errors = attachmentIssues(
        { ...item, validation_errors: [] },
        templateFor(item.template_id),
        12000,
      );
    state.draft.can_review =
      prepared &&
      !stale &&
      selected.every(
        (item) =>
          !["queued", "filling", "reconciling", "stale"].includes(
            item.fill_state || "",
          ) && !item.validation_errors?.length,
      );
    state.draft.can_build =
      !!state.draft.review_snapshot &&
      !!state.draft.templates_completed &&
      !!state.draft.can_review;
    return structuredClone(state.draft);
  }
  function ready(fill = state.fills.at(-1)) {
    if (!fill || fill.objective !== state.draft.objective) return;
    for (const id of fill.ids) {
      const item = state.draft.attachments.find(
        (attachment) => attachment.template_id === id && attachment.enabled,
      );
      if (!item) continue;
      for (const field of templateFor(id).fields) {
        if (item.field_values?.[field.key]?.edited_by_user) continue;
        item.fields[field.key] = output[field.key];
        item.field_values = {
          ...item.field_values,
          [field.key]: {
            field_id: field.key,
            value: output[field.key],
            state: "applied",
            source_type:
              field.key === "interaction" ? "request" : "policy_suggestion",
            source_refs: ["request:objective"],
            edited_by_user: false,
            updated_at: "2026-10-28T12:01:00Z",
          },
        };
      }
      item.edited_content = renderTemplate(templateFor(id), item.fields);
      item.fill_state = "ready";
      item.attachment_revision = (item.attachment_revision || 0) + 1;
    }
    state.version++;
    metadata();
  }
  function schedule(ids: string[]) {
    if (!ids.length) return;
    const fill = {
      id: `fill-${state.fills.length + 1}`,
      ids,
      objective: state.draft.objective,
    };
    state.fills.push(fill);
    for (const id of ids) {
      const item = state.draft.attachments.find(
        (attachment) => attachment.template_id === id,
      )!;
      item.fill_state = "queued";
      item.preparation_id = fill.id;
    }
    if (state.autoFill) setTimeout(() => ready(fill), state.fillDelay);
  }
  const recommendation = () => ({
    recommendation_id: "recommendation-1",
    objective_hash: digest(state.draft.objective),
    source: state.recommendationFails ? "fallback" : "jev",
    status: state.recommendationFails ? "fallback" : "ready",
    items: state.recommendationFails
      ? []
      : [{ template_id: authTemplate.id, version_id: authTemplate.version_id }],
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const method = request.method();
    const body = method === "GET" ? null : request.postDataJSON();
    if (method !== "GET")
      state.requests.push({
        path,
        body,
        key: request.headers()["idempotency-key"] || "",
        csrf: request.headers()["x-csrf-token"] || "",
      });
    const fulfill = (json: unknown, status = 200) =>
      route.fulfill({ json, status });
    const fail = (
      code: string,
      message: string,
      status = 412,
      fields?: unknown[],
    ) =>
      fulfill(
        {
          error: { code, message, ...(fields ? { details: { fields } } : {}) },
        },
        status,
      );
    if (path === "/me")
      return fulfill({
        id: "template-student",
        name: "Aluno",
        login: "template-student",
        role: "student",
        csrf_token: "template-csrf",
        access: {
          status: "active",
          allowed_actions: [
            "read",
            "write",
            "generate",
            "learn",
            "publish",
            "export",
          ],
          period_id: "template-period",
        },
      });
    if (path === "/wallet")
      return fulfill({
        available_ratio: 1,
        reserved_ratio: 0,
        consumed_ratio: 0,
        can_start_operation: true,
        pending_reconciliation: false,
        usage_status: "available",
      });
    if (path === "/capabilities")
      return fulfill({
        policy_version: "template-policy",
        generation: { enabled: true },
        template_preparation: { enabled: state.enabled },
        publication: { enabled: false },
        preview: { enabled: true },
        limits: {
          max_projects: 2,
          max_files: 100,
          max_file_bytes: 100000,
          max_project_bytes: 1000000,
          max_path_length: 200,
          max_prompt_chars: 12000,
          max_revisions: 100,
        },
        guided_build: {
          max_objective_chars: 12000,
          max_message_chars: 12000,
          max_attachments: 8,
          max_attachment_chars: 12000,
          max_context_bytes: 60000,
          autosave_ms: 100,
          selection_debounce_ms: 100,
        },
      });
    if (path === "/templates") return fulfill({ items: state.catalog });
    if (path.startsWith("/templates/versions/"))
      return fulfill(
        state.catalog.find(
          (template) => template.version_id === path.split("/").pop(),
        ) || authTemplate,
      );
    const project = {
      id: preparationProject,
      name: "Sistema de padaria",
      objective: state.draft.objective,
      current_revision_id: state.contextRevision,
      version: 1,
      stack: "hono-typescript-d1",
      state: "active",
      created_at: "2026-10-28T12:00:00Z",
    };
    if (path === `/projects/${preparationProject}`) return fulfill(project);
    if (path === `/projects/${preparationProject}/draft`) {
      if (method === "PUT") state.projectDraft = body;
      return fulfill({
        version: 1,
        updated_at: null,
        data: state.projectDraft,
      });
    }
    if (path === `/projects/${preparationProject}/build-draft`) {
      if (method === "GET" && state.listFailures > 0) {
        state.listFailures--;
        return fail("UNAVAILABLE", "Não foi possível consultar agora.", 503);
      }
      if (method === "PUT") {
        const version = Number(
          (request.headers()["if-match"] || "").replaceAll('"', ""),
        );
        if (version !== state.version)
          return fail("DRAFT_CONFLICT", "O rascunho mudou em outra sessão.");
        const old = state.draft;
        const changedContext =
          old.objective !== body.objective ||
          old.message_text !== body.message_text;
        const changedSelection =
          JSON.stringify(
            old.attachments.map((item) => [
              item.template_id,
              item.enabled,
              item.fields,
              item.edited_content,
            ]),
          ) !==
          JSON.stringify(
            body.attachments.map((item: Attachment) => [
              item.template_id,
              item.enabled,
              item.fields,
              item.edited_content,
            ]),
          );
        state.draft = {
          ...old,
          ...body,
          attachments: body.attachments.map((item: Attachment) => {
            const template = templateFor(item.template_id);
            const previous = old.attachments.find(
              (a) => a.template_id === item.template_id,
            );
            return {
              ...previous,
              ...item,
              title: template.title,
              version: template.version,
              category: template.category,
              attachment_revision: (previous?.attachment_revision || 0) + 1,
              schema_version: 1,
              field_values: Object.fromEntries(
                Object.entries(item.fields).map(([key, value]) => [
                  key,
                  previous?.field_values?.[key] &&
                  sameField(previous.fields[key], value)
                    ? previous.field_values[key]
                    : {
                        field_id: key,
                        value,
                        state: "user_edited",
                        source_type: "manual",
                        source_refs: [],
                        edited_by_user: true,
                        updated_at: "2026-10-28T12:00:00Z",
                      },
                ]),
              ),
            };
          }),
        };
        if (changedContext || changedSelection) {
          state.draft.templates_completed = false;
          state.draft.review_snapshot = null;
        }
        if (changedContext) {
          state.draft.recommendation_status = null;
          state.draft.context_hash = undefined;
          state.draft.attachments.forEach((item) => {
            item.fill_state = "stale";
          });
        }
        if (!changedContext && state.draft.recommendation_status)
          schedule(
            state.draft.attachments
              .filter(
                (item) =>
                  item.enabled &&
                  !old.attachments.find(
                    (previous) =>
                      previous.template_id === item.template_id &&
                      previous.enabled,
                  ) &&
                  !Object.keys(item.fields).length,
              )
              .map((item) => item.template_id),
          );
        state.version++;
        if (state.loseNextSave) {
          state.loseNextSave = false;
          return route.abort("failed");
        }
      }
      return fulfill({
        version: state.version,
        updated_at: "2026-10-28T12:00:00Z",
        data: metadata(),
      });
    }
    if (path.endsWith("/recommendations") && method === "POST") {
      if (body.base_project_revision !== state.contextRevision)
        return fail(
          "CONTEXT_STALE",
          "A revisão salva mudou. Atualize o contexto antes de preparar.",
          409,
        );
      if (body.expected_draft_revision !== state.version)
        return fail("DRAFT_CONFLICT", "O pedido mudou.");
      state.draft.stage = "templates";
      state.draft.recommendation_id = "recommendation-1";
      state.draft.recommendation_status = state.recommendationFails
        ? "fallback"
        : "ready";
      state.draft.context_revision = state.contextRevision;
      state.draft.context_hash = "saved-context";
      state.version++;
      schedule(
        state.draft.attachments
          .filter((item) => item.enabled && item.fill_state === "stale")
          .map((item) => item.template_id),
      );
      return fulfill(recommendation());
    }
    if (path.includes("/recommendations/")) return fulfill(recommendation());
    if (path.endsWith("/build-fill") && method === "POST") {
      schedule(body.attachment_ids);
      state.version++;
      return fulfill({ version: state.version, data: metadata() });
    }
    if (path.endsWith("/build-review") && method === "POST") {
      metadata();
      if (body.expected_draft_revision !== state.version)
        return fail("DRAFT_CONFLICT", "O pedido mudou.");
      if (!state.draft.can_review)
        return fail(
          "TEMPLATES_STAGE_INCOMPLETE",
          "Conclua a preparação antes de revisar.",
        );
      const selected = state.draft.attachments.filter((item) => item.enabled);
      if (!selected.length && !body.confirm_empty)
        return fail("EMPTY_SELECTION_UNCONFIRMED", "Confirme a seleção vazia.");
      state.draft.stage = "review";
      state.draft.templates_completed = true;
      const snapshot = {
        draft_revision: state.version + 1,
        context_revision: state.draft.context_revision ?? null,
        context_hash: "saved-context",
        objective: state.draft.objective,
        message_text: state.draft.message_text,
        attachments: structuredClone(selected),
        empty_selection_confirmed: !selected.length,
        reviewed_at: "2026-10-28T12:03:00Z",
      };
      state.draft.review_snapshot = { ...snapshot, hash: digest(snapshot) };
      state.version++;
      return fulfill({ version: state.version, data: metadata() });
    }
    if (path.endsWith("/messages") && method === "POST") {
      if (state.sendError)
        return fail(state.sendError.code, state.sendError.message, 400, [
          state.sendError,
        ]);
      if (
        body.expected_draft_revision !== state.version ||
        body.review_snapshot_hash !== state.draft.review_snapshot?.hash ||
        !state.draft.can_build
      )
        return fail(
          "REVIEW_STALE",
          "Revise o pedido atual antes de construir.",
        );
      state.submitted.push(structuredClone(state.draft));
      const operation = {
        id: "generation-1",
        type: "generation",
        project_id: preparationProject,
        state: "queued",
        progress_stage: "queued",
        progress_message: "Pedido recebido",
        billing_status: "reserved",
        pending_reconciliation: false,
        cancel_requested: false,
        error_code: null,
        request_text: state.draft.message_text,
        attachment_snapshots: state.draft.review_snapshot!.attachments.map(
          (item) => ({
            id: item.template_id,
            title: item.title,
            version: item.version,
            content: item.edited_content,
          }),
        ),
        created_at: "2026-10-28T12:04:00Z",
        result_ref: {},
      };
      state.generation.push(operation);
      state.draft = {
        stage: "idea",
        objective: "",
        message_text: "",
        attachments: [],
        recommendation_id: null,
        recommendation_status: null,
        context_hash: undefined,
        context_revision: null,
        templates_completed: false,
        review_snapshot: null,
        can_build: false,
      };
      state.version++;
      return fulfill(operation, 202);
    }
    if (path === "/operations")
      return fulfill({
        items:
          new URL(request.url()).searchParams.get("kind") === "publication"
            ? []
            : state.generation,
        next_cursor: null,
      });
    if (path.startsWith("/operations/"))
      return fulfill(
        state.generation.find(
          (operation) => operation.id === path.split("/").pop(),
        ),
      );
    if (path.endsWith("/publication-readiness"))
      return fulfill({
        can_publish: false,
        revision_id: state.contextRevision,
        reasons: [
          {
            code: "CLOUDFLARE_NOT_CONFIGURED",
            message: "Configuração da hospedagem pendente",
          },
        ],
        publication: null,
      });
    if (
      path ===
      `/projects/${preparationProject}/revisions/${preparationRevision}`
    )
      return fulfill({
        id: preparationRevision,
        project_id: preparationProject,
        origin: "seed",
        candidate: false,
        created_at: "2026-10-28T12:00:00Z",
        files: [{ id: "index-file", path: "public/index.html", size: 100 }],
      });
    if (path.endsWith(`/revisions/${preparationRevision}/files/index-file`))
      return fulfill({
        path: "public/index.html",
        content:
          '<!doctype html><html lang="pt-BR"><head><title>Padaria</title></head><body><h1>Padaria</h1></body></html>',
      });
    if (
      state.contextRevision &&
      path.endsWith(`/revisions/${state.contextRevision}`)
    )
      return fulfill({
        id: state.contextRevision,
        project_id: preparationProject,
        files: [],
        origin: "saved",
        candidate: false,
        created_at: "2026-10-28T12:00:00Z",
      });
    if (path.endsWith("/revisions"))
      return fulfill({ items: [], next_cursor: null });
    if (path === "/projects")
      return fulfill({ items: [project], next_cursor: null });
    return fail("NOT_FOUND", `Rota de teste ausente: ${path}`, 404);
  });
  await page.goto(`/app/projetos/${preparationProject}`);
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      `Etapa ${stage === "idea" || stage === "conversation" ? 1 : stage === "templates" ? 2 : 3} de 3`,
    ),
  ).toBeVisible();
  return { state, ready, output, metadata };
}
