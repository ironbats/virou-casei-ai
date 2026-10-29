import { expect, test } from "@playwright/test";
import { workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Requer API e worker do laboratório local isolado.",
);

test("seleção integra catálogo, autofill, worker, recarga, revisão e snapshot da construção", async ({
  page,
}, info) => {
  test.setTimeout(90000);
  const project = await workspace(page);
  const initialRevision = project.current_revision_id;
  const initialPreparationIds = await page.evaluate(async (pid) => {
    const operations = (
      await (
        await fetch(`/api/v1/operations?project_id=${pid}&latest=true`)
      ).json()
    ).items;
    return operations
      .filter((item: { type: string }) =>
        ["template_autofill", "template_preparation"].includes(item.type),
      )
      .map((item: { id: string }) => item.id)
      .sort();
  }, project.id);
  let manualPreparationRequests = 0;
  let buildRequests = 0;
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    if (request.url().endsWith("/template-preparations"))
      manualPreparationRequests++;
    if (request.url().endsWith("/messages")) buildRequests++;
  });
  await page
    .getByLabel("O que vamos fazer?")
    .fill(
      "Adicionar botão de login na home que abre um modal. Reutilizar a autenticação existente. O visitante pode abrir o modal; a sessão expirada solicita novo login. Recurso inexistente mostra indisponibilidade. A conta acessa somente seus próprios dados. Conferir com duas contas sintéticas. Registrar o evento sem senha ou token.",
    );
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
  const beforeSelection = await page.evaluate(
    async (pid) =>
      (
        await (
          await fetch(`/api/v1/operations?project_id=${pid}&latest=true`)
        ).json()
      ).items,
    project.id,
  );
  expect(
    beforeSelection
      .filter((item: { type: string }) =>
        ["template_autofill", "template_preparation"].includes(item.type),
      )
      .map((item: { id: string }) => item.id)
      .sort(),
  ).toEqual(initialPreparationIds);
  const explore = page.getByRole("button", {
    name: "Explorar catálogo",
    exact: true,
  });
  if (await explore.count()) await explore.click();
  const card = page
    .locator(".template-card")
    .filter({ hasText: "Autenticação, autorização e isolamento" });
  await card.getByRole("checkbox").check();
  await expect(card.locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
    { timeout: 30000 },
  );
  const prepared = await page.evaluate(
    async (pid) =>
      await (await fetch(`/api/v1/projects/${pid}/build-draft`)).json(),
    project.id,
  );
  const attachment = prepared.data.attachments.find(
    (item: { template_id: string }) => item.template_id === "autorizacao",
  );
  expect(Object.keys(attachment.fields).length).toBeGreaterThan(0);
  expect(attachment.edited_content).not.toMatch(/\{\{[^{}]+\}\}/);
  expect(manualPreparationRequests).toBe(0);
  expect(buildRequests).toBe(0);
  await card
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByLabel("Resultado esperado para sessão expirada *", { exact: true }),
  ).toHaveCount(1);
  await expect(
    page
      .getByRole("dialog")
      .getByLabel("Resultado esperado para recurso inexistente *", {
        exact: true,
      }),
  ).toHaveCount(1);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Cancelar edição", exact: true })
    .click();
  await page.screenshot({
    path: info.outputPath("template-autofill-local-ready.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Enviar para construir", exact: true }),
  ).toBeEnabled();
  await page.reload();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page
    .locator(".attachment-card")
    .getByRole("button", { name: "Abrir", exact: true })
    .click();
  const content = await page.getByRole("dialog").locator("pre").innerText();
  expect(content).toBe(attachment.edited_content);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Fechar", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  await expect(
    page.getByText("Preparação concluída", { exact: true }),
  ).toBeVisible();
  const submittedDraft = await page.request.get(
    `/api/v1/projects/${project.id}/build-draft`,
  );
  expect(submittedDraft.ok()).toBe(true);
  expect((await submittedDraft.json()).data.stage).toBe("conversation");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
    { timeout: 30000 },
  );
  expect(buildRequests).toBe(1);
  const operations = await page.evaluate(
    async (pid) =>
      (
        await (
          await fetch(
            `/api/v1/operations?project_id=${pid}&kind=generation&latest=true`,
          )
        ).json()
      ).items,
    project.id,
  );
  const operation = operations.find(
    (item: { type: string }) => item.type === "generation",
  );
  expect(operation.attachment_snapshots[0].content).toBe(content);
  expect(operation.result_ref.applied_revision_id).toBeFalsy();
  expect(
    await page.evaluate(
      async (pid) =>
        (await (await fetch(`/api/v1/projects/${pid}`)).json())
          .current_revision_id,
      project.id,
    ),
  ).toBe(initialRevision);
  await page.screenshot({
    path: info.outputPath("template-autofill-local-snapshot.png"),
    fullPage: true,
  });
});
