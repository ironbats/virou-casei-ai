import { expect, test } from "@playwright/test";

test("login público não revela o perfil administrativo", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page.getByLabel("Senha", { exact: true }).fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Credenciais inválidas.");
  await expect(page.getByRole("alert")).not.toContainText(/administrativ|backoffice/i);
});

test("defesa do frontend oculta mensagem antiga em resposta 401", async ({ page }) => {
  await page.route("**/api/v1/auth/login", route => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "Esta conta é administrativa. Entre pelo backoffice." } }) }));
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page.getByLabel("Senha", { exact: true }).fill("invalid-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Credenciais inválidas.");
  await expect(page.getByRole("alert")).not.toContainText(/administrativ|backoffice/i);
});
