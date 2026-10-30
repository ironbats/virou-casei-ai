import { expect, test } from "@playwright/test";

test.skip(process.env.VIRA_E2E_ISOLATED !== "true", "Requer o servidor cloudflare_management_server.py com banco sintético isolado.");

test("integra a sessão administrativa, o snapshot real e a navegação sem executar comandos", async ({ page, browser }, testInfo) => {
  const errors: string[] = [];
  const requests: { method: string; path: string }[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("request", (request) => {
    if (request.url().includes("/admin-api/")) {
      requests.push({ method: request.method(), path: new URL(request.url()).pathname });
    }
  });
  await page.goto("/");
  await page.getByLabel("Login", { exact: true }).fill("operator");
  await page.getByLabel("Senha", { exact: true }).fill("test-operator-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await page.getByRole("button", { name: "Integrações e templates", exact: true }).click();
  const responsePromise = page.waitForResponse((response) => response.url().includes("publication-diagnostics") && response.status() === 200);
  await page.getByRole("button", { name: /^Gerenciar / }).first().click();
  const response = await responsePromise;
  const payload = await response.json();
  expect(payload.snapshot.schema_version).toBe(1);
  expect(payload.snapshot.publication.allowed).toBe(payload.can_publish);
  expect(payload.snapshot.current_run.id).not.toBe(payload.snapshot.previous_result.id);
  await expect(page.getByRole("heading", { name: "Prontidão da publicação" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("visao-geral-api-real.png") });
  const before = requests.length;
  await page.getByRole("button", { name: "Acompanhar verificação", exact: true }).click();
  const panel = page.getByRole("tabpanel", { name: "Verificação", exact: true });
  await expect(panel.getByRole("heading", { name: "Resultado anterior", exact: true })).toBeVisible();
  await expect(panel.getByText("API não respondeu como esperado", { exact: true }).first()).toBeVisible();
  const table = panel.getByRole("table", { name: "Prontidão de publicação e verificação por perfil" });
  await expect(table.locator("tbody tr")).toHaveCount(payload.snapshot.profiles.length);
  await Promise.all([
    page.waitForResponse((result) => result.url().includes("publication-diagnostics") && result.status() === 200),
    panel.getByRole("button", { name: "Atualizar estado", exact: true }).click(),
  ]);
  await page.screenshot({ path: testInfo.outputPath("verificacao-api-real.png") });
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const readsAfterClose = requests.filter((request) => request.path.endsWith("publication-diagnostics")).length;
  await page.waitForTimeout(3500);
  expect(requests.filter((request) => request.path.endsWith("publication-diagnostics"))).toHaveLength(readsAfterClose);
  expect(requests.slice(before).every((request) => request.method === "GET")).toBe(true);
  expect(errors).toEqual([]);
  const anonymous = await browser.newContext();
  const rejected = await anonymous.request.get(response.url());
  expect(rejected.status()).toBe(401);
  expect(await rejected.text()).not.toContain('"snapshot"');
  await anonymous.close();
  await testInfo.attach("consultas-reais", { body: JSON.stringify({ requests: requests.slice(before), errors }, null, 2), contentType: "application/json" });
});
