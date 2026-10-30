import { expect, test } from "@playwright/test";

test("falha em fonte preserva a navegacao e bloqueia gravacao insegura", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("student-e2e");
  await page.getByLabel("Senha", { exact: true }).fill("e2e-student-only-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Meus projetos" })).toBeVisible();
  const created = await page.evaluate(async () => {
    const me = await (await fetch("/api/v1/me")).json();
    const response = await fetch("/api/v1/projects", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({ name: "Projeto com fonte ausente", objective: "Projeto para exercitar indisponibilidade de revisao" }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(created.status).toBe(201);
  await page.route("**/api/v1/projects/*/revisions/*/files/*", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "SOURCE_UNAVAILABLE", message: "Fonte indisponivel para este projeto.", request_id: "e2e-source-audit" } }),
    });
  });
  await page.goto(`/app/projetos/${created.body.id}`);
  await expect(page.getByRole("heading", { name: "Projeto com fonte ausente" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Projeto disponível, mas os arquivos da revisão não carregaram" })).toBeVisible();
  await expect(page.getByText("Identificação do erro: e2e-source-audit")).toBeVisible();
  await expect(page.getByRole("button", { name: "Tentar carregar novamente" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Histórico de revisões" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Salvar revisão" })).toHaveCount(0);
});
