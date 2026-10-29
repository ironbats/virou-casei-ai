import { expect, test } from "@playwright/test";
import { workspace } from "./workspaceHelper";

test.skip(process.env.VIRA_E2E_WORKSPACE !== "true", "Requer API e worker do laboratório isolado.");

const context = "O Cliente autenticado pode criar e consultar somente os próprios pedidos. O recurso é o pedido identificado pelo id. Identificar o dono pelo usuário da sessão validada no servidor. Permitir acesso quando o cliente da sessão corresponde ao dono do pedido.";

const expectedFields: Record<string, string> = {
  field_1: "Cliente autenticado",
  field_2: "O Cliente autenticado pode criar e consultar somente os próprios pedidos.",
  field_3: "pedido identificado pelo id.",
  field_4: "usuário da sessão validada no servidor.",
  field_5: "Permitir acesso quando o cliente da sessão corresponde ao dono do pedido.",
  field_6: "Proposta: negar acesso quando a sessão não estiver autorizada para o pedido.",
  field_7: "Critério proposto: testar com dois clientes e impedir acesso aos pedidos do outro cliente.",
  field_8: "Proposta: solicitar novo login quando a sessão expirar, sem liberar o pedido.",
  field_9: "Proposta: informar que o pedido não está disponível sem revelar dados de outro cliente.",
  field_10: "Proposta: registrar a decisão de acesso ao pedido sem senha, token ou conteúdo privado.",
};

for (const retry of [false, true]) {
  test(`autofill real local preenche dez campos distintos${retry ? " após confirmação de retry" : " e conserva o snapshot"}`, async ({ page }, info) => {
    test.setTimeout(90000);
    const project = await workspace(page);
    let nativeDialogs = 0;
    let buildRequests = 0;
    page.on("dialog", async (dialog) => {
      nativeDialogs++;
      await dialog.dismiss();
    });
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/messages")) buildRequests++;
    });
    const message = `${retry ? "Corrigir" : "Adicionar"} cadastro e login de clientes na padaria. ${context}`;
    await page.getByLabel("O que vamos fazer?").fill(message);
    await page.getByRole("button", { name: "Preparar construção", exact: true }).click();
    await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
    const explore = page.getByRole("button", { name: "Explorar catálogo", exact: true });
    if (await explore.count()) await explore.click();
    const card = page.locator(".template-card").filter({ hasText: "Autenticação, autorização e isolamento" });
    await card.getByRole("checkbox").check();
    if (retry) {
      await expect(card.locator(".template-fill-state")).toHaveText("Falha no preenchimento", { timeout: 30000 });
      await card.getByRole("button", { name: "Tentar preencher novamente", exact: true }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await page.screenshot({ path: info.outputPath("retry-confirmacao-integrada.png") });
      await dialog.getByRole("button", { name: "Confirmar nova tentativa", exact: true }).click();
      await expect(dialog).not.toBeVisible();
    }
    await expect(card.locator(".template-fill-state")).toHaveText("Preenchido pela IA", { timeout: 30000 });
    const attachment = await page.evaluate(async (pid) => {
      const value = await (await fetch(`/api/v1/projects/${pid}/build-draft`)).json();
      return value.data.attachments.find((item: { template_id: string }) => item.template_id === "autorizacao");
    }, project.id);
    expect(attachment.fields).toEqual(expectedFields);
    expect(new Set(Object.values(attachment.fields)).size).toBe(10);
    expect(attachment.validation_errors).toEqual([]);
    expect(attachment.edited_content).not.toMatch(/\{\{[^{}]+\}\}/);
    expect(buildRequests).toBe(0);
    expect(nativeDialogs).toBe(0);
    await card.getByRole("button", { name: "Editar conteúdo", exact: true }).click();
    await expect(page.getByRole("dialog").getByLabel("Identidade e papel do ator *", { exact: true })).toHaveValue(expectedFields.field_1);
    await expect(page.getByRole("dialog").getByLabel("Resultado esperado para sessão expirada *", { exact: true })).toHaveValue(expectedFields.field_8);
    await page.screenshot({ path: info.outputPath("campos-preenchidos-integracao.png") });
    await page.getByRole("dialog").getByRole("button", { name: "Cancelar edição", exact: true }).click();
    await page.getByRole("button", { name: "Revisar pedido", exact: true }).click();
    await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
    await page.locator(".attachment-card").getByRole("button", { name: "Abrir", exact: true }).click();
    await expect(page.getByRole("dialog").locator("pre")).toHaveText(attachment.edited_content);
    expect(buildRequests).toBe(0);
  });
}
