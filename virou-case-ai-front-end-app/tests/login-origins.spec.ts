import { expect, test } from "@playwright/test";

for (const host of ["localhost", "127.0.0.1"]) {
  test(`login do aluno e persistência em ${host}`, async ({ page }) => {
    await page.goto(`http://${host}:5173/login`);
    await expect(page).toHaveTitle(/viroucaseai/);
    await expect(page.locator(".brand").first()).toContainText("viroucaseai");
    await page.getByLabel("Login", { exact: true }).fill("student-e2e");
    await page.getByLabel("Senha", { exact: true }).fill("e2e-student-only-password");
    await page.getByRole("button", { name: "Entrar", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Meus projetos" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Meus projetos" })).toBeVisible();
    await page.getByRole("button", { name: "Sair", exact: true }).click();
    await expect(page.getByRole("button", { name: "Entrar", exact: true })).toBeVisible();
  });

  test(`login administrativo e persistência em ${host}`, async ({ page }) => {
    await page.goto(`http://${host}:5174`);
    await expect(page).toHaveTitle(/viroucaseai/);
    await expect(page.locator(".brand").first()).toContainText("viroucaseai");
    await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
    await page.getByLabel("Senha", { exact: true }).fill("e2e-operator-only-password");
    await page.getByRole("button", { name: "Entrar no backoffice" }).click();
    await expect(page.getByRole("heading", { name: "Contas e acesso" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Contas e acesso" })).toBeVisible();
    await page.getByRole("button", { name: "Sair", exact: true }).click();
    await expect(page.getByRole("button", { name: "Entrar no backoffice" })).toBeVisible();
  });
}
