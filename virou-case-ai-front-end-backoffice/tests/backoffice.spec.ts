import { expect, test, type Page } from "@playwright/test";
import { generateKeyPairSync } from "node:crypto";
import AxeBuilder from "@axe-core/playwright";

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await expect(
    page.getByRole("heading", { name: "Contas e acesso" }),
  ).toBeVisible();
}

test("senha gerada, cadastro, ativação e ajuste do prazo preservam o saldo", async ({
  page,
  context,
}, info) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await login(page);
  await page
    .getByRole("button", { name: "Criar usuário", exact: true })
    .click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome", { exact: true }).fill("Aluno prazo E2E");
  await dialog.getByLabel("Login", { exact: true }).fill("access-duration-e2e");
  await dialog.getByRole("button", { name: "Gerar senha" }).click();
  const password = await dialog
    .getByLabel("Senha inicial / nova senha", { exact: true })
    .inputValue();
  expect(password).toHaveLength(24);
  expect(password).toMatch(/[A-Z]/);
  expect(password).toMatch(/[a-z]/);
  expect(password).toMatch(/[0-9]/);
  expect(password).toMatch(/[^a-zA-Z0-9]/);
  await dialog.getByRole("button", { name: "Copiar senha" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    password,
  );
  await page.screenshot({
    path: info.outputPath("create-user.png"),
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "Revisar operação" }).click();
  await expect(dialog).not.toContainText(password);
  await dialog.getByRole("button", { name: "Revisar campos" }).click();
  await expect(dialog.getByLabel("Nome", { exact: true })).toHaveValue(
    "Aluno prazo E2E",
  );
  await expect(
    dialog.getByLabel("Senha inicial / nova senha", { exact: true }),
  ).toHaveValue(password);
  await dialog.getByRole("button", { name: "Revisar operação" }).click();
  await dialog.getByRole("button", { name: "Confirmar e registrar" }).click();
  await expect(dialog).toHaveCount(0);
  const account = page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: "Aluno prazo E2E" }) });
  await account
    .getByRole("button", { name: "Ativar / renovar", exact: true })
    .click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("Período de acesso (dias)").fill("45");
  await page.screenshot({
    path: info.outputPath("activate-access.png"),
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "Revisar operação" }).click();
  await expect(dialog).toContainText("Dias de acesso");
  await dialog.getByRole("button", { name: "Confirmar e registrar" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(account).toContainText("Acesso ativo");
  await expect(account).toContainText("Uso da plataforma até");
  await account
    .getByRole("button", { name: "Ajustes da conta", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Período de acesso", exact: true })
    .click();
  const date = new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 16);
  await page.getByLabel("Uso da plataforma até", { exact: true }).fill(date);
  await page.getByRole("button", { name: "Revisar operação" }).click();
  await page.getByRole("button", { name: "Confirmar e registrar" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await account.getByRole("button", { name: "Ver saldo" }).click();
  await expect(page.getByRole("dialog")).toContainText("50,00000000");
});

test("IA: configurar, descobrir, testar, habilitar e desabilitar um modelo", async ({
  page,
}, info) => {
  await login(page);
  await page
    .getByRole("button", { name: "Agentes de IA", exact: true })
    .click();
  await page.getByRole("button", { name: "Nova conexão", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome da conexão").fill("OpenAI contrato E2E");
  await dialog
    .getByLabel("API key", { exact: true })
    .fill("synthetic-e2e-credential-no-real-key");
  await dialog.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "Adicionar modelo" }).click();
  dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Consultar modelos do provedor" })
    .click();
  await expect(dialog).toContainText("1 modelo(s) retornados pelo provedor");
  await dialog
    .getByLabel("Identificador do modelo")
    .fill("synthetic-text-model");
  await dialog
    .getByLabel("Nome para identificação")
    .fill("Modelo principal E2E");
  await dialog.getByLabel("Entrada (USD)", { exact: true }).fill("0.1");
  await dialog.getByLabel("Entrada em cache (USD)").fill("0.01");
  await dialog.getByLabel("Saída (USD)").fill("0.5");
  await dialog.getByLabel("Câmbio (BRL por USD)").fill("5");
  await dialog.getByLabel("Fonte do câmbio").fill("Cotação sintética de teste");
  await dialog
    .getByLabel("Referência dos preços")
    .fill("Tabela sintética de teste");
  await dialog
    .getByLabel("Fim da vigência")
    .fill(new Date(Date.now() + 86400000).toISOString().slice(0, 16));
  await expect(
    dialog.getByLabel("Habilitar para uso na plataforma"),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(dialog).toHaveCount(0);
  await page
    .getByRole("button", { name: "Testar geração", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirmar teste com possível cobrança" })
    .click();
  await expect(page.getByRole("status")).toContainText("Teste concluído");
  await page.getByRole("button", { name: "Editar modelo" }).click();
  await page.getByLabel("Habilitar para uso na plataforma").check();
  await page.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(
    page.getByText("Pronto para uso", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("ai-ready.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Editar modelo" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: "Personalizar execução" }).check();
  await dialog
    .getByLabel("Tempo máximo da chamada (segundos)", { exact: true })
    .fill("240");
  await dialog
    .getByLabel("Tempo máximo de conexão (segundos)", { exact: true })
    .fill("15");
  await dialog
    .getByLabel("Limite de tokens de saída", { exact: true })
    .fill("32000");
  await expect(
    dialog.getByRole("combobox", { name: /^Amostragem/ }).locator("option"),
  ).toHaveCount(1);
  await expect(
    dialog
      .getByRole("combobox", { name: /^Controle de raciocínio/ })
      .locator("option"),
  ).toHaveCount(2);
  const executionWrite = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      /\/models\/[^/]+$/.test(new URL(request.url()).pathname),
  );
  await dialog.getByRole("button", { name: "Salvar configuração" }).click();
  const executionRequest = await executionWrite;
  expect(executionRequest.postDataJSON()).toMatchObject({
    enabled: false,
    max_output_tokens: 32000,
    execution: {
      request_timeout_seconds: 240,
      connect_timeout_seconds: 15,
      temperature: null,
      top_p: null,
      thinking_mode: "auto",
      thinking_budget: null,
      thinking_level: null,
      reasoning_effort: null,
    },
  });
  expect(executionRequest.headers()["idempotency-key"]).toBeTruthy();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("Teste necessário", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Contas e acesso" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Agentes de IA", exact: true })
    .click();
  await page.getByRole("button", { name: "Editar modelo" }).click();
  dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("checkbox", { name: "Personalizar execução" }),
  ).toBeChecked();
  await expect(
    dialog.getByLabel("Tempo máximo da chamada (segundos)", { exact: true }),
  ).toHaveValue("240");
  await expect(
    dialog.getByLabel("Tempo máximo de conexão (segundos)", { exact: true }),
  ).toHaveValue("15");
  await expect(
    dialog.getByLabel("Limite de tokens de saída", { exact: true }),
  ).toHaveValue("32000");
  await expect(
    dialog.getByLabel("Habilitar para uso na plataforma"),
  ).toBeDisabled();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await dialog
      .getByLabel("Limite de tokens de saída", { exact: true })
      .scrollIntoViewIfNeeded();
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath(`model-execution-${width}.png`),
      fullPage: true,
    });
  }
  expect(
    (
      await new AxeBuilder({ page })
        .include(".ai-dialog")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await dialog.getByRole("checkbox", { name: "Personalizar execução" }).focus();
  await page.keyboard.press("Tab");
  await expect(
    dialog.getByLabel("Tempo máximo da chamada (segundos)", { exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "Testar geração", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirmar teste com possível cobrança" })
    .click();
  await expect(page.getByRole("status")).toContainText("Teste concluído");
  await page.getByRole("button", { name: "Editar modelo" }).click();
  await page.getByLabel("Habilitar para uso na plataforma").check();
  await page.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(
    page.getByText("Pronto para uso", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Editar conexão" }).click();
  await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
  await page.getByLabel("Conexão habilitada").uncheck();
  await page.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(
    page.getByText("Conexão desabilitada", { exact: true }),
  ).toBeVisible();
});

test("Google Service Account: catálogo local, recusa segura, configuração manual e geração", async ({
  page,
}, info) => {
  await login(page);
  await page
    .getByRole("button", { name: "Agentes de IA", exact: true })
    .click();
  await page.getByRole("button", { name: "Nova conexão", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome da conexão").fill("Google catálogo E2E");
  await dialog.getByLabel("Provedor", { exact: true }).selectOption("gemini");
  await dialog
    .getByLabel("Autenticação", { exact: true })
    .selectOption("service_account");
  const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  await dialog.getByLabel("Credencial JSON", { exact: true }).fill(
    JSON.stringify({
      type: "service_account",
      project_id: "synthetic-project",
      private_key_id: "synthetic-key-id",
      private_key: privateKey
        .export({ type: "pkcs8", format: "pem" })
        .toString(),
      client_email: "e2e@synthetic-project.iam.gserviceaccount.com",
      token_uri: "https://oauth2.googleapis.com/token",
    }),
  );
  await dialog.getByLabel("Project ID").fill("synthetic-project");
  await dialog.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(dialog).toHaveCount(0);
  const connection = page.locator("article").filter({
    has: page.getByRole("heading", { name: "Google catálogo E2E" }),
  });
  let remoteRequests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/discover")) remoteRequests++;
  });
  await connection.getByRole("button", { name: "Adicionar modelo" }).click();
  dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("46 referências disponíveis");
  expect(remoteRequests).toBe(0);
  await dialog.getByLabel("Buscar no catálogo").fill("gemini-2.5-flash");
  await dialog
    .getByLabel("Selecionar modelo do catálogo")
    .selectOption("gemini-2.5-flash");
  await expect(dialog.getByLabel("Identificador do modelo")).toHaveValue(
    "gemini-2.5-flash",
  );
  await dialog.getByLabel("Nome para identificação").fill("Google manual E2E");
  await dialog.getByLabel("Entrada (USD)", { exact: true }).fill("0.1");
  await dialog.getByLabel("Entrada em cache (USD)").fill("0.01");
  await dialog.getByLabel("Saída (USD)").fill("0.5");
  await dialog.getByLabel("Câmbio (BRL por USD)").fill("5");
  await dialog.getByLabel("Fonte do câmbio").fill("Cotação sintética de teste");
  await dialog
    .getByLabel("Referência dos preços")
    .fill("Tabela sintética de teste");
  await dialog
    .getByLabel("Fim da vigência")
    .fill(new Date(Date.now() + 86400000).toISOString().slice(0, 16));
  await dialog
    .getByRole("button", { name: "Consultar modelos do provedor" })
    .click();
  await expect(dialog.getByRole("alert")).toContainText("SERVICE_DISABLED");
  await expect(dialog.getByRole("alert")).toContainText("HTTP 403");
  await expect(dialog).not.toContainText("synthetic-private-project-debug");
  await expect(dialog.getByLabel("Nome para identificação")).toHaveValue(
    "Google manual E2E",
  );
  await expect(dialog.getByLabel("Entrada (USD)", { exact: true })).toHaveValue(
    "0.1",
  );
  await expect(
    dialog.getByLabel("Habilitar para uso na plataforma"),
  ).toBeDisabled();
  await dialog.getByLabel("Buscar no catálogo").fill("future-unlisted-model");
  await expect(dialog).toContainText("Nenhuma referência encontrada");
  await dialog
    .getByLabel("Identificador do modelo")
    .fill("future-unlisted-model");
  await expect(dialog.getByLabel("Identificador do modelo")).toHaveValue(
    "future-unlisted-model",
  );
  await dialog.getByLabel("Identificador do modelo").fill("gemini-2.5-flash");
  await dialog.getByRole("checkbox", { name: "Personalizar execução" }).check();
  await dialog
    .getByLabel("Limite de tokens de saída", { exact: true })
    .fill("65000");
  await dialog
    .getByRole("combobox", { name: /^Amostragem/ })
    .selectOption("temperature");
  await dialog.getByLabel("Temperatura", { exact: true }).fill("0.2");
  await dialog
    .getByRole("combobox", { name: /^Controle de raciocínio/ })
    .selectOption("budget");
  const budget = dialog.getByLabel("Orçamento de raciocínio (tokens)", {
    exact: true,
  });
  const maximumBudget = Number(await budget.getAttribute("max"));
  await budget.fill(String(maximumBudget + 1));
  expect(
    await budget.evaluate((element: HTMLInputElement) =>
      element.checkValidity(),
    ),
  ).toBe(false);
  await budget.fill("0");
  expect(
    await budget.evaluate((element: HTMLInputElement) =>
      element.checkValidity(),
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("google-catalog-fallback.png"),
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(connection).toContainText("Teste necessário");
  await connection
    .getByRole("button", { name: "Testar geração", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirmar teste com possível cobrança" })
    .click();
  await expect(page.getByRole("status")).toContainText("Teste concluído");
  await connection.getByRole("button", { name: "Editar modelo" }).click();
  await expect(page.getByLabel("Nome para identificação")).toHaveValue(
    "Google manual E2E",
  );
  await expect(
    page.getByLabel("Limite de tokens de saída", { exact: true }),
  ).toHaveValue("65000");
  await expect(page.getByLabel("Temperatura", { exact: true })).toHaveValue(
    "0.2",
  );
  await expect(
    page.getByLabel("Orçamento de raciocínio (tokens)", { exact: true }),
  ).toHaveValue("0");
  await page.getByLabel("Habilitar para uso na plataforma").check();
  await page.getByRole("button", { name: "Salvar configuração" }).click();
  await expect(connection).toContainText("Pronto para uso");
  expect(remoteRequests).toBe(1);
});

for (const width of [320, 375, 768, 1024, 1440]) {
  test(`formulários legíveis e sem rolagem horizontal em ${width}px`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page);
    await page
      .getByRole("button", { name: "Criar usuário", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Gerar senha" }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    await page.keyboard.press("Escape");
    await page
      .getByRole("button", { name: "Agentes de IA", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Nova conexão", exact: true })
      .click();
    await page.getByLabel("Provedor", { exact: true }).selectOption("gemini");
    await page
      .getByLabel("Autenticação", { exact: true })
      .selectOption("service_account");
    await expect(
      page.getByLabel("Credencial JSON", { exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    await page.screenshot({
      path: info.outputPath(`google-service-account-${width}.png`),
      fullPage: true,
    });
  });
}
