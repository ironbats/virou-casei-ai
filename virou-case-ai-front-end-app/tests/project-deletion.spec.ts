import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("excluir projeto confirma nome, preserva cancelamento e libera a vaga após o worker", async ({ page }, info) => {
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("student-e2e");
  await page.getByLabel("Senha", { exact: true }).fill("e2e-student-only-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Meus projetos" })).toBeVisible();
  const created = await page.evaluate(async () => {
    const me = await (await fetch("/api/v1/me")).json();
    const response = await fetch("/api/v1/projects", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token, "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ name: "Excluir no navegador", objective: "Projeto temporário para validar a exclusão" }) });
    return { status: response.status, data: await response.json() };
  });
  expect(created.status).toBe(201);
  await page.reload();
  const card = page.locator("article.project-card").filter({ hasText: "Excluir no navegador" });
  await card.getByRole("button", { name: "Excluir projeto Excluir no navegador" }).click();
  let dialog = page.getByRole("dialog", { name: "Excluir projeto", exact: true });
  await expect(dialog.getByRole("button", { name: "Confirmar exclusão" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancelar", exact: true }).click();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Excluir projeto Excluir no navegador" }).click();
  dialog = page.getByRole("dialog", { name: "Excluir projeto", exact: true });
  await dialog.getByLabel("Digite o nome do projeto para confirmar").fill("nome errado");
  await expect(dialog.getByRole("button", { name: "Confirmar exclusão" })).toBeDisabled();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(false);
    expect(await dialog.evaluate((el) => el.scrollWidth > el.clientWidth + 1)).toBe(false);
  }
  expect((await new AxeBuilder({ page }).include("dialog").withTags(["wcag2a", "wcag2aa"]).analyze()).violations).toEqual([]);
  await page.screenshot({ path: info.outputPath("excluir-projeto.png"), fullPage: true });
  await dialog.getByLabel("Digite o nome do projeto para confirmar").fill("Excluir no navegador");
  await dialog.getByRole("button", { name: "Confirmar exclusão" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(card).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByRole("button", { name: "Novo projeto", exact: true })).toBeEnabled();
});
