import { expect, test, type Page } from "@playwright/test";

const worker = {
  id: "worker:test",
  available: false,
  code: "SANDBOX_ARGUMENT_INVALID",
  message: "Synthetic sandbox failure",
  checked_at: "2026-10-27T21:30:00Z",
  diagnostic_id: "00000000-0000-4000-8000-000000000001",
  remediation: "Recreate the worker with the corrected namespace arguments.",
  details: {
    stage: "sandbox_preflight",
    exit_code: 1,
    stderr: "bwrap: --disable-userns requires --unshare-user",
  },
  environment: { uid: 10001, Seccomp: "2" },
};
const blocked = {
  can_publish: false,
  can_verify: false,
  reasons: [{ code: "EXECUTOR_UNAVAILABLE", message: "Executor unavailable" }],
  workers: [worker],
  verification: null,
};

async function open(page: Page, diagnostic: unknown) {
  await page.route(
    "**/admin-api/v1/integration-connections/connection-test/publication-diagnostics",
    (route) => route.fulfill({ json: diagnostic }),
  );
  await page.goto("/tests/publication-diagnostics.fixture.html");
  await page
    .getByLabel("Motivo da verifica\u00e7\u00e3o")
    .fill("Synthetic operational verification");
}

test("sandbox blocked: concrete stderr and correlation, no remote POST", async ({
  page,
}) => {
  let sent = 0;
  page.on("request", (request) => {
    if (request.method() === "POST") sent += 1;
  });
  await open(page, blocked);
  await expect(
    page.getByRole("button", {
      name: "Verificar publica\u00e7\u00e3o",
      exact: true,
    }),
  ).toBeDisabled();
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await expect(
    page.getByText(worker.diagnostic_id, { exact: true }),
  ).toBeVisible();
  await page
    .getByText("Detalhes t\u00e9cnicos do executor", { exact: true })
    .click();
  await expect(page.getByLabel("Erro sanitizado do executor")).toHaveText(
    worker.details.stderr,
  );
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBeTruthy();
  }
  expect(sent).toBe(0);
});

test("ready executor can verify but does not imply completed qualification", async ({
  page,
}) => {
  await open(page, {
    ...blocked,
    can_verify: true,
    reasons: [
      { code: "CAPABILITY_UNVALIDATED", message: "Qualification pending" },
    ],
    workers: [
      {
        ...worker,
        available: true,
        code: "READY",
        details: { stage: "isolation_probe", exit_code: 0, stderr: "" },
      },
    ],
  });
  await expect(
    page.getByRole("button", {
      name: "Verificar publica\u00e7\u00e3o",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByRole("listitem").filter({ hasText: "Qualification pending" }),
  ).toBeVisible();
});

test("pending cleanup can resume even while executor is unavailable", async ({
  page,
}) => {
  await open(page, {
    ...blocked,
    verification: {
      id: "pending-cleanup",
      state: "reconciling",
      stage: "verification_cleanup",
      error_code: "PUBLICATION_RESULT_UNKNOWN",
    },
  });
  await expect(
    page.getByRole("button", {
      name: "Retomar verifica\u00e7\u00e3o",
      exact: true,
    }),
  ).toBeEnabled();
});

test("failed refresh clears stale readiness and disables remote action", async ({
  page,
}) => {
  await open(page, {
    ...blocked,
    can_verify: true,
    workers: [{ ...worker, available: true, code: "READY" }],
  });
  const button = page.getByRole("button", {
    name: "Verificar publica\u00e7\u00e3o",
    exact: true,
  });
  await expect(button).toBeEnabled();
  await page.unroute(
    "**/admin-api/v1/integration-connections/connection-test/publication-diagnostics",
  );
  await page.route(
    "**/admin-api/v1/integration-connections/connection-test/publication-diagnostics",
    (route) =>
      route.fulfill({
        status: 503,
        json: { error: { message: "Diagnostic temporarily unavailable" } },
      }),
  );
  await page
    .getByRole("button", { name: "Atualizar estado", exact: true })
    .click();
  await expect(button).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText(
    "Diagnostic temporarily unavailable",
  );
});

test("partial verification exposes the failed profile and preserves the approved profile", async ({
  page,
}) => {
  const failure = {
    code: "PUBLICATION_RESULT_UNKNOWN",
    message: "Python upload could not be confirmed",
    stage: "verification_uploading",
    stack: "fastapi-python-d1",
  };
  await open(page, {
    ...blocked,
    can_publish: true,
    can_verify: true,
    reasons: [],
    workers: [{ ...worker, available: true, code: "READY" }],
    stack_readiness: [
      { stack: "hono-typescript-d1", can_publish: true, reasons: [] },
      {
        stack: "fastapi-python-d1",
        can_publish: false,
        reasons: [
          {
            code: "CAPABILITY_UNVALIDATED",
            message: "Python verification pending",
          },
        ],
      },
    ],
    verification: {
      id: "partial-check",
      state: "failed",
      stage: "failed",
      error_code: failure.code,
      failed_stage: failure.stage,
      failure,
      temporary_resources: false,
      stack_results: [
        { stack: "hono-typescript-d1", passed: true },
        { stack: "fastapi-python-d1", passed: false, failure },
      ],
      cleanup: {
        state: "retained",
        worker_removed: false,
        database_removed: false,
      },
      resources: [
        {
          stack: "hono-typescript-d1",
          worker_name: "viroucase-ai-check-hono-retained",
          database_id: "database-retained",
          url: "https://viroucase-ai-check-hono-retained.example.workers.dev",
          state: "retained",
          worker_confirmed: true,
        },
      ],
    },
  });
  const profiles = page.getByRole("table", {
    name: "Prontidão de publicação e verificação por perfil",
  });
  await expect(
    profiles.getByRole("row").filter({ hasText: "hono-typescript-d1" }),
  ).toContainText("Liberada");
  await expect(
    profiles.getByRole("row").filter({ hasText: "fastapi-python-d1" }),
  ).toContainText("Bloqueada");
  const previous = page.locator(".cf-previous-result");
  await expect(previous).toContainText("fastapi-python-d1");
  await expect(previous).toContainText("Enviando o Worker");
  await expect(previous).toContainText("Resultado da publicação ainda não confirmado");
  await page
    .getByText("Resultado por perfil da última verificação", { exact: true })
    .click();
  const results = page.getByRole("list", { name: "Resultado por perfil" });
  await expect(
    results.getByRole("listitem").filter({ hasText: "hono-typescript-d1" }),
  ).toContainText("Checks aprovados no histórico");
  await expect(
    results.getByRole("listitem").filter({ hasText: "fastapi-python-d1" }),
  ).toContainText("Não aprovado");
  await expect(
    page.getByText("Retenção habilitada. Os recursos de teste são preservados, inclusive em caso de falha.", { exact: false }),
  ).toBeVisible();
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await page
    .getByText("Recursos e detalhes da verificação", { exact: true })
    .click();
  await expect(page.getByRole("region", { name: "Diagnóstico do resultado anterior", exact: true })).toContainText(failure.message);
  await expect(
    page.getByRole("list", { name: "Recursos da verificação" }),
  ).toContainText("database-retained");
  await expect(
    page.getByRole("link", { name: "Abrir Worker de hono-typescript-d1" }),
  ).toHaveAttribute(
    "href",
    "https://viroucase-ai-check-hono-retained.example.workers.dev/",
  );
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBeTruthy();
  }
});

test("retained resources never expose non-HTTPS links", async ({ page }) => {
  await open(page, {
    ...blocked,
    verification: {
      id: "retained-check",
      state: "failed",
      stage: "failed",
      error_code: "PUBLICATION_RESULT_UNKNOWN",
      cleanup: { state: "retained" },
      temporary_resources: false,
      resources: [
        {
          stack: "hono-typescript-d1",
          worker_name: "retained-worker",
          database_id: "retained-database",
          url: "javascript:alert(1)",
          state: "retained",
          worker_confirmed: true,
        },
      ],
    },
  });
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await page
    .getByText("Recursos e detalhes da verificação", { exact: true })
    .click();
  await expect(
    page.getByRole("list", { name: "Recursos da verificação" }),
  ).toContainText("retained-worker");
  await expect(
    page.getByRole("link", { name: "Abrir Worker de hono-typescript-d1" }),
  ).toHaveCount(0);
});

test("retention policy does not imply successful resource creation", async ({
  page,
}) => {
  await open(page, {
    ...blocked,
    verification: {
      id: "planned-check",
      state: "failed",
      stage: "failed",
      error_code: "CONFIGURATION_INVALID",
      cleanup: { state: "retained" },
      temporary_resources: false,
      resources: [
        {
          stack: "hono-typescript-d1",
          worker_name: "planned-worker",
          database_id: null,
          url: "https://planned-worker.example.workers.dev",
          state: "retained",
          worker_confirmed: false,
        },
      ],
    },
  });
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await page
    .getByText("Recursos e detalhes da verificação", { exact: true })
    .click();
  const resources = page.getByRole("list", { name: "Recursos da verificação" });
  await expect(resources).toContainText(
    "Nome previsto do Worker: planned-worker",
  );
  await expect(resources).toContainText(
    "Criação/publicação ainda não confirmada.",
  );
  await expect(resources).toContainText("Provisionamento ainda não confirmado");
  await expect(
    page.getByRole("link", { name: "Abrir Worker de hono-typescript-d1" }),
  ).toHaveCount(0);
});

test("legacy cleanup remains visible without describing new checks as destructive", async ({
  page,
}) => {
  await open(page, {
    ...blocked,
    verification: {
      id: "legacy-check",
      state: "failed",
      stage: "failed",
      error_code: "PUBLICATION_RESULT_UNKNOWN",
      temporary_resources: true,
      cleanup: {
        state: "succeeded",
        worker_removed: true,
        database_removed: true,
      },
    },
  });
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await page.getByText("Recursos e detalhes da verificação", { exact: true }).click();
  await expect(
    page.getByText(
      "Esta verificação antiga removeu recursos temporários ao terminar.",
      { exact: false },
    ),
  ).toBeVisible();
  await page.getByText("O que esta verificação faz?", { exact: true }).click();
  await expect(
    page.getByText(
      "e preserva esses recursos ao terminar, inclusive em caso de falha.",
      { exact: false },
    ),
  ).toBeVisible();
});

test("verification keeps the versioned command contract and never requests deletion", async ({
  page,
}) => {
  const mutations: {
    method: string;
    path: string;
    version?: string;
    reason: string;
  }[] = [];
  const methods: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
      methods.push(request.method());
  });
  await page.route(
    "**/admin-api/v1/integration-connections/connection-test/verify-publication",
    async (route) => {
      const request = route.request();
      mutations.push({
        method: request.method(),
        path: new URL(request.url()).pathname,
        version: request.headers()["if-match"],
        reason: request.postDataJSON().reason,
      });
      await route.fulfill({
        status: 202,
        json: { id: "new-check", state: "queued" },
      });
    },
  );
  await open(page, {
    ...blocked,
    can_verify: true,
    workers: [{ ...worker, available: true, code: "READY" }],
  });
  await page
    .getByRole("button", { name: "Verificar publicação", exact: true })
    .click();
  await expect
    .poll(() => mutations)
    .toEqual([
      {
        method: "POST",
        path: "/admin-api/v1/integration-connections/connection-test/verify-publication",
        version: "1",
        reason: "Synthetic operational verification",
      },
    ]);
  expect(methods).toEqual(["POST"]);
});
