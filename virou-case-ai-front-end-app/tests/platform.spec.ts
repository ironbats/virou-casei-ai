import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("student-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-student-only-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Meus projetos" }),
  ).toBeVisible();
}

test("home, oferta e contato não criam cadastro", async ({ page }, info) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: /Aprenda com IA/ }),
  ).toBeVisible();
  await page.screenshot({ path: info.outputPath("home.png"), fullPage: true });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page
    .getByRole("link", { name: /Conheça agora/ })
    .first()
    .click();
  await expect(page.getByText("R$ 75,00")).toBeVisible();
  await expect(
    page.getByRole("link", { name: /Participar da experimentação/ }),
  ).toHaveAttribute("href", "https://wa.me/5511941810735");
  await expect(page.locator("input")).toHaveCount(0);
});

test("guia real, filtro, template copiável e ciclo enxuto", async ({
  page,
  context,
}, info) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await login(page);
  await page.getByRole("link", { name: "Aprender", exact: true }).click();
  await expect(page.locator(".guide-card")).toHaveCount(14);
  await page.screenshot({
    path: info.outputPath("learning.png"),
    fullPage: true,
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.getByRole("button", { name: "Segurança", exact: true }).click();
  await expect(page.locator(".guide-card")).toHaveCount(5);
  await page.getByRole("button", { name: "Todos", exact: true }).click();
  await page.getByLabel("Buscar guias").fill("SDD");
  await page.getByRole("link", { name: "Ver guia" }).click();
  await page.getByRole("tab", { name: "2 Adapte" }).click();
  await page
    .getByLabel("Objetivo do projeto")
    .fill("Criar uma lista de tarefas completa para o time");
  await page.getByLabel("Para quem é o projeto?").fill("Equipe de operações");
  await page
    .getByLabel("Critérios de aceite")
    .fill("Cadastrar uma tarefa e marcá-la como concluída");
  await page.getByRole("tab", { name: "3 Aplique" }).click();
  await page.getByRole("button", { name: "Copiar template" }).click();
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  expect(clipboard.length).toBeGreaterThan(150);
  await page.getByRole("link", { name: "Uso e acesso", exact: true }).click();
  await expect(page.getByText("Dia 32", { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("usage.png"), fullPage: true });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await expect(page.getByText("R$ 50")).toHaveCount(0);
  await expect(
    page.getByText("Baixe seus projetos antes do dia 32"),
  ).toHaveCount(0);
});

test("criação, edição, persistência, export e gate de publicação", async ({
  page,
}, info) => {
  await login(page);
  await page.getByRole("button", { name: "Novo projeto", exact: true }).click();
  await page.getByLabel("Nome do projeto").fill("Projeto E2E");
  await page
    .getByLabel("O que você quer construir?")
    .fill("Criar uma aplicação funcional para minha ideia de produto");
  await page
    .getByRole("button", { name: "Criar projeto", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Projeto E2E", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("workspace.png"),
    fullPage: true,
  });
  await page.getByRole("treeitem", { name: "app.js", exact: true }).click();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  const editor = page.locator(".code-editor");
  const old = await editor.inputValue();
  await editor.fill(old + "\nconst savedByE2E = true;\n");
  await page.getByRole("button", { name: "Salvar revisão" }).click();
  await expect(page.getByText("Revisão salva.", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("treeitem", { name: "app.js", exact: true }).click();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  await expect(editor).toHaveValue(/savedByE2E/);
  await editor.fill("Edição local ainda não salva");
  const projectUrl = page.url();
  await page.getByRole("link", { name: "Aprender", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Aprender e aplicar", exact: true }),
  ).toBeVisible();
  await page.goto(projectUrl);
  await page.getByRole("treeitem", { name: "app.js", exact: false }).click();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  await expect(editor).toHaveValue("Edição local ainda não salva");
  await page
    .getByRole("button", { name: "Baixar projeto", exact: true })
    .click();
  await expect(page.getByText(/O ZIP contém a revisão salva/)).toBeVisible();
  await page
    .getByRole("button", { name: "Baixar revisão salva", exact: true })
    .click();
  const download = page.getByRole("link", { name: /Baixar ZIP/ }).first();
  await expect(download).toBeVisible({ timeout: 15000 });
  const pending = page.waitForEvent("download");
  await download.click();
  const file = await pending;
  expect(file.suggestedFilename()).toMatch(/\.zip$/);
  await expect(
    page.getByRole("button", { name: "Publicar", exact: true }),
  ).toBeDisabled();
  await page.goto(projectUrl + "/publicar");
  await expect(
    page.getByRole("button", { name: "Publicar projeto", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText(/Publicação bloqueada/)).toBeVisible();
});

test("layouts responsivos sem overflow horizontal", async ({ page }, info) => {
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    if (width === 375)
      await page.screenshot({
        path: info.outputPath("home-mobile.png"),
        fullPage: true,
      });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    );
    expect(overflow, `Home em ${width}px`).toBe(false);
  }
  await login(page);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/app/aprendizado");
    await expect(
      page.getByRole("heading", { name: "Aprender e aplicar", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth + 1,
      ),
      `Aprender em ${width}px`,
    ).toBe(false);
    await page.goto("/app/consumo");
    await expect(page.getByText("Dia 32", { exact: true })).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth + 1,
      ),
      `Uso em ${width}px`,
    ).toBe(false);
  }
});

test("backoffice provisiona e confirma acesso manual", async ({ page }) => {
  await page.goto("http://127.0.0.1:5174");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await page
    .getByRole("button", { name: "Criar usuário", exact: true })
    .click();
  await page
    .getByLabel("Nome", { exact: true })
    .fill("Conta criada no navegador");
  await page.getByLabel("Login", { exact: true }).fill("browser-test");
  await page
    .getByLabel("Senha inicial / nova senha")
    .fill("browser-test-password");
  await page.getByLabel("Motivo").fill("Provisionamento para teste integrado");
  await page.getByRole("button", { name: "Revisar operação" }).click();
  await page.getByRole("button", { name: "Confirmar e registrar" }).click();
  const account = page.locator("article").filter({
    has: page.getByRole("heading", { name: "Conta criada no navegador" }),
  });
  await expect(account).toBeVisible();
  await account.getByRole("button", { name: "Ativar / renovar" }).click();
  await page
    .getByText("Dados do pagamento e crédito de IA", { exact: true })
    .click();
  await page
    .getByLabel("Referência única do pagamento")
    .fill("browser-payment-test");
  await page.getByLabel("Motivo").fill("Pagamento sintético confirmado");
  await page.getByRole("button", { name: "Revisar operação" }).click();
  await expect(page.getByText(/Novo ciclo:/)).toBeVisible();
  await page.getByRole("button", { name: "Confirmar e registrar" }).click();
  await expect(
    account.getByText("Acesso ativo", { exact: true }),
  ).toBeVisible();
  await account.getByRole("button", { name: "Ver saldo" }).click();
  await expect(page.getByText(/50,00000000/).first()).toBeVisible();
});
