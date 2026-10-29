import { expect, test } from "@playwright/test";
import { productionPolicy } from "./productionPolicy";
import { editorAction, workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Requer laboratório integrado de construção.",
);
test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

test("altura útil, scroll local e composer em desktop, tablet e mobile", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await workspace(page);
  for (const [width, height] of [
    [1280, 720],
    [1366, 768],
    [1920, 1080],
    [1024, 768],
    [768, 1024],
    [375, 812],
    [320, 568],
  ]) {
    await page.setViewportSize({ width, height });
    await expect
      .poll(() =>
        page
          .locator(".ide-layout")
          .evaluate((node) => node.classList.contains("is-narrow")),
      )
      .toBe(width < 1180);
    const root = await page.evaluate(() => ({
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
      viewportWidth: innerWidth,
      viewportHeight: innerHeight,
    }));
    expect(root.width).toBeLessThanOrEqual(root.viewportWidth + 1);
    expect(root.height).toBeLessThanOrEqual(root.viewportHeight + 1);
    const editor = await page.locator(".code-surface").boundingBox();
    if (width === 1280) expect(editor!.height).toBeGreaterThanOrEqual(560);
    if (width === 1366) expect(editor!.height).toBeGreaterThanOrEqual(600);
    if (width === 1920) expect(editor!.height).toBeGreaterThan(900);
    if (width < 1180)
      await page.getByRole("tab", { name: "Chat", exact: true }).click();
    await expect(
      page.getByLabel("O que vamos fazer?", { exact: true }),
    ).toBeVisible();
    const composer = await page
      .getByRole("button", { name: "Preparar construção", exact: true })
      .boundingBox();
    expect(composer!.y + composer!.height).toBeLessThanOrEqual(height);
    const timeline = await page.locator(".build-conversation").boundingBox();
    if (width === 1280) expect(timeline!.height).toBeGreaterThanOrEqual(360);
    if (width === 1366) expect(timeline!.height).toBeGreaterThanOrEqual(400);
    if (width < 1180) {
      await expect(
        page.getByRole("separator", {
          name: "Ajustar largura de Código e Chat",
        }),
      ).toHaveCount(0);
      await page
        .getByRole("tab", { name: "Código", exact: true })
        .first()
        .click();
    }
  }
});

test("divisor acessível e troca de painel preservam código e pedido", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await workspace(page);
  const initial = await page.locator(".code-editor").inputValue();
  await page
    .locator(".code-editor")
    .fill(initial + "\n<section>Rascunho preservado</section>");
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .fill("Preservar o pedido entre painéis");
  const divider = page.getByRole("separator", {
    name: "Ajustar largura de Código e Chat",
  });
  await divider.focus();
  await divider.press("Home");
  await expect(divider).toHaveAttribute("aria-valuenow", "640");
  expect((await page.locator(".ide-panel").boundingBox())!.width).toBeCloseTo(
    640,
    0,
  );
  await divider.press("Shift+ArrowRight");
  await expect(divider).toHaveAttribute("aria-valuenow", "704");
  await divider.press("End");
  const panels = await page.locator(".conversation-panel").boundingBox();
  expect(panels!.width).toBeGreaterThanOrEqual(359);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("Preservar o pedido entre painéis");
  await page.getByRole("tab", { name: "Código", exact: true }).first().click();
  await expect(page.locator(".code-editor")).toHaveValue(
    initial + "\n<section>Rascunho preservado</section>",
  );
  await page.setViewportSize({ width: 1366, height: 768 });
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("Preservar o pedido entre painéis");
  await expect(
    page.getByRole("separator", { name: "Ajustar largura de Código e Chat" }),
  ).toHaveAttribute(
    "aria-valuenow",
    (await divider.getAttribute("aria-valuemax")) as string,
  );
});

test("editor mantém seleção e undo ao alternar prévia e largura", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await workspace(page);
  await editorAction(page, "Editor com destaque");
  const content = page.locator(".cm-content");
  await content.click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type("\nPreservar histórico de edição");
  await expect(content).toContainText("Preservar histórico de edição");
  await page.getByRole("tab", { name: "Prévia", exact: true }).click();
  await page.getByRole("tab", { name: "Código", exact: true }).click();
  await page.setViewportSize({ width: 375, height: 812 });
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  await page.getByRole("tab", { name: "Código", exact: true }).first().click();
  await page.setViewportSize({ width: 1366, height: 768 });
  await content.click();
  await page.keyboard.press("Control+z");
  await expect(content).not.toContainText("Preservar histórico de edição");
});

test("conversa preserva a mensagem lida com novas respostas e histórico anterior", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  const project = await workspace(page);
  const makeOperation = (index: number) => ({
    id: `leitura-${index}`,
    project_id: project.id,
    type: "generation",
    state: "succeeded",
    request_text: `Pedido de leitura ${index}`,
    progress_message: "Construção concluída.",
    created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
    result_ref: {
      message: `Resposta do pedido ${index}. ${"Conteúdo preservado para verificar a âncora de leitura. ".repeat(12)}`,
      applied_revision_id: project.current_revision_id,
    },
  });
  let operations = Array.from({ length: 20 }, (_, index) =>
    makeOperation(index + 20),
  ).reverse();
  await page.route("**/api/v1/operations?**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("project_id") !== project.id)
      return route.fallback();
    await route.fulfill({
      json: {
        items:
          url.searchParams.get("state") === "active"
            ? []
            : url.searchParams.has("cursor")
              ? Array.from({ length: 8 }, (_, index) =>
                  makeOperation(index),
                ).reverse()
              : operations,
        next_cursor: url.searchParams.has("cursor") ? null : "older-history",
      },
    });
  });
  await page.reload();
  const timeline = page.getByRole("region", {
    name: "Conversa de construção",
    exact: true,
  });
  await expect(page.locator(".build-turn")).toHaveCount(20);
  await timeline.evaluate((element) => {
    element.scrollTop = element.scrollHeight / 2;
  });
  await expect(
    page.getByRole("button", { name: "Ir para a última mensagem" }),
  ).toBeVisible();
  const anchor = await timeline.evaluate((element) => {
    const edge = element.getBoundingClientRect().top;
    const node = Array.from(
      element.querySelectorAll<HTMLElement>(".build-turn"),
    ).find((item) => item.getBoundingClientRect().bottom > edge + 1)!;
    return {
      id: node.dataset.operationId!,
      offset: node.getBoundingClientRect().top - edge,
    };
  });
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .fill("Continuo lendo sem deslocamento automático");
  operations = [makeOperation(40), ...operations];
  await expect(page.locator(".build-turn")).toHaveCount(21, { timeout: 10000 });
  const offset = () =>
    page
      .locator(`[data-operation-id="${anchor.id}"]`)
      .evaluate(
        (node) =>
          node.getBoundingClientRect().top -
          node.closest(".build-conversation")!.getBoundingClientRect().top,
      );
  expect(Math.abs((await offset()) - anchor.offset)).toBeLessThanOrEqual(2);
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Histórico e ações da conversa", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Conversas anteriores", exact: true })
    .click();
  await expect(page.locator(".build-turn")).toHaveCount(29);
  expect(Math.abs((await offset()) - anchor.offset)).toBeLessThanOrEqual(2);
  await page.getByRole("button", { name: "Ir para a última mensagem" }).click();
  await expect
    .poll(() =>
      timeline.evaluate(
        (element) =>
          element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    )
    .toBeLessThanOrEqual(2);
});

test("zoom de 200% conserva altura, reflow e escala do divisor", async ({
  page,
}) => {
  const { preparationWorkspace } = await import("./templatePreparationFixture");
  await page.setViewportSize({ width: 1440, height: 900 });
  await preparationWorkspace(page);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await expect(page.locator(".ide-layout")).toHaveClass(/is-narrow/);
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  const viewport = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.width + 1);
  expect(viewport.scrollHeight).toBeLessThanOrEqual(viewport.height + 1);
  const button = await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .boundingBox();
  expect(button!.y + button!.height).toBeLessThanOrEqual(viewport.height);
  await page.setViewportSize({ width: 2880, height: 1200 });
  await expect(page.locator(".ide-layout")).not.toHaveClass(/is-narrow/);
  const divider = page.getByRole("separator", {
    name: "Ajustar largura de Código e Chat",
  });
  await divider.focus();
  await divider.press("Home");
  expect((await page.locator(".ide-panel").boundingBox())!.width).toBeCloseTo(
    1280,
    0,
  );
  const bounds = await divider.boundingBox();
  const x = bounds!.x + bounds!.width / 2;
  const y = bounds!.y + bounds!.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 64, y);
  await page.mouse.up();
  await expect(divider).toHaveAttribute("aria-valuenow", "672");
  expect((await page.locator(".ide-panel").boundingBox())!.width).toBeCloseTo(
    1344,
    0,
  );
  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
  });
});

test("exportação concluída oferece o ZIP solicitado sem abrir o histórico nem alterar o rascunho", async ({
  page,
}) => {
  const { preparationWorkspace, preparationProject, preparationRevision } =
    await import("./templatePreparationFixture");
  await page.setViewportSize({ width: 1366, height: 768 });
  const { state } = await preparationWorkspace(page);
  const previous = {
    id: "export-previous",
    project_id: preparationProject,
    type: "export",
    state: "succeeded",
    created_at: "2026-10-28T12:00:00Z",
    result_ref: { revision_id: "previous-revision" },
  };
  const current = {
    id: "export-requested",
    project_id: preparationProject,
    type: "export",
    state: "queued",
    created_at: "2026-10-29T12:00:00Z",
    result_ref: { revision_id: preparationRevision },
  };
  state.generation.push(previous);
  await page.reload();
  await editorAction(page, "Usar texto simples");
  const source = await page.locator(".code-editor").inputValue();
  const draft = source + "\n<section>Rascunho não publicado</section>";
  await page.locator(".code-editor").fill(draft);
  const requests: unknown[] = [];
  let downloads = 0;
  page.on("download", () => {
    downloads++;
  });
  await page.route(
    `**/api/v1/projects/${preparationProject}/exports`,
    async (route) => {
      requests.push(route.request().postDataJSON());
      state.generation.push(current);
      await route.fulfill({ status: 202, json: current });
    },
  );
  await page
    .getByRole("button", { name: "Baixar projeto", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Baixar revisão salva", exact: true })
    .click();
  const status = page.locator(".workspace-export-status");
  await expect(status).toHaveAttribute("data-operation-id", current.id);
  await expect(status).toContainText("Preparando download da revisão salva.");
  await expect(status.getByRole("link")).toHaveCount(0);
  current.state = "succeeded";
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(status).toContainText("Download pronto.");
  await expect(
    status.getByRole("link", { name: /Baixar ZIP/ }),
  ).toHaveAttribute("href", `/api/v1/exports/${current.id}/download`);
  await expect(status).not.toContainText("Preparando download");
  await expect(page.getByRole("link", { name: /Baixar ZIP/ })).toHaveCount(1);
  await expect(page.locator(".code-editor")).toHaveValue(draft);
  expect(state.contextRevision).toBe(preparationRevision);
  expect(requests).toEqual([{ revision_id: preparationRevision }]);
  expect(downloads).toBe(0);
  expect(
    state.requests.filter((request) => request.path.endsWith("/revisions")),
  ).toEqual([]);
  await page
    .getByRole("button", { name: "Mais ações do projeto", exact: true })
    .click();
  await expect(
    page.locator(
      `.workspace-menu-content a[href="/api/v1/exports/${previous.id}/download"]`,
    ),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 320, height: 568 });
  await expect(page.locator(".ide-layout")).toHaveClass(/is-narrow/);
  const downloadLink = status.getByRole("link", { name: /Baixar ZIP/ });
  await expect(downloadLink).toBeVisible();
  const bounds = await downloadLink.boundingBox();
  expect(bounds!.height).toBeGreaterThanOrEqual(44);
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(320);
  expect(
    await status.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
});

test("exportação em falha ou apuração não oferece ZIP anterior nem repete o envio", async ({
  page,
}) => {
  const { preparationWorkspace, preparationProject, preparationRevision } =
    await import("./templatePreparationFixture");
  await page.setViewportSize({ width: 1366, height: 768 });
  const { state } = await preparationWorkspace(page);
  state.generation.push({
    id: "export-old-success",
    project_id: preparationProject,
    type: "export",
    state: "succeeded",
    created_at: "2026-10-28T12:00:00Z",
    result_ref: { revision_id: "old-revision" },
  });
  const current = {
    id: "export-failed-request",
    project_id: preparationProject,
    type: "export",
    state: "reconciling",
    progress_message: "Aguardando confirmação.",
    created_at: "2026-10-29T12:00:00Z",
    result_ref: { revision_id: preparationRevision },
  };
  let requests = 0;
  let rejectRequest = false;
  await page.route(
    `**/api/v1/projects/${preparationProject}/exports`,
    async (route) => {
      requests++;
      if (rejectRequest)
        return route.fulfill({
          status: 400,
          json: {
            error: {
              code: "EXPORT_UNAVAILABLE",
              message: "Exportação indisponível para esta revisão.",
            },
          },
        });
      state.generation.push(current);
      await route.fulfill({ status: 202, json: current });
    },
  );
  await page
    .getByRole("button", { name: "Baixar projeto", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Baixar revisão salva", exact: true })
    .click();
  const status = page.locator(".workspace-export-status");
  await expect(status).toContainText("O download está em apuração.");
  await expect(status.getByRole("link")).toHaveCount(0);
  current.state = "failed";
  current.progress_message = "Não foi possível gerar o ZIP desta tentativa.";
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(status).toHaveAttribute("role", "alert");
  await expect(status).toContainText(current.progress_message);
  await expect(page.getByRole("link", { name: /Baixar ZIP/ })).toHaveCount(0);
  expect(requests).toBe(1);
  rejectRequest = true;
  await page
    .getByRole("button", { name: "Baixar projeto", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Baixar revisão salva", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Baixar seu projeto" }),
  ).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText(
    "Exportação indisponível para esta revisão.",
  );
  await expect(status).toHaveCount(0);
  expect(requests).toBe(2);
});
