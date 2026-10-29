import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {
  authTemplate,
  preparationProject,
  preparationWorkspace,
} from "./templatePreparationFixture";

test.use({ viewport: { width: 1440, height: 950 } });

const retryDialog = (page: Page) =>
  page.getByRole("dialog", {
    name: "Tentar preencher novamente?",
    exact: true,
  });
const retryButton = (page: Page) =>
  page.getByRole("button", {
    name: "Tentar preencher novamente",
    exact: true,
  });
const confirmButton = (page: Page) =>
  retryDialog(page).getByRole("button", {
    name: "Confirmar nova tentativa",
    exact: true,
  });
const fillPath = `**/api/v1/projects/${preparationProject}/build-fill`;
const draftPath = `**/api/v1/projects/${preparationProject}/build-draft`;

async function failedTemplate(page: Page) {
  const fixture = await preparationWorkspace(page, "templates");
  fixture.state.autoFill = false;
  await page
    .getByRole("checkbox", { name: `Selecionar ${authTemplate.title}` })
    .check();
  await expect.poll(() => fixture.state.fills.length).toBe(1);
  const attachment = fixture.state.draft.attachments[0];
  attachment.fill_state = "failed";
  attachment.fill_message =
    "Não foi possível preencher este template. Suas edições foram preservadas.";
  attachment.fields.interaction = "Minha edição manual deve continuar aqui.";
  attachment.field_values = {
    interaction: {
      field_id: "interaction",
      value: attachment.fields.interaction,
      edited_by_user: true,
      state: "confirmed",
      source_type: "user",
      source_refs: [],
      updated_at: "2026-10-28T16:00:00Z",
    },
  };
  fixture.state.version++;
  await page.reload();
  await expect(retryButton(page)).toBeVisible();
  return fixture;
}

test("modal estilizado cancela por botão, ESC e fechar com foco seguro e sem chamada", async ({
  page,
}, info) => {
  const { state } = await failedTemplate(page);
  const nativeDialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    nativeDialogs.push(dialog.type());
    await dialog.dismiss();
  });
  const saved = structuredClone(state.draft);
  await retryButton(page).click();
  const dialog = retryDialog(page);
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAccessibleDescription(
    `O template ${authTemplate.title} será preenchido novamente com o contexto salvo deste pedido.`,
  );
  await expect(
    dialog.getByText(authTemplate.title, { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText(/Uma nova tentativa pode consumir/),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Cancelar", exact: true }),
  ).toBeFocused();
  for (let index = 0; index < 6; index++) {
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
  }
  await dialog.getByRole("button", { name: "Fechar", exact: true }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(confirmButton(page)).toBeFocused();
  await dialog.getByRole("button", { name: "Cancelar", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(retryButton(page)).toBeFocused();
  await retryButton(page).press("Enter");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(retryButton(page)).toBeFocused();
  await retryButton(page).click();
  await expect(page.locator(".cm-content")).toBeVisible();
  await page.screenshot({
    path: info.outputPath("01-confirmacao-desktop.png"),
    fullPage: false,
  });
  await dialog.getByRole("button", { name: "Fechar", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(retryButton(page)).toBeFocused();
  expect(
    state.requests.filter((request) => request.path.endsWith("/build-fill")),
  ).toHaveLength(0);
  expect(state.draft).toEqual(saved);
  expect(nativeDialogs).toEqual([]);
});

test("confirmar protege clique duplo e ESC durante rede; retorno atualiza só o template escolhido", async ({
  page,
}) => {
  const { state } = await failedTemplate(page);
  state.autoFill = true;
  let requests = 0;
  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(fillPath, async (route) => {
    requests++;
    await pending;
    await route.fallback();
  });
  await retryButton(page).click();
  await confirmButton(page).evaluate((element) => {
    (element as HTMLButtonElement).click();
    (element as HTMLButtonElement).click();
  });
  await expect.poll(() => requests).toBe(1);
  const dialog = retryDialog(page);
  await expect(
    dialog.getByText("Solicitando nova tentativa…", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Cancelar", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "Fechar", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "Aguarde…", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  expect(state.fills).toHaveLength(1);
  release();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => state.fills.length).toBe(2);
  await expect.poll(() => state.draft.attachments[0].fill_state).toBe("ready");
  expect(state.fills[1].ids).toEqual([authTemplate.id]);
  expect(state.draft.attachments[0].fields.interaction).toBe(
    "Minha edição manual deve continuar aqui.",
  );
  const request = state.requests.find((item) =>
    item.path.endsWith("/build-fill"),
  )!;
  expect(request.body.attachment_ids).toEqual([authTemplate.id]);
  expect(request.key).not.toBe("");
  expect(request.csrf).toBe("template-csrf");
  expect(requests).toBe(1);
  await expect(
    page.getByRole("heading", {
      name: "Templates para este pedido",
      exact: true,
    }),
  ).toBeFocused();
});

test("erro definitivo permanece no modal com referência segura e permite confirmar de novo", async ({
  page,
}, info) => {
  const { state } = await failedTemplate(page);
  state.autoFill = true;
  const versions: number[] = [];
  await page.route(fillPath, async (route) => {
    versions.push(route.request().postDataJSON().expected_draft_revision);
    if (versions.length === 1) {
      state.version++;
      await route.fulfill({
        status: 412,
        json: {
          error: {
            code: "DRAFT_CONFLICT",
            message: "O pedido mudou. Confira o rascunho e tente novamente.",
            request_id: "retry-request-01",
          },
        },
      });
      return;
    }
    await route.fallback();
  });
  await retryButton(page).click();
  await confirmButton(page).click();
  const dialog = retryDialog(page);
  await expect(dialog.getByRole("alert")).toContainText("O pedido mudou.");
  await expect(
    dialog.getByText("Referência para suporte: retry-request-01"),
  ).toBeVisible();
  await expect(dialog.locator(".template-retry-error")).toBeFocused();
  await expect(confirmButton(page)).toBeEnabled();
  await page.screenshot({
    path: info.outputPath("02-erro-recuperavel.png"),
    fullPage: false,
  });
  expect(state.fills).toHaveLength(1);
  await confirmButton(page).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => state.fills.length).toBe(2);
  expect(versions[1]).toBe(versions[0] + 1);
});

test("resposta perdida verifica o mesmo payload e chave mesmo após revisão avançar", async ({
  page,
}, info) => {
  const { state, metadata } = await failedTemplate(page);
  const requests: { payload: unknown; key: string }[] = [];
  await page.route(fillPath, async (route) => {
    requests.push({
      payload: route.request().postDataJSON(),
      key: route.request().headers()["idempotency-key"],
    });
    if (requests.length === 1) {
      const fill = {
        id: "fill-2",
        ids: [authTemplate.id],
        objective: state.draft.objective,
      };
      state.fills.push(fill);
      state.draft.attachments[0].preparation_id = fill.id;
      state.version++;
      await route.abort("failed");
      return;
    }
    await route.fulfill({ json: { version: state.version, data: metadata() } });
  });
  await retryButton(page).click();
  await confirmButton(page).click();
  const dialog = retryDialog(page);
  await expect(dialog.getByRole("alert")).toContainText(
    "Não foi possível confirmar o resultado",
  );
  await expect(dialog.getByText(/Verifique a mesma tentativa/)).toBeVisible();
  await expect(dialog.locator(".template-retry-error")).toBeFocused();
  await page.screenshot({
    path: info.outputPath("03-resposta-perdida.png"),
    fullPage: false,
  });
  await dialog.getByRole("button", { name: "Cancelar", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await retryButton(page).click();
  await dialog
    .getByRole("button", { name: "Verificar tentativa", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  expect(requests[0].key).not.toBe("");
  expect(state.fills).toHaveLength(2);
  expect(state.draft.attachments[0].fields.interaction).toBe(
    "Minha edição manual deve continuar aqui.",
  );
});

test("falha ao consultar após aceite não reenvia comando e mantém aviso no pedido", async ({
  page,
}) => {
  const { state } = await failedTemplate(page);
  let accepted = false;
  let attempts = 0;
  await page.route(draftPath, async (route) => {
    if (accepted && route.request().method() === "GET") {
      await route.abort("failed");
      return;
    }
    await route.fallback();
  });
  await page.route(fillPath, async (route) => {
    attempts++;
    accepted = true;
    await route.fallback();
  });
  await retryButton(page).click();
  await confirmButton(page).click();
  await expect(retryDialog(page)).toHaveCount(0);
  await expect(
    page.getByText(/Não foi possível atualizar o pedido/),
  ).toBeVisible();
  expect(state.fills).toHaveLength(2);
  expect(attempts).toBe(1);
});

test("mudança externa de estado impede outra tentativa enquanto a confirmação está aberta", async ({
  page,
}) => {
  const { state } = await failedTemplate(page);
  await retryButton(page).click();
  state.draft.attachments[0].fill_state = "reconciling";
  state.version++;
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(
    retryDialog(page).getByText(/O status deste template mudou/),
  ).toBeVisible();
  await expect(confirmButton(page)).toBeDisabled();
  await retryDialog(page)
    .getByRole("button", { name: "Cancelar", exact: true })
    .click();
  await expect(retryDialog(page)).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "Templates para este pedido",
      exact: true,
    }),
  ).toBeFocused();
  expect(
    state.requests.filter((request) => request.path.endsWith("/build-fill")),
  ).toHaveLength(0);
});

test("modal acessível mantém ações visíveis em mobile, tablet e zoom 200%", async ({
  page,
}, info) => {
  const { state } = await failedTemplate(page);
  await retryButton(page).click();
  const dialog = retryDialog(page);
  expect(
    (
      await new AxeBuilder({ page })
        .include(".template-retry-dialog")
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 800 });
    const bounds = await dialog.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(800);
    await expect(confirmButton(page)).toBeInViewport();
    await expect(
      dialog.getByRole("button", { name: "Cancelar", exact: true }),
    ).toBeInViewport();
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    if (width === 375)
      await page.screenshot({
        path: info.outputPath("04-confirmacao-mobile.png"),
        fullPage: false,
      });
  }
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await expect(confirmButton(page)).toBeInViewport();
  await expect(
    dialog.getByRole("button", { name: "Cancelar", exact: true }),
  ).toBeInViewport();
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("05-confirmacao-zoom-200.png"),
    fullPage: false,
  });
  expect(state.fills).toHaveLength(1);
});
