import { expect, test, type Page, type BrowserContext } from "@playwright/test";

let authentication: Awaited<ReturnType<BrowserContext["storageState"]>>;
test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:5175/");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await expect(
    page.getByRole("button", { name: "Integrações e templates", exact: true }),
  ).toBeVisible();
  authentication = await context.storageState();
  await context.close();
});

async function openIntegrations(page: Page, provider = "cloudflare") {
  await page.context().addCookies(authentication.cookies);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Integrações e templates", exact: true })
    .click();
  if (provider !== "cloudflare")
    await page
      .getByRole("button", {
        name: provider === "jev" ? "Fluxo guiado e Jev" : "Templates",
        exact: true,
      })
      .click();
}

async function createJev(page: Page, name: string, limit: string) {
  await page
    .getByRole("button", { name: "Nova conexão Jev", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome da conexão", { exact: true }).fill(name);
  await dialog
    .getByLabel("Limite de caracteres da ideia", { exact: true })
    .fill(limit);
  await dialog
    .getByLabel("Token da integração", { exact: true })
    .fill("synthetic-only-jev-" + name);
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Teste de múltiplas conexões sem chamada externa");
  await dialog
    .getByRole("button", { name: "Cadastrar conexão", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("row", { name: new RegExp(name) })).toBeVisible();
}

async function manage(page: Page, name: string, action: string, state: string) {
  await page
    .getByRole("button", { name: "Gerenciar " + name, exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("combobox", { name: "Ação", exact: true })
    .selectOption(action);
  await dialog
    .getByLabel("Motivo da ação", { exact: true })
    .fill("Teste de ação local por conexão");
  await dialog.locator("form button[type=submit], form button.primary").click();
  await expect(
    dialog.getByText("Ação concluída. O estado da conexão foi atualizado."),
  ).toBeVisible();
  await expect(dialog.locator(".integration-summary")).toContainText(state);
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

test("duas conexões Jev: cadastro, ativação, padrão, edição isolada, filtros e recarga", async ({
  page,
}) => {
  await openIntegrations(page, "jev");
  await createJev(page, "Jev equipe A", "1800");
  await createJev(page, "Jev equipe B", "2200");
  for (const name of ["Jev equipe A", "Jev equipe B"]) {
    await manage(page, name, "validate", "Validada");
    await manage(page, name, "activate", "Ativa");
  }
  await manage(page, "Jev equipe B", "default", "Sim");
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toContainText(
    "Padrão",
  );
  await expect(
    page.getByRole("row", { name: /Jev equipe A/ }),
  ).not.toContainText("Padrão");
  expect(
    (await (await page.request.get("/admin-api/v1/integrations/jev")).json())
      .settings.max_objective_chars,
  ).toBe(2200);
  await page
    .getByRole("button", { name: "Editar Jev equipe A", exact: true })
    .click();
  let dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Substituir token (deixe vazio para preservar)", {
      exact: true,
    }),
  ).toHaveValue("");
  await expect(
    dialog.getByLabel("Limite de caracteres da ideia", { exact: true }),
  ).toHaveValue("1800");
  await dialog
    .getByLabel("Limite de caracteres da ideia", { exact: true })
    .fill("1900");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Atualizar somente os limites da conta A");
  await dialog
    .getByRole("button", { name: "Salvar nova versão", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Jev equipe A/ })).toContainText(
    "Ativa",
  );
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toContainText(
    "Ativa",
  );
  expect(
    (await (await page.request.get("/admin-api/v1/integrations/jev")).json())
      .settings.max_objective_chars,
  ).toBe(2200);
  await page
    .getByRole("combobox", { name: "Estado", exact: true })
    .selectOption("active");
  await expect(page.getByRole("row", { name: /Jev equipe A/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toBeVisible();
  await page
    .getByRole("combobox", { name: "Estado", exact: true })
    .selectOption("draft");
  await expect(page.getByRole("row", { name: /Jev equipe A/ })).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toHaveCount(0);
  await page
    .getByRole("combobox", { name: "Estado", exact: true })
    .selectOption("");
  await page.getByLabel("Buscar conexões", { exact: true }).fill("nao-existe");
  await expect(
    page.getByText("Nenhuma conexão corresponde aos filtros"),
  ).toBeVisible();
  await page.getByLabel("Buscar conexões", { exact: true }).fill("equipe B");
  await page.getByRole("button", { name: "Atualizar", exact: true }).click();
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "Integrações e templates", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Fluxo guiado e Jev", exact: true })
    .click();
  await expect(page.getByRole("row", { name: /Jev equipe B/ })).toContainText(
    "Padrão",
  );
  await expect(page.getByRole("row", { name: /Jev equipe A/ })).toContainText(
    "Ativa",
  );
  await page
    .getByRole("button", { name: "Editar Jev equipe A", exact: true })
    .click();
  dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Limite de caracteres da ideia", { exact: true }),
  ).toHaveValue("1900");
  await expect(
    dialog.getByLabel("Substituir token (deixe vazio para preservar)", {
      exact: true,
    }),
  ).toHaveValue("");
});

test("duas contas Cloudflare e erro de edição preservam campos e segredo somente de escrita", async ({
  page,
}) => {
  await openIntegrations(page);
  for (const [label, account] of [
    ["Cloudflare produção", "a"],
    ["Cloudflare laboratório", "b"],
  ]) {
    await page
      .getByRole("button", { name: "Nova conexão Cloudflare", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    for (const [name, value] of [
      ["Nome da conexão", label],
      ["Account ID Cloudflare", account.repeat(32)],
      ["Ambiente", "homologacao"],
      ["Prefixo dos projetos", "lab"],
      ["Data de compatibilidade", "2026-10-27"],
      ["Projetos hospedados na conta (máximo 10)", "5"],
      ["Publicações simultâneas", "2"],
      ["Validade da validação (horas)", "24"],
      ["Timeout Cloudflare (segundos)", "10"],
      ["Token da integração", "synthetic-only-cloudflare-" + account],
      ["Motivo da alteração", "Cadastro de teste sem publicação externa"],
    ])
      await dialog.getByLabel(name, { exact: true }).fill(value);
    await dialog
      .getByRole("combobox", { name: "Tipo do token", exact: true })
      .selectOption("user");
    await dialog
      .getByRole("combobox", { name: "Plano permitido", exact: true })
      .selectOption("free");
    await dialog.getByLabel("Hono / TypeScript / D1", { exact: true }).check();
    await dialog
      .getByRole("button", { name: "Cadastrar conexão", exact: true })
      .click();
    await expect(dialog).toHaveCount(0);
  }
  await expect(
    page.getByRole("row", { name: /Cloudflare produção/ }),
  ).toContainText("a".repeat(32));
  await expect(
    page.getByRole("row", { name: /Cloudflare laboratório/ }),
  ).toContainText("b".repeat(32));
  await page
    .getByRole("button", { name: "Editar Cloudflare produção", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Substituir token (deixe vazio para preservar)", {
      exact: true,
    }),
  ).toHaveValue("");
  await dialog.getByLabel("Ambiente", { exact: true }).fill("producao");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Ajuste do ambiente de produção");
  await page.route(
    "**/admin-api/v1/integrations/cloudflare/connections/*",
    async (route) => {
      if (route.request().method() === "PUT")
        await route.fulfill({
          status: 412,
          contentType: "application/json",
          body: JSON.stringify({
            error: {
              message: "A configuração mudou. Atualize antes de salvar.",
            },
          }),
        });
      else await route.continue();
    },
    { times: 1 },
  );
  await dialog
    .getByRole("button", { name: "Salvar nova versão", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText("A configuração mudou");
  await expect(dialog.getByLabel("Ambiente", { exact: true })).toHaveValue(
    "producao",
  );
  await dialog
    .getByRole("button", { name: "Salvar nova versão", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("row", { name: /Cloudflare produção/ }),
  ).toContainText("producao");
  await expect(
    page.getByRole("row", { name: /Cloudflare laboratório/ }),
  ).toContainText("homologacao");
  const response = await page.request.get(
    "/admin-api/v1/integrations/cloudflare/connections",
  );
  expect(await response.text()).not.toContain("synthetic-only-cloudflare");
  await page
    .getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true })
    .click();
  await dialog.getByRole("tab", { name: "Qualificação", exact: true }).click();
  await expect(
    dialog.getByLabel("Carregar relatório JSON", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", {
      name: "Registrar resultados e revisão responsável",
    }),
  ).toBeDisabled();
});

test("templates em tabela: criação com campos pessoais, edição, revogação e paginação", async ({
  page,
}) => {
  await openIntegrations(page, "templates");
  await page
    .getByRole("button", { name: "Novo template", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Título", { exact: true })
    .fill("Template da equipe E2E");
  await dialog.getByLabel("Identificador", { exact: true }).fill("equipe-e2e");
  await dialog
    .getByRole("combobox", { name: "Categoria", exact: true })
    .selectOption("quality");
  await dialog
    .getByLabel("Descrição", { exact: true })
    .fill("Template criado no modal com campos pessoais.");
  await dialog
    .getByLabel("Conteúdo", { exact: true })
    .fill(
      "Critérios de teste para {{objetivo}}. Preserve o comportamento atual.",
    );
  await dialog
    .getByRole("button", { name: "Adicionar campo", exact: true })
    .click();
  await dialog.getByLabel("Chave do campo 1", { exact: true }).fill("objetivo");
  await dialog
    .getByLabel("Nome exibido do campo 1", { exact: true })
    .fill("Objetivo do projeto");
  await dialog.getByLabel("Campo 1 obrigatório", { exact: true }).check();
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Criação de template de teste E2E");
  await dialog
    .getByRole("button", { name: "Cadastrar template", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page.getByLabel("Buscar templates", { exact: true }).fill("equipe-e2e");
  await expect(
    page.getByRole("row", { name: /Template da equipe E2E/ }),
  ).toContainText("v1");
  await page
    .getByRole("button", { name: "Editar Template da equipe E2E", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Chave do campo 1", { exact: true }),
  ).toHaveValue("objetivo");
  await expect(
    dialog.getByLabel("Identificador", { exact: true }),
  ).toHaveAttribute("readonly", "");
  await dialog
    .getByLabel("Disponível para novos envios", { exact: true })
    .uncheck();
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Revogar novos envios e preservar histórico");
  await dialog
    .getByRole("button", { name: "Salvar nova versão", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("row", { name: /Template da equipe E2E/ }),
  ).toContainText("v2");
  await expect(
    page.getByRole("row", { name: /Template da equipe E2E/ }),
  ).toContainText("Desativado");
  await page
    .getByRole("combobox", { name: "Disponibilidade", exact: true })
    .selectOption("true");
  await expect(
    page.getByText("Nenhum template encontrado", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Buscar templates", { exact: true }).fill("");
  await page
    .getByRole("combobox", { name: "Disponibilidade", exact: true })
    .selectOption("");
  await page.getByRole("button", { name: "Próxima", exact: true }).click();
  await expect(page.locator(".integration-pagination")).toContainText(
    "Página 2",
  );
  await page
    .getByRole("combobox", { name: "Filtrar categoria", exact: true })
    .selectOption("quality");
  await expect(page.locator(".integration-pagination")).toContainText(
    "Página 1",
  );
});

test("modal acessível, cancelamento sem perda e layout de 320 a 1440 pixels", async ({
  page,
}) => {
  await openIntegrations(page, "jev");
  await page
    .getByRole("button", { name: "Nova conexão Jev", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Nome da conexão", { exact: true }),
  ).toBeFocused();
  await dialog
    .getByLabel("Nome da conexão", { exact: true })
    .fill("Cadastro em andamento");
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByText("Descartar alterações não salvas?", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Continuar editando", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Nome da conexão", { exact: true }),
  ).toHaveValue("Cadastro em andamento");
  for (const width of [1440, 768, 640, 375, 320]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect(
      await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth),
    ).toBe(true);
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((el) => el.contains(document.activeElement)),
    ).toBe(true);
  }
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Descartar e fechar", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Nova conexão Jev", exact: true }),
  ).toBeFocused();
  for (const width of [1440, 640, 320]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({
    path: "test-results/integrations-desktop.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Nova conexão Jev", exact: true })
    .click();
  await page.screenshot({ path: "test-results/integration-modal-desktop.png" });
  await page.setViewportSize({ width: 375, height: 900 });
  await page.screenshot({ path: "test-results/integration-modal-mobile.png" });
});
