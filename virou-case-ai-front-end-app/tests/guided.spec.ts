import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { showBuildHistory, workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Requer laboratório local isolado.",
);

test("ideia, seleção automática, edição, autosave, snapshot e aplicação explícita", async ({
  page,
}, info) => {
  test.setTimeout(90000);
  const project = await workspace(page);
  await expect(page.getByText("Etapa 1 de 3")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publicar", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("#publication-reason")).toContainText(
    "Configuração da hospedagem pendente",
  );
  let sends = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/messages"))
      sends++;
  });
  await page
    .getByLabel("O que vamos fazer?")
    .fill(
      "Aplicação para gerenciar tarefas da equipe interna. Criar, listar e concluir tarefas.",
    );
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
  const explore = page.getByRole("button", {
    name: "Explorar catálogo",
    exact: true,
  });
  if (await explore.count()) await explore.click();
  const card = page
    .locator(".template-card")
    .filter({ hasText: "SDD" })
    .first();
  await card.getByRole("checkbox").check();
  await expect(card.locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
    { timeout: 30000 },
  );
  await card
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("textbox", { name: "Objetivo do projeto *", exact: true })
    .fill("Gerenciar tarefas");
  await dialog
    .getByRole("textbox", { name: "Para quem é o projeto? *", exact: true })
    .fill("Equipe interna");
  await dialog
    .getByRole("textbox", { name: "Critérios de aceite *", exact: true })
    .fill("Criar, listar e concluir tarefas");
  await dialog
    .getByLabel("Conteúdo que será enviado")
    .fill("Minha cópia pessoal integral para orientar a construção.");
  await dialog
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page
    .locator(".attachment-card")
    .getByRole("button", { name: "Abrir", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Minha cópia pessoal integral",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Fechar", exact: true })
    .click();
  expect(sends).toBe(0);
  await page.screenshot({
    path: info.outputPath("guided-review.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  await expect(page.getByText("Etapa 1 de 3")).toBeVisible();
  const turn = page.locator(".build-turn").last();
  await page.getByLabel("O que vamos fazer?").fill("Próximo pedido preservado");
  await expect(turn).toHaveAttribute("data-state", "succeeded", {
    timeout: 30000,
  });
  expect(
    await page.evaluate(
      async (pid) =>
        (await (await fetch(`/api/v1/projects/${pid}`)).json())
          .current_revision_id,
      project.id,
    ),
  ).toBe(project.current_revision_id);
  await showBuildHistory(page);
  await turn
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("O que vamos fazer?")).toHaveValue(
    "Próximo pedido preservado",
  );
  expect(sends).toBe(1);
  await page.reload();
  await expect(page.getByLabel("O que vamos fazer?")).toHaveValue(
    "Próximo pedido preservado",
  );
  expect(
    (
      await new AxeBuilder({ page })
        .include(".ide-layout")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 900)
      await page.getByRole("tab", { name: "Chat", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if (width === 375)
      await page.screenshot({
        path: info.outputPath("guided-mobile.png"),
        fullPage: true,
      });
  }
});
