import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type BrowserContext } from "@playwright/test";
import type { AgentConfiguration } from "../src/agent-configuration-fields";

type Policy = {
  version: number;
  state?: string;
  settings: Record<string, unknown>;
  defaults?: Record<string, unknown>;
};
let sessionCookies: Awaited<ReturnType<BrowserContext["cookies"]>> | undefined;

async function read<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (value) => {
    const response = await fetch(`/admin-api/v1${value}`);
    if (!response.ok) throw new Error(`GET ${value}: ${response.status}`);
    return response.json();
  }, path);
}

async function openAgents(page: Page) {
  await page
    .getByRole("button", { name: "Agentes de IA", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Configurar construção", exact: true }),
  ).toBeEnabled();
}

async function login(page: Page) {
  if (sessionCookies) {
    await page.context().addCookies(sessionCookies);
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Contas e acesso" }),
    ).toBeVisible();
    await openAgents(page);
    return;
  }
  await page.goto("/");
  await page.getByLabel("Login", { exact: true }).fill("operator-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-operator-only-password");
  await page.getByRole("button", { name: "Entrar no backoffice" }).click();
  await expect(
    page.getByRole("heading", { name: "Contas e acesso" }),
  ).toBeVisible();
  sessionCookies = await page.context().cookies();
  await openAgents(page);
}

test("construção salva e recarrega parâmetros pela API real, preservando a política Jev", async ({
  page,
}) => {
  await login(page);
  const metadata = await read<AgentConfiguration>(
    page,
    "/ai/configuration-schema",
  );
  const original = await read<Policy>(page, "/integrations/jev");
  await page
    .getByRole("button", { name: "Configurar construção", exact: true })
    .click();
  let dialog = page.getByRole("dialog", { name: "Configurar construção" });
  const values: Record<string, [string, number]> = {
    max_generation_calls: ["Máximo de chamadas por operação", 9],
    max_generation_context_bytes: [
      "Contexto máximo da geração (bytes)",
      200000,
    ],
    max_truncation_retries: ["Recuperações de resposta cortada", 4],
    max_response_repairs: ["Reparações de formato", 3],
    max_batch_characters: ["Tamanho máximo do lote (caracteres)", 31000],
    minimum_batch_characters: ["Tamanho mínimo do lote (caracteres)", 900],
    batch_shrink_factor: ["Fator de redução após corte", 0.4],
  };
  for (const [key, [label, value]] of Object.entries(values)) {
    const field = dialog.getByLabel(label, { exact: true });
    await expect(field).toHaveAttribute(
      "min",
      String(metadata.construction.schema.properties[key].minimum),
    );
    await expect(field).toHaveAttribute(
      "max",
      String(metadata.construction.schema.properties[key].maximum),
    );
    await field.fill(String(value));
  }
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Validar recuperação de respostas cortadas no laboratório E2E.");
  const requestPromise = page.waitForRequest(
    (request) =>
      request.url().endsWith("/integrations/jev") && request.method() === "PUT",
  );
  await dialog
    .getByRole("button", { name: "Salvar política de construção" })
    .click();
  const request = await requestPromise;
  await expect(dialog.getByRole("status")).toHaveText(
    `Configuração salva na versão ${original.version + 1}.`,
  );
  expect(request.headers()["if-match"]).toBe(String(original.version));
  expect(request.headers()["x-csrf-token"]).toBeTruthy();
  expect(request.headers()["idempotency-key"]).toBeTruthy();
  const submitted = request.postDataJSON();
  expect(submitted).not.toHaveProperty("credential");
  expect(submitted).not.toHaveProperty("token");
  expect(submitted.settings).toEqual({
    ...metadata.construction.defaults,
    ...original.defaults,
    ...original.settings,
    ...Object.fromEntries(
      Object.entries(values).map(([key, [, value]]) => [key, value]),
    ),
  });
  const stored = await read<Policy>(page, "/integrations/jev");
  expect(stored.settings).toEqual(submitted.settings);
  expect(stored.version).toBe(original.version + 1);
  expect(stored.state).toBe(
    original.state === "missing" ? "draft" : original.state,
  );
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Contas e acesso" }),
  ).toBeVisible();
  await openAgents(page);
  await page
    .getByRole("button", { name: "Configurar construção", exact: true })
    .click();
  dialog = page.getByRole("dialog", { name: "Configurar construção" });
  for (const [label, value] of Object.values(values))
    await expect(dialog.getByLabel(label, { exact: true })).toHaveValue(
      String(value),
    );
});

test("construção preserva a edição quando outra gravação real muda a versão", async ({
  page,
}) => {
  await login(page);
  const original = await read<Policy>(page, "/integrations/jev");
  await page
    .getByRole("button", { name: "Configurar construção", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Configurar construção" });
  const alternate = original.settings.max_generation_calls === 8 ? 7 : 8;
  const concurrent = await page.evaluate(
    async ({ original, alternate }) => {
      const user = await (await fetch("/admin-api/v1/me")).json();
      const response = await fetch("/admin-api/v1/integrations/jev", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": user.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": String(original.version),
        },
        body: JSON.stringify({
          settings: {
            ...original.defaults,
            ...original.settings,
            max_generation_calls: alternate,
          },
          reason:
            "Alteração concorrente sintética para validar controle de versão.",
        }),
      });
      return { status: response.status, data: await response.json() };
    },
    { original, alternate },
  );
  expect(concurrent.status).toBe(200);
  await dialog
    .getByLabel("Recuperações de resposta cortada", { exact: true })
    .fill("6");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill("Edição local preservada durante conflito de versão.");
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith("/integrations/jev") &&
      response.request().method() === "PUT",
  );
  await dialog
    .getByRole("button", { name: "Salvar política de construção" })
    .click();
  expect((await responsePromise).status()).toBe(412);
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(
    dialog.getByLabel("Recuperações de resposta cortada", { exact: true }),
  ).toHaveValue("6");
  await expect(
    dialog.getByLabel("Motivo da alteração", { exact: true }),
  ).toHaveValue("Edição local preservada durante conflito de versão.");
  expect(
    (await read<Policy>(page, "/integrations/jev")).settings
      .max_generation_calls,
  ).toBe(alternate);
});

test("preenchimento salva limites ampliados e recarrega a política real", async ({
  page,
}) => {
  await login(page);
  const metadata = await read<AgentConfiguration>(
    page,
    "/ai/configuration-schema",
  );
  const original = await read<Policy>(page, "/template-preparation-policy");
  await page
    .getByRole("button", { name: "Configurar preenchimento", exact: true })
    .click();
  let dialog = page.getByRole("dialog", { name: "Configurar preenchimento" });
  await expect(
    dialog.getByLabel("Saída máxima (tokens)", { exact: true }),
  ).toHaveAttribute(
    "max",
    String(metadata.preparation.schema.properties.max_output_tokens.maximum),
  );
  await dialog
    .getByLabel("Contexto máximo (bytes)", { exact: true })
    .fill("40000");
  await dialog
    .getByLabel("Saída máxima (tokens)", { exact: true })
    .fill("32768");
  await dialog
    .getByRole("combobox", { name: /Reparações permitidas/ })
    .selectOption("3");
  await dialog
    .getByLabel("Motivo da alteração", { exact: true })
    .fill(
      "Ampliar a saída e testar persistência da preparação no laboratório.",
    );
  const requestPromise = page.waitForRequest(
    (request) =>
      request.url().endsWith("/template-preparation-policy") &&
      request.method() === "PUT",
  );
  await dialog
    .getByRole("button", { name: "Salvar política de preenchimento" })
    .click();
  const request = await requestPromise;
  await expect(dialog.getByRole("status")).toHaveText(
    `Configuração salva na versão ${original.version + 1}.`,
  );
  expect(request.headers()["if-match"]).toBe(String(original.version));
  expect(request.headers()["idempotency-key"]).toBeTruthy();
  const stored = await read<Policy>(page, "/template-preparation-policy");
  expect(stored.settings).toEqual({
    ...original.settings,
    max_context_bytes: 40000,
    max_output_tokens: 32768,
    max_repairs: 3,
  });
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Contas e acesso" }),
  ).toBeVisible();
  await openAgents(page);
  await page
    .getByRole("button", { name: "Configurar preenchimento", exact: true })
    .click();
  dialog = page.getByRole("dialog", { name: "Configurar preenchimento" });
  await expect(
    dialog.getByLabel("Contexto máximo (bytes)", { exact: true }),
  ).toHaveValue("40000");
  await expect(
    dialog.getByLabel("Saída máxima (tokens)", { exact: true }),
  ).toHaveValue("32768");
  await expect(
    dialog.getByRole("combobox", { name: /Reparações permitidas/ }),
  ).toHaveValue("3");
});

test("configurações de construção e preenchimento permitem teclado, foco e telas estreitas", async ({
  page,
}, info) => {
  await login(page);
  for (const title of ["Configurar construção", "Configurar preenchimento"]) {
    const trigger = page.getByRole("button", { name: title, exact: true });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: title });
    await expect(dialog).toBeVisible();
    await expect
      .poll(() =>
        dialog.evaluate((element) => element.contains(document.activeElement)),
      )
      .toBe(true);
    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 960 });
      expect(
        await dialog.evaluate(
          (element) => element.scrollWidth <= element.clientWidth + 1,
        ),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(
          `${title.includes("construção") ? "construction" : "preparation"}-${width}.png`,
        ),
        fullPage: true,
      });
    }
    const scan = await new AxeBuilder({ page })
      .include(".integration-dialog")
      .analyze();
    expect(scan.violations).toEqual([]);
    await page.evaluate(() => {
      document.documentElement.style.zoom = "2";
    });
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
    await page.evaluate(() => {
      document.documentElement.style.zoom = "";
    });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }
});
