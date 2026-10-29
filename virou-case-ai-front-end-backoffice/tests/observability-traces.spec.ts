import { expect, test } from "@playwright/test";

import {
  event,
  feed,
  trace,
  contents,
  payload,
  monitorMocks,
  openTrace,
} from "./observability-test-data";

test("preserva filtros de serviço, nível, correlação e projeto e apresenta metadados reais", async ({
  page,
}) => {
  const requests: URL[] = [];
  await page.route("**/admin-api/v1/observability/events?*", (route) => {
    requests.push(new URL(route.request().url()));
    return route.fulfill({ json: feed });
  });
  await page.goto("/tests/observability.fixture.html");
  await expect(page.getByText(event.message, { exact: true })).toBeVisible();
  await expect(page.getByText("3822 ms", { exact: true })).toBeVisible();
  await page
    .getByRole("combobox", { name: "Serviço", exact: true })
    .selectOption("jev");
  await page
    .getByRole("combobox", { name: "Nível", exact: true })
    .selectOption("info");
  await page
    .getByLabel("ID da operação ou recomendação", { exact: true })
    .fill("journey-fixture");
  await page
    .getByLabel("ID do projeto", { exact: true })
    .fill("project-fixture");
  await page
    .getByRole("button", { name: "Aplicar filtros", exact: true })
    .click();
  await expect
    .poll(() => requests.at(-1)?.searchParams.get("correlation"))
    .toBe("journey-fixture");
  expect(requests.at(-1)?.searchParams.get("service")).toBe("jev");
  expect(requests.at(-1)?.searchParams.get("level")).toBe("info");
  expect(requests.at(-1)?.searchParams.get("project")).toBe("project-fixture");
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
});

test("filtra etapa, estado e datas UTC e mantém filtro inválido sem nova consulta", async ({
  page,
}) => {
  const calls = await monitorMocks(page);
  await page.goto("/tests/observability.fixture.html");
  await page.getByRole("tab", { name: "Integrações e payloads" }).click();
  await page
    .getByRole("combobox", { name: "Etapa", exact: true })
    .selectOption("preparation");
  await page.getByLabel("Estado registrado", { exact: true }).fill("failed");
  await page
    .getByLabel("Desde (horário local)", { exact: true })
    .fill("2026-10-27T10:00");
  await page
    .getByLabel("Até (horário local)", { exact: true })
    .fill("2026-10-28T18:30");
  await page.getByRole("button", { name: "Aplicar filtros" }).click();
  await expect
    .poll(() => new URL(calls.at(-1)!.url()).searchParams.get("state"))
    .toBe("failed");
  const url = new URL(calls.at(-1)!.url());
  expect(url.searchParams.get("phase")).toBe("preparation");
  const dates = await page.evaluate(() => [
    new Date("2026-10-27T10:00").toISOString(),
    new Date("2026-10-28T18:30").toISOString(),
  ]);
  expect(url.searchParams.get("since")).toBe(dates[0]);
  expect(url.searchParams.get("until")).toBe(dates[1]);
  await page.getByRole("button", { name: "Pausar", exact: true }).click();
  const before = calls.length;
  await page
    .getByLabel("Desde (horário local)", { exact: true })
    .fill("2026-10-30T10:00");
  await page.getByRole("button", { name: "Aplicar filtros" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "A data inicial não pode ser posterior",
  );
  expect(calls.length).toBe(before);
});

test("pausa cancela o poller; atualização manual e troca de visão não duplicam consultas", async ({
  page,
}) => {
  await page.clock.install();
  const calls = await monitorMocks(page);
  const count = (kind: string) =>
    calls.filter((call) =>
      new URL(call.url()).pathname.endsWith(`/observability/${kind}`),
    ).length;
  await page.goto("/tests/observability.fixture.html");
  await expect(page.getByText(event.message, { exact: true })).toBeVisible();
  expect(count("events")).toBe(1);
  await page.getByRole("button", { name: "Pausar", exact: true }).click();
  await page.clock.fastForward(6000);
  expect(count("events")).toBe(1);
  await page.getByRole("button", { name: "Atualizar monitor" }).click();
  await expect.poll(() => count("events")).toBe(2);
  await page.getByRole("tab", { name: "Integrações e payloads" }).click();
  await expect(
    page.getByRole("button", { name: "Ver detalhes da integração" }),
  ).toBeVisible();
  await page.clock.fastForward(6000);
  expect(count("traces")).toBe(1);
  await page.getByRole("button", { name: "Retomar", exact: true }).click();
  await expect.poll(() => count("traces")).toBe(2);
  await expect(
    page.getByRole("button", { name: "Atualizar monitor" }),
  ).toBeEnabled();
  await page.clock.fastForward(3000);
  await expect.poll(() => count("traces")).toBe(3);
  await page.getByRole("tab", { name: "Eventos operacionais" }).click();
  await expect.poll(() => count("events")).toBe(3);
  await expect(
    page.getByRole("button", { name: "Atualizar monitor" }),
  ).toBeEnabled();
  await page.clock.fastForward(3000);
  await expect.poll(() => count("events")).toBe(4);
  expect(count("traces")).toBe(3);
});

test("carrega só a seção aberta, separa resposta e resultado e mostra falha mesmo com HTTP 200", async ({
  page,
}) => {
  const calls = await monitorMocks(page);
  await page.goto("/tests/observability.fixture.html");
  await page.getByRole("tab", { name: "Integrações e payloads" }).click();
  await expect(
    page.getByRole("button", { name: "Ver detalhes da integração" }),
  ).toBeVisible();
  expect(calls.some((call) => call.url().includes("/payload?"))).toBe(false);
  await page
    .getByRole("button", { name: "Ver detalhes da integração" })
    .click();
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(
    dialog.getByLabel("JSON protegido: Request", { exact: true }),
  ).toContainText("[CONTEÚDO PROTEGIDO]");
  await expect(
    dialog.getByRole("heading", { name: "Preenchimento · failed" }),
  ).toBeVisible();
  await expect(
    dialog.locator(".trace-record-summary").getByText(/HTTP 200/),
  ).toBeVisible();
  expect(
    calls
      .filter((call) => call.url().includes("/payload?"))
      .map((call) => new URL(call.url()).searchParams.get("section")),
  ).toEqual(["request"]);
  await dialog.getByRole("tab", { name: "Response", exact: true }).click();
  await expect(
    dialog.getByLabel("JSON protegido: Response", { exact: true }),
  ).toContainText('"input_tokens": 42');
  await dialog
    .getByRole("tab", { name: "Resultado mapeado", exact: true })
    .click();
  await expect(
    dialog.getByLabel("JSON protegido: Resultado mapeado", { exact: true }),
  ).toContainText('"validation": "failed"');
  expect(
    calls
      .filter((call) => call.url().includes("/payload?"))
      .map((call) => new URL(call.url()).searchParams.get("section")),
  ).toEqual(["request", "response", "normalized"]);
  await dialog.getByRole("tab", { name: "Campos", exact: true }).click();
  const rows = dialog.getByRole("region", {
    name: "Comparação dos campos por identificador",
  });
  await expect(
    rows.getByRole("row").filter({ hasText: "auth_contract" }),
  ).toContainText("Editado pelo usuário");
  await expect(
    rows.getByRole("row").filter({ hasText: "auth_contract" }),
  ).toContainText("v5");
  await expect(
    rows.getByRole("row").filter({ hasText: "intent" }),
  ).toContainText("Não registrado");
});

test("busca no JSON e só copia o conteúdo exibido após autorização auditada", async ({
  page,
}) => {
  const calls = await monitorMocks(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          (window as unknown as { copied: string[] }).copied.push(text);
        },
      },
    });
    (window as unknown as { copied: string[] }).copied = [];
  });
  await openTrace(page);
  await expect(
    page.getByLabel("JSON protegido: Request", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Buscar neste JSON", { exact: true }).fill("model");
  await expect(
    page.getByText("1 de 2 ocorrências", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Próxima ocorrência" }).click();
  await expect(
    page.getByText("2 de 2 ocorrências", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Copiar JSON protegido" }).click();
  await expect(
    page.getByRole("button", { name: "JSON copiado" }),
  ).toBeVisible();
  const copy = calls.find((call) => call.url().endsWith("/copy"))!;
  expect(copy.method()).toBe("POST");
  expect(copy.postDataJSON()).toEqual({ section: "request" });
  expect(copy.headers()["x-csrf-token"]).toBe("csrf-fixture");
  expect(copy.headers()["idempotency-key"]).toBeTruthy();
  expect(
    await page.evaluate(
      () => (window as unknown as { copied: string[] }).copied,
    ),
  ).toEqual([JSON.stringify(contents.request, null, 2)]);
});

test("revogação de permissão nega cópia e não escreve no clipboard", async ({
  page,
}) => {
  await monitorMocks(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async () => {
          (window as unknown as { copied: boolean }).copied = true;
        },
      },
    });
  });
  await page.route("**/observability/traces/*/copy", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { message: "Permissão específica revogada." } },
    }),
  );
  await openTrace(page);
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Copiar JSON protegido" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Permissão específica revogada.",
  );
  expect(
    await page.evaluate(
      () => (window as unknown as { copied?: boolean }).copied,
    ),
  ).toBeUndefined();
});

test("conteúdo alterado entre leitura e cópia não é copiado como se fosse o exibido", async ({
  page,
}) => {
  await monitorMocks(page);
  await page.route("**/observability/traces/*/copy", (route) =>
    route.fulfill({
      json: { ...payload("request"), payload: { new_capture: true } },
    }),
  );
  await openTrace(page);
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Copiar JSON protegido" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "O conteúdo protegido mudou",
  );
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({
      json: { ...payload("request"), payload: { new_capture: true } },
    }),
  );
  await page.getByRole("button", { name: "Atualizar conteúdo" }).click();
  await expect(
    page.getByLabel("JSON protegido: Request", { exact: true }),
  ).toContainText("new_capture");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("sem permissão continua mostrando metadados sem buscar nem copiar payloads", async ({
  page,
}) => {
  const calls = await monitorMocks(page, false);
  await openTrace(page);
  await expect(
    page.getByText(/Para inspecionar ou copiar payloads/),
  ).toBeVisible();
  for (const name of ["Response", "Resultado mapeado", "Campos"])
    await page.getByRole("tab", { name, exact: true }).click();
  expect(
    calls.some(
      (call) =>
        call.url().includes("/payload?") || call.url().endsWith("/copy"),
    ),
  ).toBe(false);
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toHaveCount(0);
});

test("ausência de captura e truncamento são explícitos, sem reconstruir dados", async ({
  page,
}) => {
  const calls = await monitorMocks(page, true, {
    ...trace,
    capture_state: "not_captured",
    available_sections: [],
  });
  await openTrace(page);
  await expect(
    page
      .getByRole("dialog")
      .getByText("Payload não capturado", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  expect(calls.some((call) => call.url().includes("/payload?"))).toBe(false);
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.route("**/observability/traces?*", (route) =>
    route.fulfill({
      json: {
        ...feed,
        items: [{ ...trace, capture_state: "truncated", truncated: true }],
        can_inspect_payloads: true,
      },
    }),
  );
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({
      json: {
        ...payload("request"),
        capture_state: "truncated",
        truncated_paths: ["messages[1]"],
      },
    }),
  );
  await page.getByRole("button", { name: "Atualizar monitor" }).click();
  await expect(
    page.getByText("Captura truncada", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Ver detalhes da integração" })
    .click();
  await expect(
    page.getByText("Esta captura é parcial.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Os limites afetaram somente a cópia de observabilidade.", {
      exact: true,
    }),
  ).toBeVisible();
});

test("agrupa tentativas na jornada, preserva contexto e permite teclado em celular", async ({
  page,
}, info) => {
  await monitorMocks(page);
  await openTrace(page);
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(dialog.locator(".trace-timeline > ol > li")).toHaveCount(4);
  await dialog
    .getByText("Contexto e versões da integração", { exact: true })
    .click();
  await expect(
    dialog.getByText("journey-fixture", { exact: false }).first(),
  ).toBeVisible();
  await dialog
    .getByText("Contexto e versões da integração", { exact: true })
    .click();
  await dialog.locator(".trace-timeline > summary").click();
  await dialog
    .getByRole("button", { name: "Tentativa 2 · openai ready · exchange" })
    .click();
  await expect(
    dialog.getByRole("heading", { name: "Preenchimento · ready" }),
  ).toBeVisible();
  await dialog.locator(".trace-timeline > summary").click();
  await dialog.getByRole("tab", { name: "Request", exact: true }).focus();
  await page.keyboard.press("End");
  await expect(
    dialog.getByRole("tab", { name: "Campos", exact: true }),
  ).toBeFocused();
  await expect(
    dialog.getByRole("tab", { name: "Campos", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await expect(
      dialog.getByRole("button", { name: "Fechar", exact: true }),
    ).toBeVisible();
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
      `${width}px no diálogo`,
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
      `${width}px na página`,
    ).toBe(true);
  }
  await page.screenshot({
    path: info.outputPath("trace-campos-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 375, height: 820 });
  await page.screenshot({
    path: info.outputPath("trace-campos-mobile.png"),
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Ver detalhes da integração" }),
  ).toBeFocused();
});

test("erro de consulta mantém eventos anteriores e permite recuperação manual", async ({
  page,
}) => {
  await monitorMocks(page);
  await page.goto("/tests/observability.fixture.html");
  await expect(page.getByText(event.message, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Pausar", exact: true }).click();
  await page.route("**/observability/events?*", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "Consulta temporariamente indisponível." } },
    }),
  );
  await page.getByRole("button", { name: "Atualizar monitor" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Consulta temporariamente indisponível",
  );
  await expect(page.getByText(event.message, { exact: true })).toBeVisible();
  await expect(
    page.getByText(/use Atualizar monitor para tentar novamente/),
  ).toBeVisible();
  await page.route("**/observability/events?*", (route) =>
    route.fulfill({ json: feed }),
  );
  await page.getByRole("button", { name: "Atualizar monitor" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("cursor opaco mantém filtros e histórico não inicia polling até voltar ao vivo", async ({
  page,
}) => {
  await page.clock.install();
  const calls = await monitorMocks(page);
  await page.route("**/observability/traces?*", (route) => {
    calls.push(route.request());
    const historical = new URL(route.request().url()).searchParams.has(
      "before",
    );
    return route.fulfill({
      json: {
        ...feed,
        items: [trace],
        next_cursor: historical ? null : "cursor:opaque==",
        can_inspect_payloads: true,
      },
    });
  });
  await page.goto("/tests/observability.fixture.html");
  await page.getByRole("tab", { name: "Integrações e payloads" }).click();
  await page
    .getByRole("combobox", { name: "Etapa", exact: true })
    .selectOption("preparation");
  await expect(
    page.getByRole("button", { name: "Integrações anteriores" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Integrações anteriores" }).click();
  await expect(page.getByText(/Histórico ·/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Atualizar monitor" }),
  ).toBeEnabled();
  const before = calls.length;
  const last = new URL(calls.at(-1)!.url());
  expect(last.searchParams.get("before")).toBe("cursor:opaque==");
  expect(last.searchParams.get("phase")).toBe("preparation");
  await page.clock.fastForward(6000);
  expect(calls.length).toBe(before);
  await page.getByRole("button", { name: "Voltar ao vivo" }).click();
  await expect
    .poll(() => new URL(calls.at(-1)!.url()).searchParams.has("before"))
    .toBe(false);
  expect(new URL(calls.at(-1)!.url()).searchParams.get("phase")).toBe(
    "preparation",
  );
});

test("falha de leitura do payload mantém metadados e permite tentar novamente sem copiar cache", async ({
  page,
}) => {
  await monitorMocks(page);
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({
      status: 403,
      json: { error: { message: "Inspeção de payload não autorizada." } },
    }),
  );
  await openTrace(page);
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(dialog.getByRole("alert")).toContainText(
    "Inspeção de payload não autorizada.",
  );
  await expect(
    dialog.getByRole("heading", { name: "Preenchimento · failed" }),
  ).toBeVisible();
  await expect(
    dialog.getByLabel("JSON protegido: Request", { exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({ json: payload("request") }),
  );
  await dialog
    .getByRole("button", { name: "Tentar carregar novamente" })
    .click();
  await expect(
    dialog.getByLabel("JSON protegido: Request", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});

test("conteúdo longo é carregado sob demanda e texto de JSON não executa HTML", async ({
  page,
}) => {
  await monitorMocks(page);
  const unsafe = '<img src="x" onerror="window.traceExecuted=true">';
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({
      json: {
        ...payload("request"),
        payload: {
          value: unsafe,
          technical_ids: Array.from(
            { length: 1500 },
            (_, index) => `field_${index}`,
          ),
        },
      },
    }),
  );
  await openTrace(page);
  const json = page.getByLabel("JSON protegido: Request", { exact: true });
  await expect(json).toContainText("field_1499");
  expect(await json.locator("img").count()).toBe(0);
  expect(
    await page.evaluate(
      () => (window as unknown as { traceExecuted?: boolean }).traceExecuted,
    ),
  ).toBeUndefined();
  expect(
    await json.evaluate(
      (element) => element.scrollHeight > element.clientHeight,
    ),
  ).toBe(true);
});

test("payload expirado mantém contexto e impede cópia, sem anunciar conteúdo disponível", async ({
  page,
}) => {
  await monitorMocks(page, true, {
    ...trace,
    capture_state: "expired",
    available_sections: [],
  });
  await openTrace(page);
  await expect(
    page.getByRole("dialog").getByText("Payload expirado", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(
    page.getByText("Request: payload expirado · Response: payload expirado", {
      exact: true,
    }),
  ).toBeVisible();
});

test("request pendurada expira e o detalhe pode ser consultado novamente", async ({
  page,
}) => {
  await page.clock.install();
  await monitorMocks(page);
  let release: () => void = () => {};
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/observability/traces/*/payload?*", async (route) => {
    await wait;
    await route.fulfill({ json: payload("request") }).catch(() => {});
  });
  await openTrace(page);
  await expect(
    page.getByText("Carregando conteúdo protegido…", { exact: true }),
  ).toBeVisible();
  await page.clock.fastForward(12000);
  await expect(page.getByRole("alert")).toContainText(
    "A consulta demorou mais que o esperado",
  );
  await expect(
    page.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  release();
  await page.route("**/observability/traces/*/payload?*", (route) =>
    route.fulfill({ json: payload("request") }),
  );
  await page.getByRole("button", { name: "Tentar carregar novamente" }).click();
  await expect(
    page.getByLabel("JSON protegido: Request", { exact: true }),
  ).toBeVisible();
});

test("metadados opcionais ausentes e nulos não geram HTTP, tentativa ou versão fictícios", async ({
  page,
}) => {
  await monitorMocks(page, true, {
    ...trace,
    http_status: undefined,
    attempt: undefined,
    duration_ms: undefined,
    draft_revision: null,
    template_versions: null,
    model: null,
    capture_state: "disabled",
    available_sections: [],
  });
  await openTrace(page);
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(dialog.locator(".trace-record-summary")).toContainText(
    "Tentativa não informada",
  );
  await expect(dialog.locator(".trace-record-summary")).not.toContainText(
    "HTTP",
  );
  await expect(dialog.locator(".trace-record-summary")).toContainText(
    "Duração não informada",
  );
  await expect(
    dialog.getByText("Captura desabilitada", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  await dialog
    .getByText("Contexto e versões da integração", { exact: true })
    .click();
  await expect(dialog.locator(".trace-context")).toContainText(
    "HTTP não informado",
  );
  await expect(dialog.locator(".trace-context")).toContainText(
    "Revisão não informada",
  );
});

test("estado por seção distingue Response indisponível e não cria payload sintético", async ({
  page,
}) => {
  const calls = await monitorMocks(page, true, {
    ...trace,
    available_sections: ["request"],
    section_states: { request: "protected", response: "unavailable" },
    capture_reason: "NON_JSON_RESPONSE",
  });
  await openTrace(page);
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(
    dialog.getByLabel("JSON protegido: Request", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByText(/A resposta recebida não pôde/)).toHaveCount(0);
  await dialog.getByRole("tab", { name: "Response", exact: true }).click();
  await expect(
    dialog.getByText("Payload indisponível", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText(/A resposta recebida não pôde ser interpretada como JSON/),
  ).toBeVisible();
  await expect(
    dialog.getByLabel("JSON protegido: Response", { exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Copiar JSON protegido" }),
  ).toBeDisabled();
  expect(
    calls
      .filter((call) => call.url().includes("/payload?"))
      .map((call) => new URL(call.url()).searchParams.get("section")),
  ).toEqual(["request"]);
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(
    page.getByText(
      "Request: protegido e disponível · Response: payload indisponível",
      { exact: true },
    ),
  ).toBeVisible();
});

test("abas ligam exchange e normalized da mesma tentativa sem misturar outras etapas ou jornadas", async ({
  page,
}) => {
  const exchange = {
    ...trace,
    id: "exchange-one",
    span_id: "span-exchange-one",
    status: "received",
    available_sections: ["request", "response"],
  };
  const normalized = {
    ...trace,
    id: "normalized-one",
    span_id: "span-normalized-one",
    parent_span_id: exchange.span_id,
    sequence: 3,
    direction: "normalized",
    available_sections: ["normalized", "fields"],
  };
  const unrelated = [
    { ...normalized, id: "normalized-two", attempt: 2, sequence: 6 },
    {
      ...normalized,
      id: "another-operation",
      operation_id: "another-operation",
      sequence: 7,
    },
    { ...normalized, id: "another-stage", stage: "review", sequence: 8 },
    {
      ...normalized,
      id: "another-journey",
      trace_id: "another-journey",
      sequence: 9,
    },
  ];
  await monitorMocks(page, true, exchange);
  await page.route("**/observability/traces/*/timeline", (route) =>
    route.fulfill({
      json: {
        items: [exchange, normalized, ...unrelated],
        can_inspect_payloads: true,
      },
    }),
  );
  const requested: { id: string; section: string }[] = [];
  await page.route("**/observability/traces/*/payload?*", (route) => {
    const url = new URL(route.request().url());
    const id = url.pathname.split("/").at(-2)!;
    const section = url.searchParams.get("section")!;
    requested.push({ id, section });
    return route.fulfill({
      json: { ...payload(section, id), payload: { record_id: id, section } },
    });
  });
  await openTrace(page);
  const dialog = page.getByRole("dialog", { name: "Detalhes da integração" });
  await expect(
    dialog.getByLabel("JSON protegido: Request", { exact: true }),
  ).toContainText("exchange-one");
  await dialog
    .getByRole("tab", { name: "Resultado mapeado", exact: true })
    .click();
  await expect(
    dialog.getByLabel("JSON protegido: Resultado mapeado", { exact: true }),
  ).toContainText("normalized-one");
  await dialog.getByRole("tab", { name: "Response", exact: true }).click();
  await expect(
    dialog.getByLabel("JSON protegido: Response", { exact: true }),
  ).toContainText("exchange-one");
  await dialog.getByRole("tab", { name: "Campos", exact: true }).click();
  await expect(
    dialog.getByLabel("JSON protegido: Campos", { exact: true }),
  ).toContainText("normalized-one");
  expect(requested).toEqual([
    { id: "exchange-one", section: "request" },
    { id: "normalized-one", section: "normalized" },
    { id: "exchange-one", section: "response" },
    { id: "normalized-one", section: "fields" },
  ]);
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await page.route("**/observability/traces?*", (route) =>
    route.fulfill({
      json: { ...feed, items: [normalized], can_inspect_payloads: true },
    }),
  );
  await page.getByRole("button", { name: "Atualizar monitor" }).click();
  await page
    .getByRole("button", { name: "Ver detalhes da integração" })
    .click();
  await expect(
    dialog.getByRole("tab", { name: "Resultado mapeado", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    dialog.getByLabel("JSON protegido: Resultado mapeado", { exact: true }),
  ).toContainText("normalized-one");
});
