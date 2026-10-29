import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Operation } from "../src/shared/types";

const projectId = "project-publication-regression";
const revisionId = "revision-saved-hono";
const publicationUrl = "https://publication-regression.example.workers.dev";
const pendingReason = {
  code: "CAPABILITY_UNVALIDATED",
  message:
    "A publicação aguarda a verificação operacional na conexão Cloudflare do backoffice.",
};

async function publication(
  page: Page,
  editor = false,
  allowedActions = ["read", "write", "publish", "export"],
) {
  const state = {
    reasons: [pendingReason],
    readinessFailures: 0,
    operationFailures: 0,
    submissionFailures: 0,
    submissionStatus: 503,
    lostSubmissionResponses: 0,
    submissionDelay: 0,
    draftFailures: 0,
    operationRequests: [] as string[],
    buildDraft: {
      stage: "idea",
      objective: "Publicar a revisão validada",
      message_text: "",
      attachments: [],
      recommendation_id: null,
    },
    draft: {
      base_revision_id: revisionId,
      files: null as Record<string, string> | null,
      prompt: "",
    },
    draftWrites: [] as string[],
    readinessRequests: [] as string[],
    operations: [] as Operation[],
    submissions: [] as {
      body: unknown;
      csrf: string;
      idempotency: string;
    }[],
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    if (path === "/me")
      return route.fulfill({
        json: {
          id: "student-publication-regression",
          name: "Aluno de teste",
          login: "publication-regression",
          role: "student",
          csrf_token: "publication-regression-csrf",
          access: {
            status: "active",
            allowed_actions: allowedActions,
            period_id: "period-publication-regression",
          },
        },
      });
    if (path === "/wallet")
      return route.fulfill({
        json: {
          available_ratio: 1,
          reserved_ratio: 0,
          consumed_ratio: 0,
          can_start_operation: true,
          pending_reconciliation: false,
          usage_status: "available",
        },
      });
    if (path === `/projects/${projectId}`)
      return route.fulfill({
        json: {
          id: projectId,
          name: "Projeto Hono verificado",
          objective: "Publicar a revisão validada",
          stack: "hono-typescript-d1",
          version: 1,
          current_revision_id: revisionId,
          state: "active",
          created_at: "2026-10-28T11:00:00Z",
        },
      });
    if (path === "/capabilities")
      return route.fulfill({
        json: {
          guided_build: {
            max_objective_chars: 12000,
            max_message_chars: 12000,
            max_attachments: 10,
            max_attachment_chars: 12000,
            max_context_bytes: 100000,
            autosave_ms: 700,
          },
          limits: {
            max_projects: 10,
            max_files: 100,
            max_file_bytes: 100000,
            max_project_bytes: 1000000,
            max_path_length: 200,
            max_prompt_chars: 12000,
            max_revisions: 100,
          },
          policy_version: "test",
          generation: { enabled: true },
          publication: { enabled: true },
          preview: { enabled: true },
        },
      });
    if (path === "/templates") return route.fulfill({ json: { items: [] } });
    if (
      path === `/projects/${projectId}/draft` ||
      path === `/projects/${projectId}/build-draft`
    ) {
      const key = path.endsWith("/build-draft") ? "buildDraft" : "draft";
      if (request.method() === "PUT") {
        state.draftWrites.push(key);
        if (state.draftFailures > 0) {
          state.draftFailures--;
          return route.fulfill({
            status: 503,
            json: {
              error: {
                code: "DRAFT_UNAVAILABLE",
                message: "Não foi possível salvar o rascunho agora.",
              },
            },
          });
        }
        state[key] = request.postDataJSON();
      }
      return route.fulfill({
        json: {
          version: 1 + state.draftWrites.length,
          updated_at: "2026-10-28T11:00:00Z",
          data: state[key],
        },
      });
    }
    if (path === `/projects/${projectId}/revisions/${revisionId}`)
      return route.fulfill({
        json: {
          id: revisionId,
          project_id: projectId,
          origin: "manual",
          candidate: false,
          created_at: "2026-10-28T11:00:00Z",
          files: [{ id: "index-file", path: "public/index.html", size: 80 }],
        },
      });
    if (
      path === `/projects/${projectId}/revisions/${revisionId}/files/index-file`
    )
      return route.fulfill({
        json: {
          path: "public/index.html",
          content:
            "<!doctype html><html><body><h1>Meu projeto</h1></body></html>",
        },
      });
    if (path === "/operations" || path.startsWith("/operations/")) {
      state.operationRequests.push(url.pathname + url.search);
      if (state.operationFailures > 0) {
        state.operationFailures--;
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "STATUS_UNAVAILABLE",
              message:
                "A consulta do status está temporariamente indisponível.",
            },
          },
        });
      }
      if (path.startsWith("/operations/")) {
        const operation = state.operations.find(
          (item) => item.id === path.split("/").at(-1),
        );
        return operation
          ? route.fulfill({ json: operation })
          : route.fulfill({
              status: 404,
              json: {
                error: {
                  code: "NOT_FOUND",
                  message: "Publicação não encontrada.",
                },
              },
            });
      }
      return route.fulfill({
        json: {
          items:
            url.searchParams.get("kind") === "generation"
              ? []
              : state.operations,
          next_cursor: null,
        },
      });
    }
    if (path === `/projects/${projectId}/publication-readiness`) {
      state.readinessRequests.push(url.searchParams.get("revision_id") ?? "");
      if (state.readinessFailures > 0) {
        state.readinessFailures--;
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "UNAVAILABLE",
              message: "A verificação está temporariamente indisponível.",
            },
          },
        });
      }
      const completed = state.operations.find((op) => op.state === "succeeded");
      return route.fulfill({
        json: {
          can_publish: state.reasons.length === 0,
          revision_id: revisionId,
          configuration_version: 3,
          reasons: state.reasons,
          publication: completed
            ? {
                url: completed.result_ref.url,
                revision_id: completed.result_ref.revision_id ?? revisionId,
                published_at: completed.result_ref.verified_at,
              }
            : null,
        },
      });
    }
    if (
      path === `/projects/${projectId}/publications` &&
      request.method() === "POST"
    ) {
      state.submissions.push({
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
        idempotency: request.headers()["idempotency-key"],
      });
      if (state.submissionFailures > 0) {
        state.submissionFailures--;
        return route.fulfill({
          status: state.submissionStatus,
          json: {
            error: {
              code:
                state.submissionStatus === 403
                  ? "ACCESS_EXPIRED"
                  : "SUBMISSION_UNAVAILABLE",
              message:
                state.submissionStatus === 403
                  ? "Seu acesso não permite novas publicações."
                  : "O envio não pôde ser confirmado. Consulte o status antes de tentar novamente.",
            },
          },
        });
      }
      const operation: Operation = {
        id: `publication-operation-${state.submissions.length}`,
        type: "publication",
        project_id: projectId,
        state: "queued",
        progress_stage: "queued",
        progress_message: "Publicação aguardando execução.",
        billing_status: "not_applicable",
        pending_reconciliation: false,
        cancel_requested: false,
        error_code: null,
        created_at: "2026-10-28T11:00:00Z",
        updated_at: "2026-10-28T11:00:00Z",
        result_ref: {},
      };
      state.operations = [operation, ...state.operations];
      state.reasons = [
        {
          code: "OPERATION_CONFLICT",
          message:
            "Há uma publicação em andamento ou em verificação neste projeto.",
        },
      ];
      if (state.lostSubmissionResponses > 0) {
        state.lostSubmissionResponses--;
        return route.abort("failed");
      }
      if (state.submissionDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, state.submissionDelay),
        );
      return route.fulfill({ status: 202, json: operation });
    }
    await route.fulfill({
      status: 404,
      json: { error: { code: "NOT_FOUND" } },
    });
  });
  await page.goto(`/app/projetos/${projectId}${editor ? "" : "/publicar"}`);
  if (editor)
    await expect(
      page.getByRole("tree", { name: "Arquivos do projeto" }),
    ).toBeVisible();
  else
    await expect(
      page.getByRole("heading", { name: "Publicação do projeto", exact: true }),
    ).toBeVisible();
  await expect(page.locator("#publication-reason")).toHaveText(
    pendingReason.message,
  );
  return state;
}

test("libera após qualificação da stack, publica a revisão salva e acompanha o resultado", async ({
  page,
}) => {
  const state = await publication(page);
  const publish = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  await expect(publish).toBeDisabled();
  await expect(page.locator("#publication-reason")).toHaveText(
    pendingReason.message,
  );
  expect(state.submissions).toHaveLength(0);

  state.reasons = [];
  await page.getByRole("button", { name: "Verificar novamente" }).click();
  await expect(publish).toBeEnabled();
  expect(
    state.readinessRequests.every((revision) => revision === revisionId),
  ).toBe(true);
  await publish.click();
  await expect(page).toHaveURL(
    `/app/projetos/${projectId}/publicar?operation=${state.operations[0].id}`,
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(publish).toBeDisabled();
  await expect(page.locator("#publication-reason")).toContainText(
    "publicação em andamento",
  );
  expect(state.submissions).toHaveLength(1);
  expect(state.submissions[0]).toEqual({
    body: { revision_id: revisionId },
    csrf: "publication-regression-csrf",
    idempotency: expect.any(String),
  });
  expect(state.submissions[0].idempotency).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );

  state.reasons = [];
  state.operations = [
    {
      ...state.operations[0],
      state: "succeeded",
      progress_stage: "succeeded",
      progress_message: "Publicação confirmada.",
      updated_at: "2026-10-28T11:01:00Z",
      result_ref: {
        revision_id: revisionId,
        url: publicationUrl,
        verified_at: "2026-10-28T11:01:00Z",
      },
    },
  ];
  await expect(
    page.getByRole("link", { name: "Abrir publicação" }),
  ).toHaveAttribute("href", publicationUrl + "/", { timeout: 10000 });
  await expect(
    page.getByText("Publicação confirmada.", { exact: true }),
  ).toBeVisible();
  await expect(publish).toBeEnabled();
  expect(state.submissions).toHaveLength(1);
});

test("atualiza automaticamente a disponibilidade sem recarregar o projeto", async ({
  page,
}) => {
  await page.clock.install();
  const state = await publication(page);
  const publish = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  await expect(page.locator("#publication-reason")).toHaveText(
    pendingReason.message,
  );
  await expect(publish).toBeDisabled();
  const before = state.readinessRequests.length;
  state.reasons = [];
  await page.clock.fastForward(15000);
  await expect(publish).toBeEnabled();
  expect(state.readinessRequests.length).toBeGreaterThan(before);
  expect(state.submissions).toHaveLength(0);
});

test("mantém bloqueio quando a stack ou o acesso não estão autorizados", async ({
  page,
}) => {
  const state = await publication(page, false, ["read"]);
  const publish = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  for (const reason of [
    {
      code: "UNSUPPORTED_STACK",
      message: "Este perfil ainda não foi habilitado para publicação.",
    },
    {
      code: "ACCESS_EXPIRED",
      message:
        "Seu acesso não permite novas publicações. Fale com o responsável.",
    },
  ]) {
    state.reasons = [reason];
    await page.getByRole("button", { name: "Verificar novamente" }).click();
    await expect(page.locator("#publication-reason")).toHaveText(
      reason.message,
    );
    await expect(publish).toBeDisabled();
  }
  state.reasons = [];
  await page.getByRole("button", { name: "Verificar novamente" }).click();
  await expect(publish).toBeEnabled();
  expect(state.submissions).toHaveLength(0);
  state.submissionFailures = 1;
  state.submissionStatus = 403;
  state.reasons = [
    {
      code: "ACCESS_EXPIRED",
      message: "Seu acesso não permite novas publicações.",
    },
  ];
  await publish.click();
  await expect(page.getByRole("alert")).toContainText(
    "Seu acesso não permite novas publicações.",
  );
  await expect(publish).toBeDisabled();
  expect(state.operations).toHaveLength(0);
  expect(state.submissions).toHaveLength(1);
});

test("recupera falha de consulta sem liberar publicação antes da resposta válida", async ({
  page,
}) => {
  const state = await publication(page);
  const publish = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  await expect(page.locator("#publication-reason")).toHaveText(
    pendingReason.message,
  );
  state.readinessFailures = 1;
  await page.getByRole("button", { name: "Verificar novamente" }).click();
  await expect(page.locator("#publication-reason")).toHaveText(
    "Não foi possível verificar a publicação.",
  );
  await expect(publish).toBeDisabled();
  expect(state.submissions).toHaveLength(0);
  state.reasons = [];
  await page.getByRole("button", { name: "Verificar novamente" }).click();
  await expect(publish).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

function setStage(
  state: Awaited<ReturnType<typeof publication>>,
  stage: string,
  status = "running",
) {
  const previous = state.operations[0];
  const at = new Date(
    Date.parse(previous.updated_at ?? previous.created_at) + 1000,
  ).toISOString();
  state.operations[0] = {
    ...previous,
    state: status,
    progress_stage: stage,
    progress_message:
      stage === "publication_building"
        ? "Preparando o artefato no executor isolado."
        : stage === "publication_verifying"
          ? "Verificando frontend, API e banco."
          : stage === "publication_reconciling"
            ? "Verificando resultado da publicação. Não repita o envio."
            : "Não foi possível concluir a publicação.",
    updated_at: at,
    activity: [...(previous.activity ?? []), { stage, at, message: stage }],
  };
}

async function startPublication(
  page: Page,
  state: Awaited<ReturnType<typeof publication>>,
) {
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
}

test("editor abre a tela com dois cliques para publicar e preserva o pedido ainda em edição", async ({
  page,
}) => {
  const state = await publication(page, true);
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  await page
    .getByLabel("O que vamos fazer?")
    .fill("Meu próximo pedido precisa continuar salvo");
  await page.getByRole("button", { name: "Publicar", exact: true }).click();
  await expect(page).toHaveURL(`/app/projetos/${projectId}/publicar`);
  await expect(
    page.getByRole("heading", { name: "Publicação do projeto", exact: true }),
  ).toBeFocused();
  expect(state.buildDraft.message_text).toBe(
    "Meu próximo pedido precisa continuar salvo",
  );
  expect(state.submissions).toHaveLength(0);
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  expect(state.submissions).toHaveLength(1);
  await page
    .getByRole("link", { name: "Voltar ao projeto", exact: true })
    .click();
  await expect(page.getByLabel("O que vamos fazer?")).toHaveValue(
    "Meu próximo pedido precisa continuar salvo",
  );
  await page
    .getByRole("button", { name: "Acompanhar publicação", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  expect(state.submissions).toHaveLength(1);
});

test("falha ao salvar rascunho mantém editor e conteúdo sem publicar", async ({
  page,
}) => {
  const state = await publication(page, true);
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  state.draftFailures = 10;
  await page
    .getByLabel("O que vamos fazer?")
    .fill("Conteúdo que não pode ser perdido");
  await page.getByRole("button", { name: "Publicar", exact: true }).click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Não foi possível salvar o rascunho agora." })
      .first(),
  ).toBeVisible();
  await expect(page).toHaveURL(`/app/projetos/${projectId}`);
  await expect(page.getByLabel("O que vamos fazer?")).toHaveValue(
    "Conteúdo que não pode ser perdido",
  );
  expect(state.submissions).toHaveLength(0);
});

test("rascunho de código bloqueia envio e pode acompanhar sem perder alterações", async ({
  page,
}) => {
  const state = await publication(page, true);
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  const content =
    "<!doctype html><html><body><h1>Minha alteração preservada</h1></body></html>";
  await page.locator(".code-editor").fill(content);
  await expect(
    page.getByRole("button", { name: "Publicar", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("#publication-reason")).toContainText(
    "Salve as alterações",
  );
  await page
    .getByRole("button", { name: "Ver detalhes da publicação", exact: true })
    .click();
  expect(state.draft.files?.["public/index.html"]).toBe(content);
  expect(state.submissions).toHaveLength(0);
  await page
    .getByRole("link", { name: "Voltar ao projeto", exact: true })
    .click();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  await expect(page.locator(".code-editor")).toHaveValue(
    /Minha alteração preservada/,
  );
  await expect(
    page.getByRole("button", { name: "Publicar", exact: true }),
  ).toBeDisabled();
});

test("recarga e falha transitória preservam etapa e reconectam sem repetir o envio", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  setStage(state, "publication_building");
  await expect(
    page.getByRole("heading", { name: "Publicação em andamento", exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(".publication-steps [aria-current=step]"),
  ).toContainText("Preparar aplicação");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Publicação em andamento", exact: true }),
  ).toBeVisible();
  expect(state.submissions).toHaveLength(1);
  state.operationFailures = 3;
  await expect(
    page.getByText("Atualização interrompida", { exact: false }),
  ).toBeVisible({ timeout: 10000 });
  await expect(
    page.locator(".publication-steps [aria-current=step]"),
  ).toContainText("Preparar aplicação");
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeDisabled();
  state.operationFailures = 0;
  setStage(state, "publication_verifying");
  await page.getByRole("button", { name: "Reconectar acompanhamento" }).click();
  await expect(
    page.locator(".publication-steps [aria-current=step]"),
  ).toContainText("Verificar publicação");
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(state.submissions).toHaveLength(1);
});

test("resultado em conferência bloqueia reenvio mesmo com disponibilidade desatualizada", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  setStage(state, "publication_verifying");
  setStage(state, "publication_reconciling", "reconciling");
  state.reasons = [];
  await expect(
    page.getByRole("heading", { name: "Resultado em conferência" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeDisabled();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Resultado em conferência" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeDisabled();
  expect(state.submissions).toHaveLength(1);
});

test("envio recusado mantém confirmação e a repetição usa a mesma chave idempotente", async ({
  page,
}) => {
  const state = await publication(page);
  state.submissionFailures = 1;
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  const submit = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  await submit.click();
  await expect(page.getByRole("alert")).toContainText(
    "O envio não pôde ser confirmado",
  );
  await expect(page).toHaveURL(`/app/projetos/${projectId}/publicar`);
  await expect(submit).toBeEnabled();
  expect(state.submissions).toHaveLength(1);
  await submit.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[1].idempotency).toBe(
    state.submissions[0].idempotency,
  );
});

test("nova tentativa acompanha a nova operação e mantém revisão da publicação anterior separada", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  const firstId = state.operations[0].id;
  setStage(state, "publication_building");
  setStage(state, "publication_failed", "failed");
  state.operations[0].error_code = "BUILD_FAILED";
  state.reasons = [];
  await expect(
    page.getByRole("heading", {
      name: "Publicação não concluída",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  expect(state.operations[0].id).not.toBe(firstId);
  await expect(page).toHaveURL(
    new RegExp(`operation=${state.operations[0].id}$`),
  );
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[1].idempotency).not.toBe(
    state.submissions[0].idempotency,
  );
  await page.getByText("Detalhes da publicação", { exact: true }).click();
  await expect(page.locator(".publication-details")).toContainText(
    state.operations[0].id,
  );
  await expect(page.locator(".publication-details")).not.toContainText(
    "BUILD_FAILED",
  );
});

test("operação de outro projeto não é exibida nem libera envio", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  state.operations[0].project_id = "outro-projeto";
  state.operations[0].progress_message = "Conteúdo de outro projeto";
  state.reasons = [];
  await page.reload();
  await expect(page.getByRole("alert")).toContainText(
    "não pertence ao projeto",
  );
  await expect(page.locator("body")).not.toContainText(
    "Conteúdo de outro projeto",
  );
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeDisabled();
  expect(state.submissions).toHaveLength(1);
});

test("sucesso confirma somente etapas registradas e não repete polling da operação terminal", async ({
  page,
  context,
}) => {
  await page.clock.install();
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await publication(page);
  await startPublication(page, state);
  setStage(state, "publication_building");
  setStage(state, "publication_verified", "succeeded");
  state.operations[0].result_ref = {
    url: publicationUrl,
    revision_id: "revision-anterior",
    verified_at: "2026-10-28T11:05:00Z",
  };
  state.reasons = [];
  await page.clock.fastForward(1500);
  await expect(
    page.getByRole("heading", { name: "Publicação concluída", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".publication-steps li")
      .filter({ hasText: "Preparar recursos" }),
  ).toContainText("Sem registro");
  await expect(
    page
      .locator(".publication-steps li")
      .filter({ hasText: "Preparar aplicação" }),
  ).toContainText("Concluída");
  await page.getByRole("button", { name: "Copiar link", exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    publicationUrl + "/",
  );
  await expect(page.getByText("Link copiado.", { exact: true })).toBeVisible();
  const count = state.operationRequests.length;
  await page.clock.fastForward(60000);
  expect(state.operationRequests).toHaveLength(count);
  expect(state.submissions).toHaveLength(1);
  await page.getByText("Detalhes da publicação", { exact: true }).click();
  await expect(page.locator(".publication-details")).toContainText(
    "revision-anterior",
  );
  await expect(page.locator(".publication-confirmation")).toContainText(
    revisionId.slice(0, 8),
  );
});

test("link inseguro fica indisponível e leitura direta nunca dispara uma publicação", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  setStage(state, "publication_verified", "succeeded");
  state.operations[0].result_ref = {
    url: "javascript:alert(1)",
    revision_id: revisionId,
  };
  state.reasons = [];
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Publicação concluída", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Abrir publicação" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Copiar link" })).toHaveCount(
    0,
  );
  expect(state.submissions).toHaveLength(1);
});

test("tela de publicação acessível por teclado e responsiva sem overflow", async ({
  page,
}, info) => {
  const state = await publication(page);
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    (await page.locator(".publication-confirmation").boundingBox())!.y,
  ).toBeLessThan(
    (await page.locator(".publication-progress").boundingBox())!.y,
  );
  await expect(page.locator(".publication-retention")).toContainText(
    "Consulte Uso e acesso para conferir o prazo de download.",
  );
  await expect(page.locator(".publication-retention")).not.toContainText(
    "Ainda não definido",
  );
  await page.screenshot({
    path: info.outputPath("publication-before-375.png"),
    fullPage: true,
  });
  await startPublication(page, state);
  setStage(state, "publication_building");
  await expect(
    page.getByRole("heading", { name: "Publicação em andamento", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".publication-steps li")
      .filter({ hasText: "Solicitação recebida" }),
  ).toContainText("Sem registro");
  expect(
    (await page.locator(".publication-progress").boundingBox())!.y,
  ).toBeLessThan(
    (await page.locator(".publication-confirmation").boundingBox())!.y,
  );
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if ([375, 1440].includes(width))
      await page.screenshot({
        path: info.outputPath(`publication-${width}.png`),
        fullPage: true,
      });
  }
  const details = page.locator(".publication-details summary");
  await details.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".publication-details")).toHaveAttribute(
    "open",
    "",
  );
  expect(
    (
      await new AxeBuilder({ page })
        .include(".publication-page")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("resposta de envio perdida recupera a nova operação ao consultar uma tentativa anterior", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  const previousId = state.operations[0].id;
  setStage(state, "publication_failed", "failed");
  state.reasons = [];
  await expect(
    page.getByRole("heading", {
      name: "Publicação não concluída",
      exact: true,
    }),
  ).toBeVisible();
  state.lostSubmissionResponses = 1;
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  await expect(page.getByRole("alert")).toContainText(
    "O envio não pôde ser confirmado. Consulte o status acima antes de tentar novamente.",
  );
  expect(state.operations[0].id).not.toBe(previousId);
  await page.getByText("Detalhes da publicação", { exact: true }).click();
  await expect(page.locator(".publication-details")).toContainText(
    state.operations[0].id,
  );
  expect(state.submissions).toHaveLength(2);
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
});

test("sair durante o envio mantém o destino escolhido e a operação segue recuperável", async ({
  page,
}) => {
  const state = await publication(page);
  state.reasons = [];
  await page
    .getByRole("button", { name: "Verificar novamente", exact: true })
    .click();
  state.submissionDelay = 1500;
  const response = page.waitForResponse(
    (value) =>
      value.request().method() === "POST" &&
      value.url().endsWith(`/projects/${projectId}/publications`),
  );
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Enviando solicitação", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "sending",
  );
  await page
    .getByRole("link", { name: "Voltar ao projeto", exact: true })
    .click();
  await response;
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await expect(page).toHaveURL(`/app/projetos/${projectId}`);
  await page
    .getByRole("button", { name: "Acompanhar publicação", exact: true })
    .click();
  await expect(page.locator(".publication-summary")).toHaveAttribute(
    "data-state",
    "queued",
  );
  expect(state.submissions).toHaveLength(1);
});

function failBuild(state: Awaited<ReturnType<typeof publication>>) {
  setStage(state, "publication_building");
  setStage(state, "publication_failed", "failed");
  const operation = state.operations[0];
  operation.error_code = "BUILD_FAILED";
  operation.result_ref = {
    revision_id: revisionId,
    publication_failure: {
      code: "BUILD_FAILED",
      message: "A compilação encontrou um erro no código da revisão salva.",
      stage: "publication_building",
      build: {
        format_version: 1,
        category: "source",
        tool: "esbuild",
        exit_code: 1,
        duration_ms: 672,
        stdout_bytes: 0,
        stderr_bytes: 420,
        output_truncated: false,
        operation_id: operation.id,
        correlation_id: operation.id,
        revision_id: revisionId,
        issues: [
          {
            code: "SYNTAX_ERROR",
            message: 'Expected ";" but found "}"',
            hint: "Revise a sintaxe na posição indicada e salve uma nova revisão.",
            path: "src/index.ts",
            line: 27,
            column: 9,
          },
        ],
      },
    },
  };
  state.reasons = [];
}

test("falha de compilação mostra arquivo, posição, orientação e relatório copiável", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await publication(page);
  await startPublication(page, state);
  failBuild(state);
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da compilação",
    exact: true,
  });
  await expect(diagnostic).toBeVisible();
  await expect(page.locator(".publication-summary")).toContainText(
    "A compilação encontrou um erro no código",
  );
  await expect(diagnostic).toContainText("src/index.ts:27:9");
  await expect(diagnostic).toContainText('Expected ";" but found "}"');
  await expect(diagnostic).toContainText(
    "Revise a sintaxe na posição indicada",
  );
  await expect(diagnostic).toContainText(state.operations[0].id);
  await expect(diagnostic).toContainText(revisionId);
  await expect(
    diagnostic.getByRole("link", { name: "Revisar código do projeto" }),
  ).toHaveAttribute("href", `/app/projetos/${projectId}`);
  await expect(
    diagnostic.getByRole("link", { name: "Consultar revisões" }),
  ).toHaveAttribute("href", `/app/projetos/${projectId}/historico`);
  await diagnostic.getByRole("button", { name: "Copiar diagnóstico" }).click();
  await expect(diagnostic.getByRole("status")).toHaveText(
    "Diagnóstico copiado.",
  );
  const report = await page.evaluate(() => navigator.clipboard.readText());
  expect(report).toContain("src/index.ts:27:9");
  expect(report).toContain(`Correlação: ${state.operations[0].id}`);
  expect(report).toContain("SYNTAX_ERROR");
  await page.reload();
  await expect(diagnostic).toContainText("src/index.ts:27:9");
  expect(state.submissions).toHaveLength(1);
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect(diagnostic).toHaveCount(0);
  expect(state.submissions).toHaveLength(2);
});

test("tentativa antiga sem diagnóstico informa a ausência sem inventar causa ou posição", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  setStage(state, "publication_failed", "failed");
  state.operations[0].error_code = "BUILD_FAILED";
  state.reasons = [];
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da compilação",
    exact: true,
  });
  await expect(diagnostic).toContainText(
    "não possui um diagnóstico detalhado de compilação armazenado",
  );
  await expect(diagnostic).toContainText(state.operations[0].id);
  await expect(
    diagnostic.getByRole("button", { name: "Copiar diagnóstico" }),
  ).toHaveCount(0);
  await expect(diagnostic).not.toContainText("SYNTAX_ERROR");
  expect(state.submissions).toHaveLength(1);
});

test("diagnóstico distingue revisão anterior, corte da saída e mensagens como texto", async ({
  page,
}) => {
  const state = await publication(page);
  await startPublication(page, state);
  failBuild(state);
  const build = state.operations[0].result_ref.publication_failure!.build!;
  build.revision_id = "revision-before-current";
  build.output_truncated = true;
  build.issues[0].message =
    '<img src=x onerror="window.unexpectedBuildHtml=true">';
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da compilação",
    exact: true,
  });
  await expect(diagnostic).toContainText(
    "diagnóstico pertence a uma revisão anterior",
  );
  await expect(diagnostic).toContainText("A saída excedeu o limite de captura");
  await expect(diagnostic).toContainText(build.issues[0].message);
  await expect(diagnostic.locator("img")).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as typeof window & { unexpectedBuildHtml?: boolean })
          .unexpectedBuildHtml,
    ),
  ).toBeUndefined();
  await page.reload();
  await expect(diagnostic).toContainText("revision-before-current");
  expect(state.draftWrites).toHaveLength(0);
  expect(state.submissions).toHaveLength(1);
});

test("diagnóstico de build mantém detalhes e ações acessíveis em telas pequenas", async ({
  page,
}, info) => {
  const state = await publication(page);
  await startPublication(page, state);
  failBuild(state);
  const build = state.operations[0].result_ref.publication_failure!.build!;
  build.issues[0].path =
    "src/modules/authorization/handlers/session-and-cart-authorization.ts";
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da compilação",
    exact: true,
  });
  await expect(diagnostic).toBeVisible();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(
      await diagnostic.evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true);
    if ([375, 1440].includes(width))
      await page.screenshot({
        path: info.outputPath(`build-diagnostic-${width}.png`),
        fullPage: true,
      });
  }
  const execution = diagnostic.getByText("Informações da execução", {
    exact: true,
  });
  await execution.focus();
  await page.keyboard.press("Enter");
  await expect(diagnostic.locator("details")).toHaveAttribute("open", "");
  await expect(diagnostic).toContainText("Código de saída");
  expect(
    (
      await new AxeBuilder({ page })
        .include(".publication-build-diagnostics")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  const code = diagnostic.getByRole("link", {
    name: "Revisar código do projeto",
  });
  await code.focus();
  await code.scrollIntoViewIfNeeded();
  await expect(code).toBeFocused();
  await expect(code).toBeInViewport({ ratio: 0.95 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
