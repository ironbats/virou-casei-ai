import { expect, test } from "@playwright/test";

test("console real: eventos de exclusão, filtros, pausa, retomada e responsividade", async ({ page, playwright }, info) => {
  const student = await playwright.request.newContext({ baseURL: "http://127.0.0.1:5173", extraHTTPHeaders: { Origin: "http://127.0.0.1:5173" } });
  try {
    const login = await student.post("/api/v1/auth/login", { data: { login: "student-e2e", password: "e2e-student-only-password" } });
    expect(login.status()).toBe(200);
    const session = await login.json();
    const name = `Monitoria ${Date.now()}`;
    const created = await student.post("/api/v1/projects", { headers: { "X-CSRF-Token": session.csrf_token, "Idempotency-Key": `monitor-create-${name}` }, data: { name, objective: "Projeto temporário para verificar eventos reais de execução" } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    const removed = await student.delete(`/api/v1/projects/${project.id}`, { headers: { "X-CSRF-Token": session.csrf_token, "Idempotency-Key": `monitor-delete-${name}`, "If-Match": String(project.version) }, data: { confirmation: name } });
    expect(removed.status()).toBe(202);
  } finally { await student.dispose(); }
  await page.goto("/");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page.getByLabel("Senha", { exact: true }).fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await expect(page.getByRole("button", { name: "Observabilidade", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Observabilidade", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Observabilidade", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Console de eventos" })).toBeVisible();
  await page.getByRole("combobox", { name: "Serviço", exact: true }).selectOption("platform");
  await expect(page.getByText("Projeto excluído. A vaga foi liberada.", { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await page.getByRole("button", { name: "Pausar", exact: true }).click();
  await expect(page.getByText(/Atualização pausada/)).toBeVisible();
  await page.getByRole("button", { name: "Retomar", exact: true }).click();
  await expect(page.getByText(/Atualização a cada 3 segundos/)).toBeVisible();
  await page.getByRole("combobox", { name: "Nível", exact: true }).selectOption("error");
  await expect(page.getByText(/Nenhum evento para os filtros atuais/)).toBeVisible();
  await page.getByRole("combobox", { name: "Nível", exact: true }).selectOption("");
  await expect(page.locator(".console-event").first()).toBeVisible();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), `${width}px`).toBe(false);
  }
  await page.screenshot({ path: info.outputPath("observabilidade.png"), fullPage: true });
});
