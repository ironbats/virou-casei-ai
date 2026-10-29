import { expect, test } from "@playwright/test";
import type { BuildDraft } from "../src/features/guidedTypes";
import type { Draft } from "../src/shared/types";
import { workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Requer API e worker do laboratório isolado.",
);

const targetLabel = "Origem confiável do dono ou tenant";
const targetValue = "usuário da sessão validada no servidor.";
const context =
  "O Cliente autenticado pode criar e consultar somente os próprios pedidos. O recurso é o pedido identificado pelo id. Identificar o dono pelo usuário da sessão validada no servidor. Permitir acesso quando o cliente da sessão corresponde ao dono do pedido.";

for (const retry of [false, true]) {
  test(`geração individual real usa o pedido salvo e preserva os demais campos${retry ? " com falha e recuperação" : " e conteúdo personalizado"}`, async ({
    page,
  }, info) => {
    test.setTimeout(90000);
    const project = await workspace(page);
    const requestText = `${retry ? "Falha ao gerar campo pendente." : "Gerar campo pendente no pedido atual."} Adicionar cadastro e login de clientes na padaria. ${context}`;
    const beforeRequests: {
      body: {
        expected_draft_revision: number;
        attachment_ids: string[];
        target_field_id: string;
      };
      draft: Draft<BuildDraft>;
      key: string;
    }[] = [];
    let builds = 0;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith("/messages"))
        builds++;
    });
    const readDraft = async () => {
      const response = await page.request.get(
        `/api/v1/projects/${project.id}/build-draft`,
      );
      expect(response.ok()).toBe(true);
      return response.json() as Promise<Draft<BuildDraft>>;
    };
    const readAttachment = async () =>
      (await readDraft()).data.attachments.find(
        (item) => item.template_id === "autorizacao",
      )!;
    await page.route(
      `**/api/v1/projects/${project.id}/build-fill`,
      async (route) => {
        const request = route.request();
        const body = request.postDataJSON();
        if (body.target_field_id)
          beforeRequests.push({
            body,
            draft: await readDraft(),
            key: request.headers()["idempotency-key"],
          });
        await route.continue();
      },
    );
    await page
      .getByLabel("O que vamos fazer?", { exact: true })
      .fill(requestText);
    await page
      .getByRole("button", { name: "Preparar construção", exact: true })
      .click();
    await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
    const explore = page.getByRole("button", {
      name: "Explorar catálogo",
      exact: true,
    });
    if (await explore.count()) await explore.click();
    const card = page
      .locator(".template-card")
      .filter({ hasText: "Autenticação, autorização e isolamento" });
    await card.getByRole("checkbox").check();
    await expect
      .poll(
        async () => {
          const attachment = await readAttachment();
          return Object.values(attachment?.fields || {}).filter(
            (value) => typeof value === "string" && value.trim(),
          ).length;
        },
        { timeout: 30000 },
      )
      .toBe(9);
    await expect(
      card.getByRole("button", { name: `Gerar · ${targetLabel}`, exact: true }),
    ).toBeEnabled();
    const initial = await readAttachment();
    expect(initial.fields.field_4 ?? "").toBe("");
    expect(
      Object.values(initial.field_values || {})
        .filter((item) => item.value)
        .every((item) => item.source_type === "policy_suggestion"),
    ).toBe(true);
    await card
      .getByRole("button", { name: "Editar conteúdo", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    const manualValue =
      "Proposta revisada manualmente: consultar apenas os próprios pedidos autenticados.";
    await dialog
      .getByLabel("Ação permitida sobre o recurso *", { exact: true })
      .fill(manualValue);
    const customContent =
      "Conteúdo personalizado deste pedido. Preservar as decisões manuais e conferir os campos antes de construir.";
    if (!retry)
      await dialog
        .getByLabel("Conteúdo que será enviado", { exact: true })
        .fill(customContent);
    await dialog
      .getByRole("button", { name: `Gerar · ${targetLabel}`, exact: true })
      .click();
    await expect.poll(() => beforeRequests.length).toBe(1);
    expect(beforeRequests[0].body).toEqual({
      expected_draft_revision: beforeRequests[0].draft.version,
      attachment_ids: ["autorizacao"],
      target_field_id: "field_4",
    });
    expect(beforeRequests[0].draft.data.message_text).toBe(requestText);
    expect(beforeRequests[0].draft.data.attachments[0].fields.field_2).toBe(
      manualValue,
    );
    if (retry) {
      await expect
        .poll(
          async () =>
            (await readAttachment()).field_generations?.field_4?.state,
          { timeout: 30000 },
        )
        .toBe("failed");
      await expect(
        dialog.getByRole("button", {
          name: `Gerar · ${targetLabel}`,
          exact: true,
        }),
      ).toBeEnabled();
      await expect(
        dialog
          .locator(".template-editor-field")
          .filter({
            has: page.getByLabel(`${targetLabel} *`, { exact: true }),
          })
          .getByRole("alert"),
      ).toBeVisible();
      await page.screenshot({ path: info.outputPath("field-error-real.png") });
      await dialog
        .getByRole("button", { name: `Gerar · ${targetLabel}`, exact: true })
        .click();
    }
    await expect(
      dialog.getByLabel(`${targetLabel} *`, { exact: true }),
    ).toHaveValue(targetValue, { timeout: 30000 });
    await expect(
      dialog.getByLabel("Ação permitida sobre o recurso *", { exact: true }),
    ).toHaveValue(manualValue);
    const result = await readAttachment();
    expect(result.fields).toEqual({
      ...initial.fields,
      field_2: manualValue,
      field_4: targetValue,
    });
    expect(result.field_values?.field_4.source_type).toBe("policy_suggestion");
    expect(result.field_generations?.field_4.state).toBe("succeeded");
    expect(result.validation_errors).toEqual([]);
    if (!retry) {
      await expect(
        dialog.getByLabel("Conteúdo que será enviado", { exact: true }),
      ).toHaveValue(customContent);
      expect(result.edited_content).toBe(customContent);
    }
    expect(beforeRequests).toHaveLength(retry ? 2 : 1);
    if (retry) expect(beforeRequests[1].key).not.toBe(beforeRequests[0].key);
    await page.screenshot({
      path: info.outputPath("field-generated-real.png"),
    });
    await dialog
      .getByRole("button", { name: "Salvar edição", exact: true })
      .click();
    await expect(dialog).toHaveCount(0);
    await page
      .getByRole("button", { name: "Revisar pedido", exact: true })
      .click();
    await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
    const saved = await readDraft();
    const snapshot = saved.data.review_snapshot?.attachments[0];
    expect(snapshot?.fields).toEqual(result.fields);
    expect(snapshot?.content).toBe(result.edited_content);
    expect(builds).toBe(0);
  });
}
