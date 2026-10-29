import { expect, test, type Page } from "@playwright/test";
import type { Operation } from "../src/admin-types";

const initialOperation: Operation = {
  id: "publication-recovery-test",
  account_id: "publication-student",
  type: "publication",
  state: "reconciling",
  billing_status: "not_applicable",
  policy: {},
  result_ref: {
    publication_recovery: {
      state: "scheduled",
      message: "A conferência automática desta publicação está programada.",
      attempts: 1,
      last_attempt_at: "2026-10-29T10:27:00Z",
      last_checked_at: "2026-10-29T10:27:03Z",
      next_attempt_at: "2026-10-29T10:28:03Z",
      last_error_code: null,
    },
  },
};

async function operations(page: Page) {
  const state = {
    operation: structuredClone(initialOperation),
    queries: 0,
    failures: 0,
    submissions: [] as { body: unknown; csrf: string; idempotency: string }[],
  };
  await page.route("**/admin-api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/admin-api/v1", "");
    if (path === "/me")
      return route.fulfill({
        json: {
          id: "publication-operator",
          name: "Operador de publicação",
          csrf_token: "publication-recovery-csrf",
        },
      });
    if (path === "/accounts")
      return route.fulfill({ json: { items: [], next_cursor: null } });
    if (path === "/operations") {
      state.queries += 1;
      if (state.failures > 0) {
        state.failures -= 1;
        return route.fulfill({
          status: 503,
          json: {
            error: { message: "A consulta está temporariamente indisponível." },
          },
        });
      }
      return route.fulfill({
        json: { items: [state.operation], next_cursor: null },
      });
    }
    if (path === `/publications/${initialOperation.id}/reconcile`) {
      state.submissions.push({
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
        idempotency: request.headers()["idempotency-key"],
      });
      return route.fulfill({ json: state.operation });
    }
    return route.fulfill({
      status: 404,
      json: { error: { message: "Não encontrado." } },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Operações", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: initialOperation.id, exact: false }),
  ).toBeVisible();
  return state;
}

test("operações acompanham recuperação por consulta e param no estado terminal", async ({
  page,
}, info) => {
  await page.clock.install();
  const state = await operations(page);
  const row = page.getByRole("row").filter({ hasText: initialOperation.id });
  await expect(row).toContainText("Em conferência");
  await expect(row).toContainText("Próxima conferência automática");
  await expect(
    row.getByRole("button", { name: "Conferir agora" }),
  ).toBeEnabled();
  state.failures = 1;
  await page.clock.fastForward(4000);
  await expect(page.getByRole("alert")).toContainText(
    "temporariamente indisponível",
  );
  await expect(row).toContainText("Em conferência");
  state.operation = {
    ...state.operation,
    state: "failed",
    error_code: "MIGRATION_CHECKSUM_CONFLICT",
    result_ref: {
      publication_failure: {
        code: "MIGRATION_CHECKSUM_CONFLICT",
        message:
          "Uma migration já aplicada foi alterada. Restaure o arquivo original e crie uma nova migration.",
        stage: "publication_provisioning",
        migration: {
          format_version: 1,
          category: "migration",
          code: "MIGRATION_CHECKSUM_CONFLICT",
          issue_count: 1,
          published_revision_id: "published-original-revision",
          issues: [
            {
              code: "MIGRATION_CHECKSUM_CONFLICT",
              reason: "checksum_changed",
              path: "migrations/0001.sql",
              receipt_source: "remote",
              message: "O arquivo difere do registro já aplicado ao banco.",
              hint: "Restaure esta migration a partir da revisão publicada e salve as novas alterações em outro arquivo.",
            },
          ],
        },
      },
      publication_recovery: {
        ...initialOperation.result_ref!.publication_recovery!,
        state: "resolved",
        resolution: "pre_upload_failure",
        message: "A tentativa foi encerrada antes do envio à hospedagem.",
        next_attempt_at: null,
      },
    },
  };
  await page.clock.fastForward(4000);
  await expect(row).toContainText("Não concluída");
  await expect(row).toContainText("MIGRATION_CHECKSUM_CONFLICT");
  await expect(row).toContainText("Restaure o arquivo original");
  await row.getByText("Diagnóstico das migrations", { exact: true }).click();
  await expect(row).toContainText("migrations/0001.sql");
  await expect(row).toContainText(
    "O arquivo difere do registro já aplicado ao banco.",
  );
  await expect(row).toContainText("salve as novas alterações em outro arquivo");
  await expect(row).not.toContainText("Próxima conferência automática");
  await expect(
    row.getByRole("button", { name: "Reconciliar publicação" }),
  ).toBeDisabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
  const queries = state.queries;
  await page.clock.fastForward(16000);
  expect(state.queries).toBe(queries);
  expect(state.submissions).toHaveLength(0);
  await page.screenshot({
    path: info.outputPath("publication-recovery-failed-admin.png"),
    fullPage: true,
  });
});

test("publicação confirmada atualiza sem intervenção e sair da aba encerra consultas", async ({
  page,
}) => {
  await page.clock.install();
  const state = await operations(page);
  state.operation = {
    ...state.operation,
    state: "succeeded",
    result_ref: {
      publication_recovery: {
        ...initialOperation.result_ref!.publication_recovery!,
        state: "resolved",
        resolution: "published",
        message: "Publicação confirmada pela conferência remota.",
        next_attempt_at: null,
      },
    },
  };
  await page.clock.fastForward(4000);
  await expect(
    page.getByRole("row").filter({ hasText: initialOperation.id }),
  ).toContainText("Concluída");
  const terminalQueries = state.queries;
  await page.clock.fastForward(12000);
  expect(state.queries).toBe(terminalQueries);
  state.operation = structuredClone(initialOperation);
  await page.getByRole("button", { name: "Atualizar", exact: true }).click();
  await expect(page.getByText("Em conferência", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Contas e acesso", exact: true })
    .click();
  const queries = state.queries;
  await page.clock.fastForward(16000);
  expect(state.queries).toBe(queries);
  expect(state.submissions).toHaveLength(0);
});

test("revisão necessária mantém ação auditada sem duplicar a publicação ou a consulta manual", async ({
  page,
}) => {
  await page.clock.install();
  const state = await operations(page);
  state.operation.result_ref!.publication_recovery = {
    ...initialOperation.result_ref!.publication_recovery!,
    state: "attention",
    message:
      "O resultado das migrations precisa de revisão antes de continuar.",
    attempts: 5,
    next_attempt_at: null,
    last_error_code: "MIGRATION_HISTORY_UNCERTAIN",
  };
  await page.clock.fastForward(4000);
  const row = page.getByRole("row").filter({ hasText: initialOperation.id });
  await expect(row).toContainText("Aguardando revisão");
  await expect(row).not.toContainText("Próxima conferência automática");
  await row.getByRole("button", { name: "Reconciliar publicação" }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Motivo", { exact: true })
    .fill("Conferir recibos após revisão técnica");
  await dialog
    .getByRole("button", { name: "Consultar e reconciliar" })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  await expect(dialog).toHaveCount(0);
  expect(state.submissions).toHaveLength(1);
  expect(state.submissions[0]).toEqual({
    body: { reason: "Conferir recibos após revisão técnica" },
    csrf: "publication-recovery-csrf",
    idempotency: expect.any(String),
  });
  expect(state.operation.id).toBe(initialOperation.id);
  await expect(row).toContainText("Aguardando revisão");
});
