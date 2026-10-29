import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Operation } from "../src/shared/types";
import {
  preparationProject,
  preparationWorkspace,
} from "./templatePreparationFixture";
import { showBuildHistory } from "./workspaceHelper";

test.use({ viewport: { width: 1440, height: 950 } });

const operationId = "790da092-2d12-4fef-92bf-b922ff4bea11";
const request =
  "Melhorar a home da padaria com carrossel, preços e carrinho. Exigir cadastro e login para concluir a compra.";
const message =
  "O contexto ultrapassou o limite seguro desta construção. Nenhuma alteração parcial foi aplicada. Reutilize o pedido e solicite uma etapa menor; se persistir, contate o suporte com a referência da operação.";

async function failedBuild(page: Page, changes: Partial<Operation> = {}) {
  const fixture = await preparationWorkspace(page);
  const operation: Operation = {
    id: operationId,
    type: "generation",
    project_id: preparationProject,
    state: "failed",
    progress_stage: "failed",
    progress_message: message,
    billing_status: "settled",
    pending_reconciliation: false,
    cancel_requested: false,
    error_code: "CONTEXT_LIMIT_EXCEEDED",
    request_text: request,
    created_at: "2026-10-28T21:26:01Z",
    result_ref: {},
    activity: [
      {
        stage: "provider_sent",
        at: "2026-10-28T21:25:35Z",
        message: "Construindo as alterações solicitadas.",
      },
      {
        stage: "failed",
        at: "2026-10-28T21:26:01Z",
        message,
      },
    ],
    attachment_snapshots: [
      {
        id: "autorizacao",
        title: "Autenticação, autorização e isolamento",
        version: 1,
        content:
          "Exigir uma sessão válida antes de finalizar a compra. Preservar o carrinho enquanto o cliente entra.",
        content_hash: "synthetic-reviewed-context",
      },
    ],
    ...changes,
  };
  fixture.state.generation.push(operation);
  await page.reload();
  await showBuildHistory(page);
  const turn = page.locator(".build-turn").last();
  await expect(turn).toHaveAttribute("data-state", operation.state);
  return { ...fixture, turn };
}

test("falha informa motivo e referência sem perder pedido, anexo ou revisão", async ({
  page,
}, info) => {
  const { turn, state } = await failedBuild(page);
  await expect(turn.locator(".build-response > p").first()).toHaveText(message);
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  const original = await page.locator(".code-editor").inputValue();
  const details = turn.locator("details").filter({
    has: page.getByText("Detalhes da falha", { exact: true }),
  });
  await details.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText("CONTEXT_LIMIT_EXCEEDED");
  await expect(details).toContainText(operationId);
  await turn.getByRole("button", { name: /Ver conteúdo enviado/ }).click();
  const snapshot = page.getByRole("dialog");
  await expect(snapshot).toContainText("Preservar o carrinho");
  await page.keyboard.press("Escape");
  await expect(snapshot).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("generation-failure-details.png"),
  });
  page.once("dialog", (dialog) => dialog.accept());
  await turn.getByRole("button", { name: "Reutilizar pedido" }).click();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue(request);
  await expect(page.locator(".code-editor")).toHaveValue(original);
  await expect(
    page.getByRole("button", { name: "Preparar construção", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Enviar para construir", exact: true }),
  ).toHaveCount(0);
  expect(
    state.requests.filter((item) => item.path.endsWith("/messages")),
  ).toEqual([]);
  expect(state.generation).toHaveLength(1);
});

test("detalhes rejeitam identificadores fora do formato técnico permitido", async ({
  page,
}) => {
  const { turn } = await failedBuild(page, {
    id: "invalid-id?token=synthetic-private-value",
    error_code: "<img src=x onerror=alert('private')>",
  });
  await expect(
    turn.getByText("Detalhes da falha", { exact: true }),
  ).toHaveCount(0);
  await expect(turn).not.toContainText("synthetic-private-value");
  await expect(turn).not.toContainText("onerror");
  await expect(turn.locator(".build-response > p").first()).toHaveText(message);
  await expect(
    turn.getByRole("button", { name: "Reutilizar pedido" }),
  ).toBeEnabled();
});

test("operação em apuração mantém a espera sem detalhes de falha nem novo envio", async ({
  page,
}) => {
  const { turn } = await failedBuild(page, {
    state: "reconciling",
    billing_status: "reconciliation_pending",
    pending_reconciliation: true,
    progress_stage: "reconciling",
    progress_message:
      "O resultado está em apuração. Aguarde antes de tentar novamente.",
    error_code: "PROVIDER_RESULT_UNKNOWN",
    activity: [],
  });
  await expect(turn).toContainText("Aguarde antes de tentar novamente.");
  await expect(
    turn.getByText("Detalhes da falha", { exact: true }),
  ).toHaveCount(0);
  await expect(
    turn.getByRole("button", { name: "Reutilizar pedido" }),
  ).toHaveCount(0);
});

test("detalhes de falha são acessíveis e cabem de 320 a 1440 pixels", async ({
  page,
}, info) => {
  const { turn } = await failedBuild(page);
  await turn.getByText("Detalhes da falha", { exact: true }).click();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 950 });
    const chatTab = page.getByRole("tab", {
      name: "Construção",
      exact: true,
    });
    if (await chatTab.isVisible()) await chatTab.click();
    await expect(turn.getByText(operationId, { exact: true })).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    const details = turn
      .locator("details[open]")
      .filter({ hasText: "Detalhes da falha" });
    expect(
      await details.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
  }
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await expect(turn.getByText(operationId, { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
  });
  const audit = await new AxeBuilder({ page })
    .include(".build-conversation")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(audit.violations).toEqual([]);
  await page.screenshot({
    path: info.outputPath("generation-failure-responsive.png"),
  });
});
