import { expect, test, type Request } from "@playwright/test";
import {
  accessPolicy,
  capturePolicy,
  monitorMocks,
  preparationPolicy,
} from "./observability-test-data";

async function openPolicy(page: import("@playwright/test").Page) {
  await page.goto("/tests/observability.fixture.html");
  await page
    .getByRole("button", { name: "Políticas de observabilidade" })
    .click();
  return page.getByRole("dialog", { name: "Políticas de observabilidade" });
}

test("captura salva limites e categorias com versão, motivo, CSRF e idempotência", async ({
  page,
}) => {
  await monitorMocks(page);
  const writes: Request[] = [];
  await page.route("**/observability/policy", async (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: capturePolicy });
    writes.push(route.request());
    return route.fulfill({
      json: {
        ...capturePolicy,
        version: 4,
        settings: route.request().postDataJSON().settings,
      },
    });
  });
  const dialog = await openPolicy(page);
  await dialog
    .getByRole("checkbox", { name: "Habilitar captura de payloads protegidos" })
    .check();
  await dialog.getByLabel("Retenção (dias)", { exact: false }).fill("9");
  await dialog
    .getByRole("checkbox", { name: "Estruturas e schemas" })
    .uncheck();
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Investigar falhas de preenchimento.");
  await dialog
    .getByRole("button", { name: "Salvar política de captura" })
    .click();
  await expect(
    dialog.getByText("Configuração salva na versão 4.", { exact: true }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].headers()["if-match"]).toBe("3");
  expect(writes[0].headers()["x-csrf-token"]).toBe("csrf-fixture");
  expect(writes[0].headers()["idempotency-key"]).toBeTruthy();
  expect(writes[0].postDataJSON()).toMatchObject({
    reason: "Investigar falhas de preenchimento.",
    settings: {
      capture_enabled: true,
      retention_days: 9,
      allowed_categories: ["technical_metadata", "field_identifiers", "usage"],
    },
  });
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(dialog).not.toBeVisible();
});

test("troca de abas preserva edição e fechar pede descarte sem perder dados", async ({
  page,
}) => {
  await monitorMocks(page);
  const dialog = await openPolicy(page);
  await dialog.getByLabel("Retenção (dias)", { exact: false }).fill("12");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Retenção para investigação.");
  await dialog
    .getByRole("tab", { name: "Acesso a payloads", exact: true })
    .click();
  await dialog
    .getByRole("checkbox", { name: "Operador de teste operator-fixture" })
    .check();
  await dialog.getByRole("tab", { name: /Captura e retenção/ }).click();
  await expect(
    dialog.getByLabel("Retenção (dias)", { exact: false }),
  ).toHaveValue("12");
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(
    dialog.getByText("Descartar alterações não salvas?", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Continuar editando" }).click();
  await expect(
    dialog.getByLabel("Motivo da alteração", { exact: true }),
  ).toHaveValue("Retenção para investigação.");
  await dialog.getByRole("tab", { name: /Acesso a payloads/ }).click();
  await expect(
    dialog.getByRole("checkbox", {
      name: "Operador de teste operator-fixture",
    }),
  ).toBeChecked();
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await dialog.getByRole("button", { name: "Descartar e fechar" }).click();
  await expect(dialog).not.toBeVisible();
});

test("permissão de payload é explícita e versionada, com concessão limitada à seleção", async ({
  page,
}) => {
  await monitorMocks(page, false);
  const writes: Request[] = [];
  await page.route("**/observability/payload-access", async (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: accessPolicy });
    writes.push(route.request());
    return route.fulfill({
      json: {
        ...accessPolicy,
        version: 3,
        can_inspect_payloads: true,
        admins: accessPolicy.admins.map((admin) => ({
          ...admin,
          allowed: route.request().postDataJSON().admin_ids.includes(admin.id),
        })),
      },
    });
  });
  const dialog = await openPolicy(page);
  await dialog
    .getByRole("tab", { name: "Acesso a payloads", exact: true })
    .click();
  await expect(
    dialog.getByText(/Seu acesso atual: não autorizado/),
  ).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", {
      name: "Operador de teste operator-fixture",
    }),
  ).not.toBeChecked();
  await dialog
    .getByRole("checkbox", { name: "Operador de teste operator-fixture" })
    .check();
  await dialog
    .getByLabel("Motivo da alteração de acesso", { exact: true })
    .fill("Autorizar inspeção técnica para diagnóstico.");
  await dialog
    .getByRole("button", { name: "Salvar acesso a payloads" })
    .click();
  await expect(dialog.getByText(/Seu acesso atual: autorizado/)).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].headers()["if-match"]).toBe("2");
  expect(writes[0].postDataJSON().admin_ids).toEqual(["admin-fixture"]);
  await expect(
    dialog.getByRole("checkbox", {
      name: "Outro administrador another-fixture",
    }),
  ).not.toBeChecked();
});

test("limites incompatíveis impedem salvar e política usa apenas categorias permitidas", async ({
  page,
}) => {
  const calls = await monitorMocks(page);
  const dialog = await openPolicy(page);
  await dialog
    .getByLabel("Limite por payload (bytes)", { exact: false })
    .fill("100000");
  await dialog
    .getByLabel("Limite por operação (bytes)", { exact: false })
    .fill("4096");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Ajustar limites para investigação.");
  await expect(
    dialog.getByText(/O limite por payload não pode superar/),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Salvar política de captura" }),
  ).toBeDisabled();
  expect(calls.filter((call) => call.method() === "PUT")).toHaveLength(0);
  await dialog
    .getByLabel("Limite por operação (bytes)", { exact: false })
    .fill("262144");
  await expect(
    dialog.getByRole("button", { name: "Salvar política de captura" }),
  ).toBeEnabled();
});

test("política de preenchimento escolhe conexão autorizada e mantém valores após conflito 412", async ({
  page,
}) => {
  await monitorMocks(page);
  const writes: Request[] = [];
  await page.route("**/template-preparation-policy", async (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: preparationPolicy });
    writes.push(route.request());
    return route.fulfill({
      status: 412,
      json: {
        error: { message: "A política foi alterada por outro administrador." },
      },
    });
  });
  await page.goto("/tests/observability.fixture.html?view=preparation");
  await page.getByRole("button", { name: "Configurar preenchimento" }).click();
  const dialog = page.getByRole("dialog", { name: "Configurar preenchimento" });
  await expect(
    dialog.getByRole("option", { name: /Modelo indisponível/ }),
  ).toHaveAttribute("disabled", "");
  await dialog
    .getByRole("combobox", { name: "Modelo para o preenchimento" })
    .selectOption("model-ready");
  await expect(
    dialog.getByText("Conexão: connection-fixture", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByLabel("Saída máxima (tokens)", { exact: false })
    .fill("6000");
  await dialog
    .getByRole("combobox", { name: "Reparações permitidas" })
    .selectOption("1");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Atualizar limite e modelo da preparação.");
  await dialog
    .getByRole("button", { name: "Salvar política de preenchimento" })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "A política foi alterada por outro administrador.",
  );
  await expect(
    dialog.getByLabel("Saída máxima (tokens)", { exact: false }),
  ).toHaveValue("6000");
  await expect(
    dialog.getByRole("combobox", { name: "Modelo para o preenchimento" }),
  ).toHaveValue("model-ready");
  expect(writes[0].headers()["if-match"]).toBe("7");
  expect(writes[0].postDataJSON().settings).toMatchObject({
    model_id: "model-ready",
    max_output_tokens: 6000,
    max_repairs: 1,
  });
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByText("Descartar alterações não salvas?", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Continuar editando" }).click();
  await expect(
    dialog.getByLabel("Motivo da alteração", { exact: true }),
  ).toHaveValue("Atualizar limite e modelo da preparação.");
});

test("salvar flag de preenchimento bloqueia duplicação e confirma a nova versão", async ({
  page,
}) => {
  await monitorMocks(page);
  let release: () => void = () => {};
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  const writes: Request[] = [];
  await page.route("**/template-preparation-policy", async (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: preparationPolicy });
    writes.push(route.request());
    await wait;
    return route.fulfill({
      json: {
        ...preparationPolicy,
        version: 8,
        settings: route.request().postDataJSON().settings,
      },
    });
  });
  await page.goto("/tests/observability.fixture.html?view=preparation");
  await page.getByRole("button", { name: "Configurar preenchimento" }).click();
  const dialog = page.getByRole("dialog", { name: "Configurar preenchimento" });
  await dialog
    .getByRole("checkbox", { name: "Habilitar preenchimento assistido" })
    .uncheck();
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Pausar preenchimento enquanto investigamos.");
  await dialog
    .getByRole("button", { name: "Salvar política de preenchimento" })
    .click();
  await expect.poll(() => writes.length).toBe(1);
  await expect(
    dialog.getByRole("button", { name: "Salvando…" }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "Fechar", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  release();
  await expect(
    dialog.getByText("Configuração salva na versão 8.", { exact: true }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].postDataJSON().settings.enabled).toBe(false);
});

test("políticas são navegáveis por teclado e não transbordam em celular ou reflow de 200%", async ({
  page,
}, info) => {
  await monitorMocks(page);
  const dialog = await openPolicy(page);
  await dialog
    .getByRole("tab", { name: "Captura e retenção", exact: true })
    .focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    dialog.getByRole("tab", { name: "Acesso a payloads", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Home");
  await expect(
    dialog.getByRole("tab", { name: "Captura e retenção", exact: true }),
  ).toBeFocused();
  for (const viewport of [
    { width: 320, height: 820 },
    { width: 720, height: 480 },
    { width: 1440, height: 960 },
  ]) {
    await page.setViewportSize(viewport);
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    await expect(
      dialog.getByRole("button", { name: "Fechar", exact: true }),
    ).toBeVisible();
  }
  await page.screenshot({
    path: info.outputPath("capture-policy-desktop.png"),
    fullPage: true,
  });
});
