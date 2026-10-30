import { readFileSync, writeFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { managementConnection, managementDiagnostics } from "./cloudflare-management-test-data";

const path = "**/admin-api/v1/integration-connections/connection-test/publication-diagnostics";
test.use({ timezoneId: "America/Sao_Paulo" });

const configPath = "**/admin-api/v1/integrations/cloudflare/connections/connection-test";

async function setup(page: Page, diagnostic: unknown, config: unknown = managementConnection) {
  await page.route(configPath, route => route.fulfill({ json: config }));
  await page.route(path, route => route.fulfill({ json: diagnostic }));
  await page.route("**/admin-api/v1/publication-qualification-requirements", route => route.fulfill({ json: { checks: ["isolamento", "publicacao_remota"] } }));
  await page.goto("/tests/cloudflare-manager.fixture.html");
  await page.getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Visão geral", exact: true })).toBeVisible();
  await expect(page.getByText("Cadastro ativo", { exact: true })).toBeVisible();
  return page.getByRole("dialog");
}

async function select(page: Page, name: string) {
  await page.getByRole("tab", { name, exact: true }).click();
  return page.getByRole("tabpanel", { name, exact: true });
}

test("snapshot: execução corrente e falha histórica têm identidade, etapas e diagnóstico próprios", async ({ page }) => {
  const diagnostic = managementDiagnostics("running");
  let mutations = 0;
  page.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations += 1; });
  await setup(page, diagnostic);
  const overview = page.getByRole("tabpanel", { name: "Visão geral", exact: true });
  await expect(overview.getByRole("heading", { name: "Prontidão da publicação", exact: true })).toBeVisible();
  await expect(overview.getByText("Não confirma publicação", { exact: true })).toBeVisible();
  await expect(overview.getByText("Bloqueada", { exact: true })).toBeVisible();
  await expect(overview.getByText("Nova tentativa automática programada", { exact: true })).toHaveCount(0);
  await overview.getByRole("button", { name: "Acompanhar verificação", exact: true }).click();
  const panel = page.getByRole("tabpanel", { name: "Verificação", exact: true });
  const current = panel.getByRole("region", { name: "Acompanhamento da verificação", exact: true });
  await expect(current).toContainText("run-current-008");
  await expect(current).not.toContainText("PUBLICATION_API_NOT_READY");
  const previous = panel.locator(".cf-previous-result");
  await expect(previous).toContainText("run-previous-007");
  await expect(previous).toContainText("histórico");
  await expect(previous).toContainText("PUBLICATION_API_NOT_READY");
  await expect(previous).toContainText("17 falhas consecutivas");
  await expect(panel.getByRole("list", { name: "Etapas observadas" })).toContainText("Validar HTTP e JSON");
  await expect(panel.getByRole("progressbar")).toHaveCount(0);
  await expect(panel.getByText(/\d+\s*%/)).toHaveCount(0);
  await previous.getByRole("button", { name: "Ver diagnóstico", exact: true }).click();
  const previousDetail = panel.getByRole("region", { name: "Diagnóstico do resultado anterior", exact: true });
  await expect(previousDetail).toContainText("correlation-run-previous-007");
  await expect(previousDetail).toContainText("200");
  await expect(previousDetail).toContainText("JSON com status válido");
  await expect(previousDetail).toContainText("Campo de status incompatível");
  await expect(previousDetail).toContainText("Falhou");
  expect(mutations).toBe(0);
});

test("snapshot: espera automática aparece uma vez, com fuso e contador persistido", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await setup(page, managementDiagnostics("retry"));
  const overview = page.getByRole("tabpanel", { name: "Visão geral", exact: true });
  await expect(overview.getByRole("heading", { name: "Nova tentativa automática programada", exact: true })).toHaveCount(1);
  const next = overview.getByRole("region", { name: "Próximo passo" });
  await expect(next).toContainText("30/10/2026");
  await expect(next).toContainText(/UTC|GMT|BRT/);
  await expect(next).toContainText("17 falhas consecutivas");
  await expect(overview).toContainText("Projetos existentes continuam vinculados à conta original");
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true }).click();
  await expect(page.getByRole("region", { name: "Próximo passo" })).toContainText("17 falhas consecutivas");
});

test("snapshot: atraso usa o horário do servidor e execução elimina espera contraditória", async ({ page }) => {
  await setup(page, managementDiagnostics("overdue"));
  await expect(page.getByRole("tabpanel", { name: "Visão geral", exact: true }).getByText("Horário previsto ultrapassado; aguardando atualização.", { exact: true })).toBeVisible();
  const running = managementDiagnostics("running");
  running.snapshot.retry.scheduled = true;
  running.snapshot.retry.next_attempt_at = "2026-10-30T12:30:00Z";
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ json: running }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.getByRole("region", { name: "Próximo passo" })).toContainText("Verificação em andamento");
  await expect(page.getByText("Horário previsto ultrapassado; aguardando atualização.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Nova tentativa automática programada", exact: true })).toHaveCount(0);
});

for (const mode of ["never", "unknown"]) {
  test(`snapshot: ${mode} não vira sucesso nem promete retry`, async ({ page }) => {
    await setup(page, managementDiagnostics(mode));
    const overview = page.getByRole("tabpanel", { name: "Visão geral", exact: true });
    await expect(overview.getByText("Liberada", { exact: true })).toHaveCount(0);
    await expect(overview.getByText("Nova tentativa automática programada", { exact: true })).toHaveCount(0);
    if (mode === "never") await expect(overview.getByText("Ainda não verificado", { exact: true }).first()).toBeVisible();
    else await expect(overview.getByText("Não foi possível confirmar", { exact: true })).toBeVisible();
  });
}

test("snapshot: perfis são dinâmicos e aprovação parcial não se propaga", async ({ page }) => {
  const diagnostic = managementDiagnostics("partial");
  diagnostic.snapshot.profiles.push({ ...diagnostic.snapshot.profiles[1], id: "future-supported-profile", label: "Perfil adicional do backend" });
  await setup(page, diagnostic);
  await expect(page.getByText("Parcialmente liberada", { exact: true })).toBeVisible();
  const panel = await select(page, "Verificação");
  const table = panel.getByRole("table", { name: "Prontidão de publicação e verificação por perfil" });
  await expect(table.getByRole("row", { name: /hono-typescript-d1/ })).toContainText("Liberada");
  await expect(table.getByRole("row", { name: /fastapi-python-d1/ })).toContainText("Bloqueada");
  await expect(table.getByRole("row", { name: /Perfil adicional do backend/ })).toContainText("Bloqueada");
});

test("snapshot: etapas desconhecidas e não aplicáveis não recebem aprovação", async ({ page }) => {
  const diagnostic = managementDiagnostics("parallel");
  diagnostic.snapshot.current_run!.steps[0].state = "unknown";
  diagnostic.snapshot.current_run!.steps[1].state = "skipped";
  await setup(page, diagnostic);
  const panel = await select(page, "Verificação");
  await expect(panel.getByRole("region", { name: "Acompanhamento da verificação" })).toContainText("hono-typescript-d1, fastapi-python-d1");
  const steps = panel.getByRole("list", { name: "Etapas observadas" });
  const unknown = steps.getByRole("listitem").filter({ hasText: "Preparar ambiente" });
  await expect(unknown).toContainText("Não informado");
  await expect(unknown).not.toHaveClass(/cf-success/);
  const skipped = steps.getByRole("listitem").filter({ hasText: "Publicar aplicação de teste" });
  await expect(skipped).toContainText("Não aplicável");
  await expect(skipped).not.toHaveClass(/cf-success/);
  await expect(steps.getByRole("listitem").filter({ hasText: "Validar HTTP e JSON" })).toHaveCount(2);
});

test("snapshot: evidência antiga permanece histórica e não libera a versão atual", async ({ page }) => {
  await setup(page, managementDiagnostics("changed"));
  await expect(page.getByRole("region", { name: "Próximo passo" })).toContainText("Verificação anterior não se aplica à configuração atual");
  await expect(page.getByText("Liberada", { exact: true })).toHaveCount(0);
  const panel = await select(page, "Verificação");
  await expect(panel.locator(".cf-previous-result")).toContainText("Versão 6");
  await expect(panel.locator(".cf-previous-result")).toContainText("Configuração anterior");
});

test("snapshot: refresh deduplicado mantém histórico, foco e expansão após falha de rede", async ({ page }) => {
  const original = managementDiagnostics("ready");
  await setup(page, original);
  await select(page, "Verificação");
  await page.getByText("Resultado por perfil da última verificação", { exact: true }).click();
  let count = 0;
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.unroute(path);
  await page.route(path, async route => {
    count += 1;
    await pending;
    await route.fulfill({ status: 503, json: { error: { message: "Leitura temporariamente indisponível" } } });
  });
  const refresh = page.getByRole("button", { name: "Atualizar estado", exact: true });
  const reader = page.getByText("Resultado por perfil da última verificação", { exact: true });
  await reader.focus();
  await refresh.evaluate(button => { for (let index = 0; index < 20; index++) (button as HTMLButtonElement).click(); });
  await expect.poll(() => count).toBe(1);
  release();
  await expect(page.getByRole("tabpanel", { name: "Verificação", exact: true }).getByText("Atualização indisponível", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Verificação", exact: true }).getByText(/Dados de .*atualização indisponível/)).toBeVisible();
  await expect(page.locator("details").filter({ has: page.getByText("Resultado por perfil da última verificação", { exact: true }) })).toHaveAttribute("open", "");
  await expect(page.locator(".cf-previous-result")).toContainText("run-previous-007");
  await expect(reader).toBeFocused();
  const overview = await select(page, "Visão geral");
  await expect(overview.getByText("Liberada", { exact: true })).toHaveCount(0);
  await expect(overview.getByText("Não foi possível confirmar", { exact: true })).toBeVisible();
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ json: original }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(overview.getByText("Liberada", { exact: true })).toBeVisible();
});

test("snapshot: fechar interrompe polling local e duas abas não criam comandos", async ({ page, context }) => {
  await page.clock.install();
  const diagnostic = managementDiagnostics("running");
  let firstReads = 0;
  let mutations = 0;
  page.on("request", request => {
    if (request.url().includes("publication-diagnostics")) firstReads += 1;
    if (!["GET", "HEAD"].includes(request.method())) mutations += 1;
  });
  await setup(page, diagnostic);
  const second = await context.newPage();
  second.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations += 1; });
  await setup(second, diagnostic);
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  const closedReads = firstReads;
  await page.clock.runFor(30000);
  expect(firstReads).toBe(closedReads);
  expect(mutations).toBe(0);
  await second.close();
});

test("snapshot: resposta atrasada de outra conexão não reaparece após trocar alvo", async ({ page }) => {
  await setup(page, managementDiagnostics("ready"));
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.unroute(path);
  let pendingRead = false;
  await page.route(path, async route => { pendingRead = true; await pending; await route.fulfill({ json: managementDiagnostics("retry") }).catch(() => {}); });
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect.poll(() => pendingRead).toBe(true);
  const secondConfig = { ...managementConnection, id: "configuration-second", connection_id: "connection-second", name: "Cloudflare homologação", version: 3, is_default: false, settings: { account_id: "b".repeat(32), environment: "staging" } };
  const secondDiagnostic = managementDiagnostics("never");
  Object.assign(secondDiagnostic.snapshot.connection, { id: secondConfig.connection_id, name: secondConfig.name, configuration_id: secondConfig.id, configuration_version: 3, is_default: false, environment: "staging", account_masked: "bbbb…bbbb" });
  await page.route("**/admin-api/v1/integrations/cloudflare/connections/connection-second", route => route.fulfill({ json: secondConfig }));
  await page.route("**/admin-api/v1/integration-connections/connection-second/publication-diagnostics", route => route.fulfill({ json: secondDiagnostic }));
  await page.evaluate(config => window.dispatchEvent(new CustomEvent("fixture-connection", { detail: config })), secondConfig);
  await expect(page.getByRole("heading", { name: "Gerenciar Cloudflare homologação", exact: true })).toBeVisible();
  await expect(page.getByText("bbbb…bbbb", { exact: true })).toBeVisible();
  release();
  await expect(page.getByText("Nova tentativa automática programada", { exact: true })).toHaveCount(0);
  await expect(page.getByText("run-previous-007", { exact: true })).toHaveCount(0);
});

test("snapshot: sessão expirada notifica autenticação e remove diagnóstico protegido", async ({ page }) => {
  await setup(page, managementDiagnostics("running"));
  await page.evaluate(() => window.addEventListener("admin-expired", () => { document.body.dataset.sessionExpired = "true"; }));
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ status: 401, json: { error: { message: "Sessão expirada" } } }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.locator("body")).toHaveAttribute("data-session-expired", "true");
  await expect(page.getByText("run-current-008", { exact: true })).toHaveCount(0);
  await expect(page.getByText("PUBLICATION_API_NOT_READY", { exact: true })).toHaveCount(0);
});

test("snapshot: teclado, contraste, estados e semântica acessíveis nas quatro abas", async ({ page }) => {
  await setup(page, managementDiagnostics("running"));
  const dialog = page.getByRole("dialog");
  const accessibilityReports: unknown[] = [];
  for (const name of ["Visão geral", "Verificação", "Conexão", "Qualificação"]) {
    await select(page, name);
    const accessibility = await new AxeBuilder({ page }).include("dialog").withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
    accessibilityReports.push({ tab: name, violations: accessibility.violations, incomplete: accessibility.incomplete, passed_rules: accessibility.passes.map(rule => rule.id), contrast: accessibility.passes.filter(rule => rule.id === "color-contrast").flatMap(rule => rule.nodes.map(node => ({ target: node.target, checks: node.any.map(check => check.data) }))) });
    expect(accessibility.violations, JSON.stringify(accessibility.violations, null, 2)).toEqual([]);
  }
  writeFileSync("test-results/cloudflare-accessibility.json", JSON.stringify(accessibilityReports, null, 2));
  await page.getByRole("tab", { name: "Qualificação", exact: true }).focus();
  await page.keyboard.press("Home");
  await expect(page.getByRole("tab", { name: "Visão geral", exact: true })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Verificação", exact: true })).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.getByRole("tab", { name: "Qualificação", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Fechar", exact: true }).focus();
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true })).toBeFocused();
});

test("snapshot: screenshots em 390, 768, 1280 e 1536px, 200% e movimento reduzido", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const screenshotData = managementDiagnostics("retry");
  await setup(page, screenshotData);
  const dialog = page.getByRole("dialog");
  for (const width of [390, 768, 1280, 1536]) {
    await page.setViewportSize({ width, height: 1040 });
    for (const [tabName, name] of [["Visão geral", "overview"], ["Verificação", "verification"]]) {
      await select(page, tabName);
      Object.assign(screenshotData, managementDiagnostics(tabName === "Visão geral" ? "retry" : "running"));
      await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
      await expect(page.getByRole("button", { name: "Atualizar estado", exact: true })).toBeEnabled();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
      const verticalScrollers = await dialog.evaluate(element => [...element.querySelectorAll<HTMLElement>("*")].filter(node => node.getClientRects().length > 0 && /auto|scroll/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 1).map(node => node.className));
      expect(verticalScrollers.length, `${width}px ${tabName}: ${verticalScrollers.join(", ")}`).toBeLessThanOrEqual(1);
      await expect(page.getByRole("button", { name: "Fechar", exact: true })).toBeInViewport();
      await page.screenshot({ path: `test-results/cloudflare-${name}-${width}.png` });
    }
  }
  await page.setViewportSize({ width: 640, height: 520 });
  await select(page, "Visão geral");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await expect(page.getByRole("button", { name: "Fechar", exact: true })).toBeInViewport();
  await select(page, "Verificação");
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const animated = await dialog.locator(".cf-activity").evaluateAll(elements => elements.map(element => getComputedStyle(element).animationDuration));
  expect(animated.every(value => value === "0s" || value === "0.01ms" || value === "1e-05s")).toBe(true);
  await page.screenshot({ path: "test-results/cloudflare-verification-zoom200.png" });
});


test("snapshot: contrato produzido pela API real alimenta o modal sem adaptação fictícia", async ({ page }) => {
  const config = JSON.parse(readFileSync(new URL("./fixtures/cloudflare-management-connection.json", import.meta.url), "utf8"));
  const diagnostic = JSON.parse(readFileSync(new URL("./fixtures/cloudflare-management-api.json", import.meta.url), "utf8"));
  await page.route(`**/admin-api/v1/integrations/cloudflare/connections/${config.connection_id}`, route => route.fulfill({ json: config }));
  await page.route(`**/admin-api/v1/integration-connections/${config.connection_id}/publication-diagnostics`, route => route.fulfill({ json: diagnostic }));
  await page.route("**/admin-api/v1/publication-qualification-requirements", route => route.fulfill({ json: { checks: [] } }));
  await page.goto("/tests/cloudflare-manager.fixture.html");
  await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-connection", { detail: value })), config);
  await expect(page.getByRole("heading", { name: `Gerenciar ${config.name}`, exact: true })).toBeVisible();
  await expect(page.getByRole("tabpanel", { name: "Visão geral", exact: true }).getByText("Bloqueada", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Acompanhar verificação", exact: true }).click();
  await expect(page.getByRole("region", { name: "Acompanhamento da verificação" })).toContainText("Verificação em andamento");
  await expect(page.locator(".cf-previous-result")).toContainText("PUBLICATION_API_NOT_READY");
  await page.getByRole("button", { name: "Ver eventos", exact: true }).click();
  const technical = page.getByRole("region", { name: "Execução atual", exact: true });
  await expect(technical).toContainText(diagnostic.snapshot.current_run.id);
  const history = page.getByRole("region", { name: "Diagnóstico do resultado anterior", exact: true });
  await expect(history).toContainText(diagnostic.snapshot.previous_result.id);
  await expect(technical).not.toContainText(diagnostic.snapshot.previous_result.id);
  expect(diagnostic.snapshot.current_run.id).not.toBe(diagnostic.snapshot.previous_result.id);
});

test("snapshot: campos opcionais ausentes e gate desconhecido não derrubam nem liberam a tela", async ({ page }) => {
  const diagnostic = JSON.parse(JSON.stringify(managementDiagnostics("never")));
  delete diagnostic.snapshot.publication;
  delete diagnostic.snapshot.profiles;
  delete diagnostic.snapshot.retry;
  delete diagnostic.snapshot.current_run;
  delete diagnostic.snapshot.previous_result;
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await setup(page, diagnostic);
  await expect(page.getByText("Liberada", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Não foi possível confirmar", { exact: true }).first()).toBeVisible();
  const panel = await select(page, "Verificação");
  await expect(panel).toContainText("Nenhum perfil foi informado pelo backend");
  expect(errors).toEqual([]);
});

test("snapshot: versão inicial antiga e resposta de schema incompatível nunca substituem estado atual", async ({ page }) => {
  const old = managementDiagnostics("ready");
  old.snapshot.connection.configuration_version = 6;
  old.snapshot.connection.configuration_id = "configuration-old";
  await page.route(configPath, route => route.fulfill({ json: managementConnection }));
  await page.route(path, route => route.fulfill({ json: old }));
  await page.route("**/admin-api/v1/publication-qualification-requirements", route => route.fulfill({ json: { checks: [] } }));
  await page.goto("/tests/cloudflare-manager.fixture.html");
  await page.getByRole("button", { name: "Gerenciar Cloudflare produção", exact: true }).click();
  await expect(page.getByText("Liberada", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("alert").first()).toBeVisible();
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ json: managementDiagnostics("running") }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.getByRole("region", { name: "Próximo passo" })).toContainText("Verificação em andamento");
  const invalid = managementDiagnostics("ready");
  invalid.snapshot.schema_version = 999;
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ json: invalid }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.getByText("Liberada", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("tabpanel", { name: "Visão geral", exact: true })).toContainText("atualização indisponível");
});


test("snapshot: polling conserva a leitura e para quando o backend encerra acompanhamento", async ({ page }) => {
  await page.clock.install();
  const current = managementDiagnostics("running");
  let reads = 0;
  page.on("request", request => { if (request.url().includes("publication-diagnostics")) reads += 1; });
  await setup(page, current);
  await select(page, "Verificação");
  const summary = page.getByText("Detalhes técnicos e eventos", { exact: true });
  await summary.click();
  await summary.focus();
  const scrollBefore = await page.locator(".cloudflare-panels").evaluate(element => element.scrollTop).catch(() => null);
  current.snapshot.revision = "different-opaque-revision";
  current.snapshot.current_run!.steps[2].message = "Nova observação recebida sem estimativa";
  await page.clock.runFor(4001);
  await expect(summary).toBeFocused();
  await expect(page.locator("details.cf-technical-details")).toHaveAttribute("open", "");
  if (scrollBefore != null) expect(await page.locator(".cloudflare-panels").evaluate(element => element.scrollTop)).toBe(scrollBefore);
  Object.assign(current, managementDiagnostics("ready"));
  current.snapshot.polling.interval_ms = 0;
  await page.clock.runFor(4001);
  await expect(page.getByRole("region", { name: "Acompanhamento da verificação" })).toContainText("Verificação finalizada");
  const terminalReads = reads;
  await page.clock.runFor(12000);
  expect(reads).toBe(terminalReads);
  await expect(page.locator("details.cf-technical-details")).toHaveAttribute("open", "");
});

test("snapshot: copiar diagnóstico preserva ação existente sem disparar comando remoto", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const original = managementDiagnostics("running");
  const diagnostic = {
    ...original,
    snapshot: {
      ...original.snapshot,
      previous_result: {
        ...original.snapshot.previous_result!,
        failure: {
          code: "PUBLICATION_API_NOT_READY",
          stage: "verification_remote",
          details: { transport: "response_received", observed_http_status: 200 },
          remote: { attempts: [{ checks: [{ name: "api", passed: false, status: 200, code: "PUBLICATION_API_NOT_READY" }] }] },
          build: { state: "succeeded", output: "Synthetic build evidence from the prior run" },
        },
        bindings: { expected: "database-test", observed: "database-test" },
        revision_id: "synthetic-previous-revision",
      },
    },
  };
  let mutations = 0;
  page.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations += 1; });
  await setup(page, diagnostic);
  await select(page, "Verificação");
  await page.getByText("Detalhes técnicos e eventos", { exact: true }).click();
  await page.getByText("Executor e diagnóstico técnico", { exact: true }).click();
  await page.getByText("Diagnóstico completo para suporte", { exact: true }).click();
  await page.getByRole("button", { name: "Copiar diagnóstico", exact: true }).click();
  await expect(page.getByRole("button", { name: "Diagnóstico copiado", exact: true })).toBeVisible();
  const copied = JSON.parse(await page.evaluate(() => navigator.clipboard.readText()));
  expect(copied.snapshot.previous_result.id).toBe(diagnostic.snapshot.previous_result!.id);
  expect(copied.snapshot.connection.account_masked).toBe("aaaa…aaaa");
  expect(copied).toEqual(diagnostic);
  expect(copied.snapshot.previous_result.id).not.toBe(copied.snapshot.current_run.id);
  expect(mutations).toBe(0);
});

test("snapshot: mudança de versão em outra aba exige recarregar conexão antes de comando", async ({ page }) => {
  await setup(page, managementDiagnostics("ready"));
  const updated = managementDiagnostics("ready");
  updated.snapshot.connection.configuration_version = 8;
  updated.snapshot.connection.configuration_id = "configuration-version-8";
  await page.unroute(path);
  await page.route(path, route => route.fulfill({ json: updated }));
  await page.unroute(configPath);
  await page.route(configPath, route => route.fulfill({ status: 503, json: { error: { message: "Configuração temporariamente indisponível" } } }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await select(page, "Verificação");
  await expect(page.getByRole("button", { name: "Verificar publicação", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Configuração temporariamente indisponível" })).toBeVisible();
  await page.unroute(configPath);
  await page.route(configPath, route => route.fulfill({ json: { ...managementConnection, id: "configuration-version-8", version: 8 } }));
  await page.getByRole("button", { name: "Atualizar estado", exact: true }).click();
  await expect(page.locator(".cloudflare-context")).toContainText("Versão 8");
  await page.getByLabel("Motivo da verificação", { exact: true }).fill("Conferência da configuração atualizada");
  await expect(page.getByRole("button", { name: "Verificar publicação", exact: true })).toBeEnabled();
});

test("snapshot: abas móveis mantêm rótulos inteiros e seleção visível ao navegar por teclado", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1040 });
  await setup(page, managementDiagnostics("running"));
  const observations: unknown[] = [];
  const assertSelectedVisible = async (label: string) => {
    const selected = page.getByRole("tab", { name: label, exact: true });
    await expect(selected).toBeFocused();
    await expect(selected).toHaveAttribute("aria-selected", "true");
    const position = await selected.evaluate(element => {
      const button = element.getBoundingClientRect();
      const list = element.closest('[role="tablist"]')!;
      const bounds = list.getBoundingClientRect();
      return { label: element.textContent, left: button.left, right: button.right, availableLeft: bounds.left + list.clientLeft, availableRight: bounds.left + list.clientLeft + list.clientWidth, scrollLeft: list.scrollLeft };
    });
    expect(position.left).toBeGreaterThanOrEqual(position.availableLeft - 1);
    expect(position.right).toBeLessThanOrEqual(position.availableRight + 1);
    observations.push(position);
  };
  for (const width of [390, 768]) {
    await page.setViewportSize({ width, height: 1040 });
    for (const label of ["Visão geral", "Verificação", "Conexão", "Qualificação"]) {
      const lines = await page.getByRole("tab", { name: label, exact: true }).evaluate(element => {
        const nodes = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        const tops: number[] = [];
        while (nodes.nextNode()) {
          const node = nodes.currentNode;
          if (!node.textContent?.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          for (const rect of range.getClientRects()) if (rect.width > 0 && rect.height > 0) tops.push(Math.round(rect.top));
        }
        return [...new Set(tops)];
      });
      expect(lines, `${width}px ${label}`).toHaveLength(1);
    }
    await page.getByRole("tab", { name: "Visão geral", exact: true }).focus();
    await page.keyboard.press("Home");
    await assertSelectedVisible("Visão geral");
    await page.keyboard.press("ArrowRight");
    await assertSelectedVisible("Verificação");
    await page.keyboard.press("End");
    await assertSelectedVisible("Qualificação");
    await page.keyboard.press("ArrowRight");
    await assertSelectedVisible("Visão geral");
    await page.keyboard.press("Home");
    await assertSelectedVisible("Visão geral");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  }
  writeFileSync("test-results/cloudflare-mobile-tabs.json", JSON.stringify(observations, null, 2));
});
