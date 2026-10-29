import { expect, test, type Page } from "@playwright/test";

const connection = {
  id: "configuration-test",
  connection_id: "connection-test",
  name: "Cloudflare produção",
  is_default: true,
  version: 7,
  state: "active",
  settings: { account_id: "a".repeat(32), environment: "production" },
  validation: { ready: true },
  has_token: true,
  validated_at: "2026-10-28T12:00:00Z",
};
const ready = {
  can_publish: true,
  can_verify: true,
  reasons: [],
  verification_reasons: [],
  workers: [
    {
      id: "worker:test",
      available: true,
      code: "READY",
      message: "Executor pronto",
      checked_at: "2026-10-28T12:00:00Z",
    },
  ],
  stack_readiness: [
    { stack: "hono-typescript-d1", can_publish: true, reasons: [] },
  ],
  verification: {
    id: "check-previous",
    state: "succeeded",
    stage: "verification_complete",
    error_code: null,
  },
};
const diagnosticPath =
  "**/admin-api/v1/integration-connections/connection-test/publication-diagnostics";

const automatic = {
  enabled: true,
  state: "scheduled",
  message:
    "A conexão é verificada automaticamente antes do vencimento da validação.",
  next_attempt_at: "2026-10-29T12:00:00Z",
  last_attempt_at: "2026-10-28T15:00:00Z",
  last_succeeded_at: "2026-10-28T15:01:00Z",
  last_error_code: null,
  consecutive_failures: 0,
  operation_id: "check-automatic",
};

async function open(
  page: Page,
  diagnostic: unknown = ready,
  config: unknown = connection,
) {
  await page.route(
    "**/admin-api/v1/integrations/cloudflare/connections/connection-test",
    (route) => route.fulfill({ json: config }),
  );
  await page.route(diagnosticPath, (route) =>
    route.fulfill({ json: diagnostic }),
  );
  await page.route(
    "**/admin-api/v1/publication-qualification-requirements",
    (route) =>
      route.fulfill({ json: { checks: ["isolamento", "publicacao_remota"] } }),
  );
  await page.goto("/tests/cloudflare-manager.fixture.html");
  await page
    .getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true })
    .click();
  await expect(
    page.getByRole("tab", { name: "Visão geral", exact: true }),
  ).toBeVisible();
  return page.getByRole("dialog");
}

async function tab(page: Page, name: string) {
  await page.getByRole("tab", { name, exact: true }).click();
  await expect(page.getByRole("tab", { name, exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}

test("visão geral concisa, navegação por teclado e layout de 320 a 1440 pixels", async ({
  page,
}) => {
  const dialog = await open(page);
  await expect(
    dialog
      .getByRole("tabpanel", { name: "Visão geral", exact: true })
      .getByText("Publicação liberada", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", {
      name: "Ver resultados da verificação",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByLabel("Motivo da verificação", { exact: true }),
  ).toBeHidden();
  await expect(dialog.locator("pre:visible")).toHaveCount(0);
  await page.getByRole("tab", { name: "Visão geral", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("tab", { name: "Verificação", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByLabel("Motivo da verificação", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("tab", { name: "Qualificação", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Home");
  await expect(
    page.getByRole("tab", { name: "Visão geral", exact: true }),
  ).toBeFocused();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    for (const name of [
      "Visão geral",
      "Verificação",
      "Conexão",
      "Qualificação",
    ]) {
      await tab(page, name);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth + 1,
        ),
      ).toBe(true);
      expect(
        await dialog.evaluate(
          (element) => element.scrollWidth <= element.clientWidth + 1,
        ),
      ).toBe(true);
      await page.keyboard.press("Tab");
      expect(
        await dialog.evaluate((element) =>
          element.contains(document.activeElement),
        ),
      ).toBe(true);
    }
  }
  await tab(page, "Visão geral");
  await page.screenshot({
    path: "test-results/cloudflare-overview-desktop.png",
  });
  await tab(page, "Verificação");
  await page.screenshot({
    path: "test-results/cloudflare-verification-desktop.png",
  });
  await page.setViewportSize({ width: 375, height: 960 });
  await tab(page, "Visão geral");
  await page.screenshot({
    path: "test-results/cloudflare-overview-mobile.png",
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", {
      name: "Gerenciar Cloudflare produção",
      exact: true,
    }),
  ).toBeFocused();
});

test("renovação automática mantém publicação válida e recupera expiração por polling sem POST manual", async ({
  page,
}) => {
  await page.clock.install();
  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST") posts += 1;
  });
  await open(page, {
    ...ready,
    automatic_verification: { ...automatic, state: "running" },
    verification: { ...ready.verification, state: "running" },
  });
  const overview = page.getByRole("tabpanel", {
    name: "Visão geral",
    exact: true,
  });
  const renewal = overview.getByRole("region", {
    name: "Renovação automática da publicação",
  });
  await expect(
    overview.getByText("Publicação liberada", { exact: true }),
  ).toBeVisible();
  await expect(renewal).toContainText("enquanto ela permanecer válida");
  await expect(renewal).toContainText("Última verificação aprovada");
  let current = {
    ...ready,
    can_publish: false,
    automatic_verification: { ...automatic, state: "queued" },
    verification: { ...ready.verification, state: "queued" },
  };
  await page.unroute(diagnosticPath);
  await page.route(diagnosticPath, (route) => route.fulfill({ json: current }));
  await page.clock.runFor(4001);
  await expect(
    overview.getByText("Publicação liberada", { exact: true }),
  ).toHaveCount(0);
  await expect(renewal).not.toContainText("enquanto ela permanecer válida");
  current = {
    ...ready,
    automatic_verification: automatic,
  };
  await page.clock.runFor(4001);
  await expect(
    overview.getByText("Publicação liberada", { exact: true }),
  ).toBeVisible();
  await expect(renewal).toContainText("Verificação automática programada");
  await tab(page, "Verificação");
  const verification = page.getByRole("tabpanel", {
    name: "Verificação",
    exact: true,
  });
  await expect(verification).toContainText(
    "Esta verificação valida a conexão e os perfis.",
  );
  await expect(verification).toContainText(
    "o resultado de cada projeto são conferidos na própria publicação.",
  );
  await expect(verification).toContainText("não exige verificações manuais");
  expect(posts).toBe(0);
});

test("renovação expõe próxima tentativa e falha sem anunciar publicação liberada", async ({
  page,
}) => {
  await open(page, {
    ...ready,
    can_publish: false,
    reasons: [
      {
        code: "CONFIGURATION_INVALID",
        message: "Token inválido ou sem permissão.",
      },
    ],
    automatic_verification: {
      ...automatic,
      state: "retry_wait",
      message:
        "A verificação automática será repetida após o intervalo de recuperação.",
      last_error_code: "CONFIGURATION_INVALID",
      consecutive_failures: 2,
    },
  });
  const overview = page.getByRole("tabpanel", {
    name: "Visão geral",
    exact: true,
  });
  await expect(overview).toContainText("Aguardando nova tentativa automática");
  await expect(overview).toContainText("Próxima tentativa");
  await expect(overview).toContainText("CONFIGURATION_INVALID");
  await expect(overview).toContainText("Tentativas sem sucesso: 2");
  await expect(
    overview.getByText("Publicação liberada", { exact: true }),
  ).toHaveCount(0);
  await tab(page, "Verificação");
  const panel = page.getByRole("tabpanel", {
    name: "Verificação",
    exact: true,
  });
  await expect(panel).toContainText(
    "A publicação aguarda a verificação automática.",
  );
  await expect(panel).toContainText("Token inválido ou sem permissão.");
  await expect(panel).toContainText(
    "Use esta ação para antecipar uma conferência.",
  );
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
});

test("conexão desativada apresenta renovação pausada sem promessa de retentativa", async ({
  page,
}) => {
  await open(
    page,
    {
      ...ready,
      can_publish: false,
      automatic_verification: {
        ...automatic,
        enabled: false,
        state: "disabled",
        next_attempt_at: null,
        message:
          "A verificação automática está pausada porque a conexão foi desativada.",
      },
    },
    { ...connection, state: "disabled" },
  );
  const overview = page.getByRole("tabpanel", {
    name: "Visão geral",
    exact: true,
  });
  await expect(overview).toContainText("Verificação automática desativada");
  await expect(overview).toContainText("Publicação bloqueada");
  await expect(overview).not.toContainText("Próxima tentativa");
  await expect(overview).not.toContainText("Próxima verificação");
});

test("motivo da verificação sobrevive às abas e ao cancelamento do fechamento", async ({
  page,
}) => {
  const dialog = await open(page);
  await tab(page, "Verificação");
  const reason = dialog.getByLabel("Motivo da verificação", { exact: true });
  await reason.fill("Conferência antes de publicar novos projetos");
  await tab(page, "Conexão");
  await expect(
    dialog.getByRole("combobox", { name: "Ação", exact: true }),
  ).toBeDisabled();
  await tab(page, "Verificação");
  await expect(reason).toHaveValue(
    "Conferência antes de publicar novos projetos",
  );
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByText("Descartar alterações não salvas?", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Continuar editando", exact: true })
    .click();
  await expect(reason).toHaveValue(
    "Conferência antes de publicar novos projetos",
  );
  await dialog
    .getByRole("button", { name: "Limpar motivo", exact: true })
    .click();
  await tab(page, "Conexão");
  await expect(
    dialog.getByRole("combobox", { name: "Ação", exact: true }),
  ).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("verificação mantém contrato, impede duplo envio e acompanha conclusão mesmo ao trocar de aba", async ({
  page,
}) => {
  const requests: {
    path: string;
    reason: string;
    version?: string;
    csrf?: string;
    key?: string;
  }[] = [];
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let verification = { ...ready.verification };
  await open(page);
  await page.unroute(diagnosticPath);
  let polls = 0;
  await page.route(diagnosticPath, (route) => {
    polls += 1;
    return route.fulfill({ json: { ...ready, verification } });
  });
  await page.route(
    "**/admin-api/v1/integration-connections/connection-test/verify-publication",
    async (route) => {
      const request = route.request();
      requests.push({
        path: new URL(request.url()).pathname,
        reason: request.postDataJSON().reason,
        version: request.headers()["if-match"],
        csrf: request.headers()["x-csrf-token"],
        key: request.headers()["idempotency-key"],
      });
      await pending;
      verification = {
        id: "check-current",
        state: "running",
        stage: "verification_building",
        error_code: null,
      };
      await route.fulfill({ status: 202, json: verification });
    },
  );
  await tab(page, "Verificação");
  await page
    .getByLabel("Motivo da verificação", { exact: true })
    .fill("Revalidar os perfis da conta de produção");
  const submit = page.getByRole("button", {
    name: "Verificar publicação",
    exact: true,
  });
  await submit.click();
  await expect(
    page.getByRole("button", { name: "Agendando…", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Fechar", exact: true }),
  ).toBeDisabled();
  await expect.poll(() => requests.length).toBe(1);
  await page
    .getByRole("button", { name: "Agendando…", exact: true })
    .evaluate((button) => (button as HTMLButtonElement).click());
  expect(requests[0]).toMatchObject({
    path: "/admin-api/v1/integration-connections/connection-test/verify-publication",
    reason: "Revalidar os perfis da conta de produção",
    version: "7",
    csrf: "csrf-test",
  });
  expect(requests[0].key).toBeTruthy();
  release();
  await expect(
    page.getByRole("button", { name: "Verificação em andamento", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByLabel("Motivo da verificação", { exact: true }),
  ).toHaveValue("");
  await tab(page, "Conexão");
  const before = polls;
  verification = {
    ...verification,
    state: "succeeded",
    stage: "verification_complete",
  };
  await expect.poll(() => polls, { timeout: 8000 }).toBeGreaterThan(before);
  await tab(page, "Visão geral");
  await expect(
    page
      .getByRole("tabpanel", { name: "Visão geral", exact: true })
      .getByText("Verificação concluída", { exact: true }),
  ).toBeVisible();
  expect(requests).toHaveLength(1);
});

test("retomada usa a operação existente mesmo com executor indisponível", async ({
  page,
}) => {
  let mutation: { path: string; version?: string; reason: string } | undefined;
  await page.route(
    "**/admin-api/v1/publication-checks/pending-check/resume",
    async (route) => {
      mutation = {
        path: new URL(route.request().url()).pathname,
        version: route.request().headers()["if-match"],
        reason: route.request().postDataJSON().reason,
      };
      await route.fulfill({
        status: 202,
        json: { id: "pending-check", state: "queued" },
      });
    },
  );
  await open(
    page,
    {
      ...ready,
      can_publish: false,
      can_verify: false,
      workers: [{ ...ready.workers[0], available: false }],
      verification: {
        id: "pending-check",
        state: "reconciling",
        stage: "verification_cleanup",
        error_code: "PUBLICATION_RESULT_UNKNOWN",
      },
    },
    { ...connection, state: "validated" },
  );
  await expect(
    page.getByRole("button", { name: "Revisar verificação", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Revisar verificação", exact: true })
    .click();
  await page
    .getByLabel("Motivo da verificação", { exact: true })
    .fill("Conferir o resultado da verificação pendente");
  await page
    .getByRole("button", { name: "Retomar verificação", exact: true })
    .click();
  await expect
    .poll(() => mutation)
    .toEqual({
      path: "/admin-api/v1/publication-checks/pending-check/resume",
      version: undefined,
      reason: "Conferir o resultado da verificação pendente",
    });
});

test("erro de diagnóstico remove prontidão obsoleta, preserva motivo e permite recuperação", async ({
  page,
}) => {
  await open(page);
  await tab(page, "Verificação");
  await page
    .getByLabel("Motivo da verificação", { exact: true })
    .fill("Conferência operacional da publicação");
  await page.unroute(diagnosticPath);
  await page.route(diagnosticPath, (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Diagnóstico temporariamente indisponível" } },
    }),
  );
  await page
    .getByRole("button", { name: "Atualizar diagnóstico", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Diagnóstico temporariamente indisponível",
  );
  await expect(
    page.getByRole("button", { name: "Verificar publicação", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByLabel("Motivo da verificação", { exact: true }),
  ).toHaveValue("Conferência operacional da publicação");
  await tab(page, "Visão geral");
  await expect(
    page.getByText("Consulta indisponível", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("tabpanel", { name: "Visão geral", exact: true })
      .getByText("Publicação liberada", { exact: true }),
  ).toBeHidden();
  await page.unroute(diagnosticPath);
  await page.route(diagnosticPath, (route) => route.fulfill({ json: ready }));
  await page
    .getByRole("button", { name: "Atualizar estado", exact: true })
    .click();
  await expect(
    page
      .getByRole("tabpanel", { name: "Visão geral", exact: true })
      .getByText("Publicação liberada", { exact: true }),
  ).toBeVisible();
  await tab(page, "Verificação");
  await expect(
    page.getByRole("button", { name: "Verificar publicação", exact: true }),
  ).toBeEnabled();
});

test("relatório formal mantém conteúdo entre abas e em erro de versão sem disparar verificação", async ({
  page,
}) => {
  const report = JSON.stringify({
    reason: "Revisão formal de evidências da configuração",
  });
  const mutations: string[] = [];
  await page.route(
    "**/admin-api/v1/integration-connections/connection-test/qualification",
    async (route) => {
      mutations.push(new URL(route.request().url()).pathname);
      expect(route.request().headers()["if-match"]).toBe("7");
      expect(route.request().postDataJSON()).toEqual(JSON.parse(report));
      await route.fulfill({
        status: 412,
        json: {
          error: { message: "A configuração mudou. Atualize antes de salvar." },
        },
      });
    },
  );
  await open(page);
  await tab(page, "Qualificação");
  await page
    .getByLabel("Carregar relatório JSON", { exact: true })
    .setInputFiles({
      name: "qualificacao.json",
      mimeType: "application/json",
      buffer: Buffer.from(report),
    });
  await expect(
    page.getByRole("textbox", { name: "Conteúdo do relatório", exact: true }),
  ).toHaveValue(report);
  await tab(page, "Verificação");
  await expect(
    page.getByLabel("Motivo da verificação", { exact: true }),
  ).toBeDisabled();
  await tab(page, "Qualificação");
  await expect(
    page.getByRole("textbox", { name: "Conteúdo do relatório", exact: true }),
  ).toHaveValue(report);
  await page
    .getByRole("button", {
      name: "Registrar resultados e revisão responsável",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toContainText("A configuração mudou");
  await expect(
    page.getByRole("textbox", { name: "Conteúdo do relatório", exact: true }),
  ).toHaveValue(report);
  await page
    .getByRole("button", { name: "Limpar relatório", exact: true })
    .click();
  await expect(
    page.getByRole("textbox", { name: "Conteúdo do relatório", exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByLabel("Carregar relatório JSON", { exact: true }),
  ).toHaveValue("");
  expect(mutations).toEqual([
    "/admin-api/v1/integration-connections/connection-test/qualification",
  ]);
});

for (const [action, label, state] of [
  ["validate", "Validar conexão", "draft"],
  ["activate", "Ativar conexão", "validated"],
  ["default", "Definir como padrão", "active"],
  ["disable", "Desativar conexão", "active"],
]) {
  test(`ação ${action} preserva endpoint, motivo, versão e feedback`, async ({
    page,
  }) => {
    const requests: unknown[] = [];
    await page.route(
      `**/admin-api/v1/integrations/cloudflare/connections/connection-test/${action}`,
      async (route) => {
        requests.push({
          method: route.request().method(),
          body: route.request().postDataJSON(),
          version: route.request().headers()["if-match"],
        });
        await route.fulfill({ json: connection });
      },
    );
    await open(page, ready, { ...connection, state, is_default: false });
    await tab(page, "Conexão");
    await page
      .getByRole("combobox", { name: "Ação", exact: true })
      .selectOption(action);
    await page
      .getByLabel("Motivo da ação", { exact: true })
      .fill("Ajuste operacional revisado da conexão");
    await page.getByRole("button", { name: label, exact: true }).click();
    await expect(
      page.getByText("Ação concluída. O estado da conexão foi atualizado.", {
        exact: true,
      }),
    ).toBeVisible();
    expect(requests).toEqual([
      {
        method: "POST",
        body: { reason: "Ajuste operacional revisado da conexão" },
        version: "7",
      },
    ]);
  });
}

test("consulta com timeout desabilita a verificação, recupera automaticamente e cancela polling ao fechar", async ({
  page,
}) => {
  await page.clock.install();
  await open(page);
  await page.unroute(diagnosticPath);
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let polls = 0;
  await page.route(diagnosticPath, async (route) => {
    polls += 1;
    if (polls === 1) await pending;
    await route.fulfill({ json: ready }).catch(() => {});
  });
  await tab(page, "Verificação");
  await page
    .getByLabel("Motivo da verificação", { exact: true })
    .fill("Verificação após demora na comunicação");
  await page
    .getByRole("button", { name: "Atualizar diagnóstico", exact: true })
    .click();
  await expect.poll(() => polls).toBe(1);
  await page.clock.runFor(12001);
  await expect(page.getByRole("alert")).toContainText(
    "O diagnóstico demorou para responder",
  );
  await expect(
    page.getByRole("button", { name: "Verificar publicação", exact: true }),
  ).toBeDisabled();
  release();
  await page.clock.runFor(4001);
  await expect(
    page.getByRole("button", { name: "Verificar publicação", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByLabel("Motivo da verificação", { exact: true }),
  ).toHaveValue("Verificação após demora na comunicação");
  await page
    .getByRole("button", { name: "Limpar motivo", exact: true })
    .click();
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  const closedAt = polls;
  await page.clock.runFor(20000);
  expect(polls).toBe(closedAt);
});

test("aprovação parcial informa limites e verificação em andamento tem prioridade sobre ativação", async ({
  page,
}) => {
  await open(page, {
    ...ready,
    stack_readiness: [
      ...ready.stack_readiness,
      {
        stack: "fastapi-python-d1",
        can_publish: false,
        reasons: [
          {
            code: "CAPABILITY_UNVALIDATED",
            message: "Perfil Python ainda não aprovado",
          },
        ],
      },
    ],
    verification: {
      ...ready.verification,
      state: "failed",
      stage: "verification_partial",
      stack_results: [
        { stack: "hono-typescript-d1", passed: true },
        { stack: "fastapi-python-d1", passed: false },
      ],
    },
  });
  const overview = page.getByRole("tabpanel", {
    name: "Visão geral",
    exact: true,
  });
  await expect(
    overview.getByText("Publicação parcialmente liberada", { exact: true }),
  ).toBeVisible();
  await expect(
    overview.getByText("Verificação concluída com aprovação parcial", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.unroute(
    "**/admin-api/v1/integrations/cloudflare/connections/connection-test",
  );
  await page.route(
    "**/admin-api/v1/integrations/cloudflare/connections/connection-test",
    (route) => route.fulfill({ json: { ...connection, state: "validated" } }),
  );
  await page.unroute(diagnosticPath);
  await page.route(diagnosticPath, (route) =>
    route.fulfill({
      json: {
        ...ready,
        verification: {
          ...ready.verification,
          state: "running",
          stage: "verification_remote",
        },
      },
    }),
  );
  await page
    .getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Acompanhar verificação", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Ativar conexão", exact: true }),
  ).toHaveCount(0);
});
