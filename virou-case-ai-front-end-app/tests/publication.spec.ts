import { expect, test, type Page } from "@playwright/test";
import type { Operation } from "../src/shared/types";

const projectId = "project-publication-regression";
const revisionId = "revision-saved-hono";
const publicationUrl = "https://publication-regression.example.workers.dev";
const pendingReason = {
  code: "CAPABILITY_UNVALIDATED",
  message:
    "A publicação aguarda a verificação operacional na conexão Cloudflare do backoffice.",
};

async function publication(page: Page) {
  const state = {
    reasons: [pendingReason],
    readinessFailures: 0,
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
            allowed_actions: ["read", "write", "publish", "export"],
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
    if (path === "/operations")
      return route.fulfill({
        json: {
          items:
            url.searchParams.get("kind") === "generation"
              ? []
              : state.operations,
          next_cursor: null,
        },
      });
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
                revision_id: revisionId,
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
      const operation: Operation = {
        id: "publication-operation",
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
      state.operations = [operation];
      state.reasons = [
        {
          code: "OPERATION_CONFLICT",
          message: "Há uma publicação em andamento ou em verificação neste projeto.",
        },
      ];
      return route.fulfill({ status: 202, json: operation });
    }
    await route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND" } } });
  });
  await page.goto(`/app/projetos/${projectId}/publicar`);
  await expect(
    page.getByRole("heading", { name: "Publicar projeto", exact: true }),
  ).toBeVisible();
  return state;
}

test("libera após qualificação da stack, publica a revisão salva e acompanha o resultado", async ({
  page,
}) => {
  const state = await publication(page);
  const publish = page.getByRole("button", { name: "Publicar", exact: true });
  await expect(publish).toBeDisabled();
  await expect(page.locator("#publication-reason")).toHaveText(pendingReason.message);
  expect(state.submissions).toHaveLength(0);

  state.reasons = [];
  await page.getByRole("button", { name: "Verificar novamente" }).click();
  await expect(publish).toBeEnabled();
  expect(state.readinessRequests.every((revision) => revision === revisionId)).toBe(true);
  await publish.click();
  const dialog = page.getByRole("dialog", { name: "Publicar revisão salva" });
  await expect(dialog).toContainText(revisionId.slice(0, 8));
  await dialog.getByRole("button", { name: "Publicar esta revisão" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(publish).toBeDisabled();
  await expect(page.locator("#publication-reason")).toContainText("publicação em andamento");
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
  await expect(page.getByRole("link", { name: "Abrir publicação" })).toHaveAttribute(
    "href",
    publicationUrl,
    { timeout: 10000 },
  );
  await expect(page.getByText("Publicação confirmada.", { exact: true })).toBeVisible();
  await expect(publish).toBeEnabled();
  expect(state.submissions).toHaveLength(1);
});

test("atualiza automaticamente a disponibilidade sem recarregar o projeto", async ({ page }) => {
  await page.clock.install();
  const state = await publication(page);
  const publish = page.getByRole("button", { name: "Publicar", exact: true });
  await expect(page.locator("#publication-reason")).toHaveText(pendingReason.message);
  await expect(publish).toBeDisabled();
  const before = state.readinessRequests.length;
  state.reasons = [];
  await page.clock.fastForward(15000);
  await expect(publish).toBeEnabled();
  expect(state.readinessRequests.length).toBeGreaterThan(before);
  expect(state.submissions).toHaveLength(0);
});

test("mantém bloqueio quando a stack ou o acesso não estão autorizados", async ({ page }) => {
  const state = await publication(page);
  const publish = page.getByRole("button", { name: "Publicar", exact: true });
  for (const reason of [
    { code: "UNSUPPORTED_STACK", message: "Este perfil ainda não foi habilitado para publicação." },
    { code: "ACCESS_EXPIRED", message: "Seu acesso não permite novas publicações. Fale com o responsável." },
  ]) {
    state.reasons = [reason];
    await page.getByRole("button", { name: "Verificar novamente" }).click();
    await expect(page.locator("#publication-reason")).toHaveText(reason.message);
    await expect(publish).toBeDisabled();
  }
  expect(state.submissions).toHaveLength(0);
});

test("recupera falha de consulta sem liberar publicação antes da resposta válida", async ({ page }) => {
  const state = await publication(page);
  const publish = page.getByRole("button", { name: "Publicar", exact: true });
  await expect(page.locator("#publication-reason")).toHaveText(pendingReason.message);
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
