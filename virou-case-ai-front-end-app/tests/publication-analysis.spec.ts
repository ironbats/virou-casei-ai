import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type {
  Operation,
  PublicationMigrationDiagnostic,
} from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";

test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

const projectId = "publication-analysis-project";
const revisionId = "saved-current-revision";

function publicationOperation(
  code: string,
  stage: string,
  migration?: PublicationMigrationDiagnostic,
): Operation {
  return {
    id: "publication-with-problem",
    type: "publication",
    project_id: projectId,
    state: "failed",
    progress_stage: "publication_failed",
    billing_status: "settled",
    pending_reconciliation: false,
    cancel_requested: false,
    error_code: code,
    progress_message: "A publicação precisa de revisão.",
    created_at: "2026-10-29T12:00:00Z",
    result_ref: {
      revision_id: revisionId,
      publication_failure: {
        code,
        stage,
        message: "Confira o diagnóstico antes de continuar.",
        ...(migration ? { migration } : {}),
      },
    },
  };
}

const destructive: PublicationMigrationDiagnostic = {
  format_version: 1,
  category: "migration",
  code: "MIGRATION_REVIEW_REQUIRED",
  analysis_method: "sql_lexical_v2",
  issue_count: 1,
  published_revision_id: "previously-published-revision",
  issues: [
    {
      code: "MIGRATION_REVIEW_REQUIRED",
      reason: "unsafe_sql",
      path: "migrations/0004_create_carts_and_cart_items_tables.sql",
      receipt_source: "candidate",
      line: 8,
      column: 1,
      statement_index: 2,
      statement_type: "DELETE",
      operation: "DELETE",
      rule: "data_change",
      message: "A migration contém um comando que exige revisão.",
      explanation: "DELETE pode remover registros existentes.",
      hint: "Revise o comando e preserve os registros existentes.",
    },
  ],
};

async function fixture(
  page: Page,
  initialOperation: Operation | null,
  options: {
    revision?: string | null;
    allowedActions?: string[];
    analysisEnabled?: boolean;
    reasons?: { code: string; message: string }[];
    previousAnalyses?: Operation[];
  } = {},
) {
  const state = {
    operation: initialOperation,
    analyses: options.previousAnalyses ?? [],
    requests: [] as {
      body: Record<string, unknown>;
      key: string;
      csrf: string;
    }[],
    revision: options.revision === undefined ? revisionId : options.revision,
    reasons: options.reasons ?? [],
    operationFailures: 0,
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    if (path === "/me")
      return route.fulfill({
        json: {
          id: "publication-analysis-user",
          name: "Aluno",
          role: "student",
          csrf_token: "publication-analysis-csrf",
          access: {
            status: "active",
            allowed_actions: options.allowedActions ?? [
              "read",
              "write",
              "publish",
              "export",
            ],
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
          name: "Projeto com diagnóstico",
          objective: "Preservar dados e publicar a aplicação",
          stack: "hono-typescript-d1",
          version: 1,
          current_revision_id: state.revision,
          state: "active",
          created_at: "2026-10-29T10:00:00Z",
        },
      });
    if (path === "/capabilities")
      return route.fulfill({
        json: {
          policy_version: "publication-analysis-policy",
          code_analysis: { enabled: options.analysisEnabled ?? true },
          generation: { enabled: true },
          publication: { enabled: true },
        },
      });
    if (path === `/projects/${projectId}/publication-readiness`)
      return route.fulfill({
        json: {
          can_publish: !!state.revision && !state.reasons.length,
          revision_id: state.revision,
          configuration_version: 1,
          reasons: state.reasons,
          publication: null,
        },
      });
    if (path === "/operations") {
      const kind = url.searchParams.get("kind");
      if (kind === "publication" && state.operationFailures-- > 0)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "STATUS_UNAVAILABLE",
              message: "A consulta do status falhou temporariamente.",
            },
          },
        });
      return route.fulfill({
        json: {
          items:
            kind === "publication"
              ? state.operation
                ? [state.operation]
                : []
              : state.analyses,
        },
      });
    }
    if (path.startsWith("/operations/")) {
      const id = path.split("/").at(-1);
      const operation = [state.operation, ...state.analyses].find(
        (item) => item?.id === id,
      );
      return route.fulfill({
        status: operation ? 200 : 404,
        json: operation ?? {
          error: { code: "NOT_FOUND", message: "Operação ausente." },
        },
      });
    }
    if (
      path === `/projects/${projectId}/code-analyses` &&
      request.method() === "POST"
    ) {
      const body = request.postDataJSON();
      state.requests.push({
        body,
        key: request.headers()["idempotency-key"],
        csrf: request.headers()["x-csrf-token"],
      });
      const analysis: Operation = {
        ...publicationOperation("", ""),
        id: `analysis-${state.analyses.length + 1}`,
        type: "code_analysis",
        state: "succeeded",
        progress_stage: "code_analysis_completed",
        error_code: null,
        result_ref: {
          kind: "code_analysis",
          source: "publication",
          revision_id: body.revision_id,
          publication_operation_id: body.publication_operation_id,
          summary: "Diagnóstico preparado com o contexto desta publicação.",
          issues: [],
          limitations: ["Nenhuma alteração ou publicação foi executada."],
          can_request_correction: false,
        },
      };
      state.analyses.unshift(analysis);
      return route.fulfill({ status: 202, json: analysis });
    }
    return route.fulfill({
      status: 404,
      json: {
        error: { code: "NOT_FOUND", message: `Recurso ausente: ${path}` },
      },
    });
  });
  await page.goto(`/app/projetos/${projectId}/publicar`);
  await expect(
    page.getByRole("heading", { name: "Publicação do projeto" }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Análise IA da publicação", exact: true }),
  ).toBeVisible();
  return state;
}

test("migration pendente mostra evidência e permite análise IA sem liberar publicação automaticamente", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await fixture(
    page,
    publicationOperation(
      "MIGRATION_REVIEW_REQUIRED",
      "publication_provisioning",
      destructive,
    ),
  );
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico das migrations",
    exact: true,
  });
  await expect(diagnostic).toContainText(
    "migrations/0004_create_carts_and_cart_items_tables.sql:8:1",
  );
  await expect(diagnostic).toContainText(
    "DELETE pode remover registros existentes.",
  );
  await expect(diagnostic).toContainText("regras automáticas de segurança");
  await diagnostic.getByRole("button", { name: "Copiar diagnóstico" }).click();
  const report = await page.evaluate(() => navigator.clipboard.readText());
  expect(report).toContain("Localização: linha 8, coluna 1");
  expect(report).toContain("Comando SQL: DELETE");
  expect(report).toContain(destructive.issues[0].explanation);
  expect(state.requests).toHaveLength(0);
  const analysis = page.getByRole("region", {
    name: "Análise IA da publicação",
    exact: true,
  });
  await analysis
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(analysis).toContainText(
    "Diagnóstico preparado com o contexto desta publicação.",
  );
  expect(state.requests).toHaveLength(1);
  expect(state.requests[0].body).toEqual({
    revision_id: revisionId,
    source: "publication",
    publication_operation_id: state.operation!.id,
    policy_version: "publication-analysis-policy",
  });
  expect(state.requests[0].key).toBeTruthy();
  expect(state.requests[0].csrf).toBe("publication-analysis-csrf");
  expect(state.operation!.state).toBe("failed");
});

test("diagnóstico antigo de migration mantém análise sem inventar o comando causador", async ({
  page,
}) => {
  const oldDiagnostic: PublicationMigrationDiagnostic = {
    format_version: 1,
    category: "migration",
    code: "MIGRATION_REVIEW_REQUIRED",
    issue_count: 1,
    issues: [
      {
        code: "MIGRATION_REVIEW_REQUIRED",
        reason: "unsafe_sql",
        path: "migrations/0004_create_carts_and_cart_items_tables.sql",
        receipt_source: "candidate",
        message:
          "Migration potencialmente destrutiva exige revisão antes de publicação.",
        hint: "Revise a migration incremental e preserve os dados existentes antes de publicar.",
      },
    ],
  };
  const state = await fixture(
    page,
    publicationOperation(
      "MIGRATION_REVIEW_REQUIRED",
      "publication_provisioning",
      oldDiagnostic,
    ),
  );
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico das migrations",
    exact: true,
  });
  await expect(diagnostic).toContainText(
    "Esta tentativa foi registrada sem a posição e o comando detectados.",
  );
  await expect(
    diagnostic.getByText("Comando SQL", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Pedir Análise IA", exact: true }),
  ).toBeEnabled();
  expect(state.requests).toHaveLength(0);
});

test("atalho de análise no cabeçalho revela a ação por teclado sem iniciar IA", async ({
  page,
}) => {
  const state = await fixture(
    page,
    publicationOperation(
      "MIGRATION_REVIEW_REQUIRED",
      "publication_provisioning",
      destructive,
    ),
  );
  const shortcut = page.getByRole("button", {
    name: "Análise IA",
    exact: true,
  });
  const panel = page.getByRole("region", {
    name: "Análise IA da publicação",
    exact: true,
  });
  const analyze = panel.getByRole("button", {
    name: "Pedir Análise IA",
    exact: true,
  });
  await expect(analyze).toBeEnabled();
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: 720 });
    await shortcut.scrollIntoViewIfNeeded();
    await shortcut.focus();
    await page.keyboard.press("Enter");
    await expect(panel).toBeFocused();
    await expect(analyze).toBeInViewport({ ratio: 1 });
    await page.keyboard.press("Tab");
    await expect(analyze).toBeFocused();
    expect(state.requests).toHaveLength(0);
  }
  await page.keyboard.press("Enter");
  await expect(panel).toContainText(
    "Diagnóstico preparado com o contexto desta publicação.",
  );
  expect(state.requests).toHaveLength(1);
});

for (const [code, stage] of [
  ["BUILD_FAILED", "publication_building"],
  ["PUBLICATION_UPLOAD_FAILED", "publication_uploading"],
  ["PUBLICATION_API_NOT_READY", "publication_verifying"],
  ["MIGRATION_HISTORY_UNCERTAIN", "publication_migrating"],
]) {
  test(`análise disponível sem diagnóstico detalhado para ${code}`, async ({
    page,
  }) => {
    const state = await fixture(page, publicationOperation(code, stage));
    const button = page.getByRole("button", {
      name: "Pedir Análise IA",
      exact: true,
    });
    await expect(button).toBeEnabled();
    expect(state.requests).toHaveLength(0);
    await button.click();
    await expect(
      page.getByRole("heading", { name: "Diagnóstico da IA" }),
    ).toBeVisible();
    expect(state.requests[0].body.publication_operation_id).toBe(
      state.operation!.id,
    );
    expect(state.operation!.state).toBe("failed");
  });
}

test("reconciliação mantém análise e identifica revisão histórica sem habilitar novo envio", async ({
  page,
}) => {
  const operation = publicationOperation(
    "MIGRATION_HISTORY_UNCERTAIN",
    "publication_migrating",
    destructive,
  );
  operation.state = "reconciling";
  operation.result_ref.revision_id = "previously-published-revision";
  const state = await fixture(page, operation);
  const analysis = page.getByRole("region", {
    name: "Análise IA da publicação",
    exact: true,
  });
  await expect(analysis).toContainText("Esta tentativa usou outra revisão.");
  await expect(analysis).toContainText(
    "A análise não confirma o banco nem libera a publicação.",
  );
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
  await analysis
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(analysis).toContainText("Diagnóstico preparado");
  expect(state.requests[0].body.revision_id).toBe(revisionId);
  expect(state.requests[0].body.publication_operation_id).toBe(operation.id);
  expect(state.operation!.state).toBe("reconciling");
});

test("bloqueio antes da primeira tentativa mantém análise e encaminha o motivo disponível", async ({
  page,
}) => {
  const state = await fixture(page, null, {
    reasons: [
      {
        code: "CAPABILITY_UNVALIDATED",
        message: "A conexão aguarda verificação operacional.",
      },
    ],
  });
  expect(state.requests).toHaveLength(0);
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  expect(state.requests[0].body).toMatchObject({
    source: "publication",
    revision_id: revisionId,
    runtime_error:
      "Disponibilidade da publicação · CAPABILITY_UNVALIDATED: A conexão aguarda verificação operacional.",
  });
  expect(state.requests[0].body).not.toHaveProperty("publication_operation_id");
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
});

for (const scenario of [
  {
    name: "sem revisão salva",
    revision: null,
    allowedActions: ["read", "write"],
    reason: "Salve uma revisão do projeto antes de pedir a análise.",
  },
  {
    name: "somente leitura",
    revision: revisionId,
    allowedActions: ["read"],
    reason: "Solicitar análise ou correção exige permissão de edição.",
  },
]) {
  test(`ação permanece visível e explica restrição ${scenario.name}`, async ({
    page,
  }) => {
    const state = await fixture(page, null, scenario);
    await expect(
      page.getByRole("button", { name: "Pedir Análise IA", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("region", {
        name: "Análise IA da publicação",
        exact: true,
      }),
    ).toContainText(scenario.reason);
    expect(state.requests).toHaveLength(0);
  });
}

test("indisponibilidade da configuração de IA apresenta motivo sem criar operação", async ({
  page,
}) => {
  const state = await fixture(
    page,
    publicationOperation("BUILD_FAILED", "publication_building"),
    { analysisEnabled: false },
  );
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Análise IA da publicação", exact: true }),
  ).toContainText("A análise de código está indisponível.");
  expect(state.requests).toHaveLength(0);
});

test("primeira publicação não reutiliza análise do editor como diagnóstico da publicação", async ({
  page,
}) => {
  const unrelated = publicationOperation("", "");
  unrelated.id = "editor-analysis";
  unrelated.type = "code_analysis";
  unrelated.state = "succeeded";
  unrelated.result_ref = {
    kind: "code_analysis",
    source: "editor",
    revision_id: revisionId,
    summary: "Análise exclusiva do editor",
    issues: [],
  };
  await fixture(page, null, { previousAnalyses: [unrelated] });
  await expect(
    page.getByRole("button", { name: "Pedir Análise IA", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByText("Análise exclusiva do editor", { exact: true }),
  ).toHaveCount(0);
});

test("falha temporária de acompanhamento preserva a referência e a análise", async ({
  page,
}) => {
  const state = await fixture(
    page,
    publicationOperation("PUBLICATION_UPLOAD_FAILED", "publication_uploading"),
  );
  await expect(
    page.getByRole("button", { name: "Pedir Análise IA", exact: true }),
  ).toBeEnabled();
  state.operationFailures = 1;
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect(
    page.getByText("A consulta do status falhou temporariamente.", {
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  expect(state.requests[0].body.publication_operation_id).toBe(
    state.operation!.id,
  );
  expect(state.requests[0].body.runtime_error).toContain("STATUS_UNAVAILABLE");
});

test("análise e evidências de migration permanecem acessíveis em tela estreita", async ({
  page,
}, info) => {
  await fixture(
    page,
    publicationOperation(
      "MIGRATION_REVIEW_REQUIRED",
      "publication_provisioning",
      destructive,
    ),
  );
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    await expect(
      page.getByRole("button", { name: "Pedir Análise IA", exact: true }),
    ).toBeVisible();
    if ([375, 1440].includes(width))
      await page.screenshot({
        path: info.outputPath(`publication-analysis-${width}.png`),
        fullPage: true,
      });
  }
  const report = await new AxeBuilder({ page })
    .include(".publication-analysis")
    .include(".publication-build-diagnostics")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(report.violations).toEqual([]);
});
