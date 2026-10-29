import { expect, test, type Page } from "@playwright/test";
import type { Project } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";
import { reviewRequest, workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Use backend/script/test-workspace.py para iniciar o laboratório de construção.",
);
test.use({ viewport: { width: 1440, height: 950 } });
test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

async function savedProject(page: Page, pid: string) {
  return page.evaluate(async (id) => {
    const project: Project = await (
      await fetch(`/api/v1/projects/${id}`)
    ).json();
    const revision = await (
      await fetch(
        `/api/v1/projects/${id}/revisions/${project.current_revision_id}`,
      )
    ).json();
    const entries = await Promise.all(
      revision.files.map(async (file: { id: string }) => {
        const response = await fetch(
          `/api/v1/projects/${id}/revisions/${project.current_revision_id}/files/${file.id}`,
        );
        if (!response.ok) throw new Error(await response.text());
        return response.json() as Promise<{ path: string; content: string }>;
      }),
    );
    return {
      project,
      files: Object.fromEntries(
        entries.map((file) => [file.path, file.content]),
      ) as Record<string, string>,
    };
  }, pid);
}

async function send(page: Page, request: string) {
  await reviewRequest(page, request);
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  const turn = page.locator(".build-turn").last();
  await expect(turn.locator(".build-request")).toContainText(request);
  return turn;
}

test("conclui na décima chamada e preserva a revisão até a aplicação revisada", async ({
  page,
}, info) => {
  const project = await workspace(page);
  const initial = await savedProject(page, project.id);
  const request =
    "CONSTRUIR_DEZ_ETAPAS Implementar a melhoria completa da página inicial com dez etapas verificáveis.";
  let submissions = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/messages"))
      submissions += 1;
  });
  const turn = await send(page, request);
  await expect(turn).toHaveAttribute("data-state", "running");
  await expect(turn.getByLabel("Arquivos em construção")).toContainText(
    "public/construcao-etapa-05.js",
    { timeout: 15000 },
  );
  expect(await savedProject(page, project.id)).toEqual(initial);
  await expect(turn).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  await expect(turn.locator(".build-request")).toContainText(request);
  expect(submissions).toBe(1);
  expect(await savedProject(page, project.id)).toEqual(initial);
  await turn
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "public/construcao-etapa-10.js",
  );
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const applied = await savedProject(page, project.id);
  expect(applied.project.current_revision_id).not.toBe(
    initial.project.current_revision_id,
  );
  expect(applied.files["public/index.html"]).toContain('data-etapas="10"');
  for (let batch = 1; batch <= 10; batch += 1) {
    const path = `public/construcao-etapa-${String(batch).padStart(2, "0")}.js`;
    expect(applied.files[path]).toContain(`export const etapa = ${batch};`);
    expect(applied.files[path]).toContain(request);
  }
  expect(applied.files["public/construcao-etapa-11.js"]).toBeUndefined();
  for (const [path, content] of Object.entries(initial.files))
    if (path !== "public/index.html") expect(applied.files[path]).toBe(content);
  await page.screenshot({
    path: info.outputPath("generation-tenth-call-complete.png"),
  });
});

test("ao atingir dez chamadas incompletas mantém arquivos e permite reutilizar o pedido", async ({
  page,
}, info) => {
  const project = await workspace(page);
  const initial = await savedProject(page, project.id);
  const request =
    "CONSTRUIR_DEZ_ETAPAS MANTER_INCOMPLETO Preservar o projeto quando o agente ainda não concluiu.";
  const turn = await send(page, request);
  await expect(turn.getByLabel("Arquivos em construção")).toContainText(
    "public/construcao-etapa-05.js",
    { timeout: 15000 },
  );
  expect(await savedProject(page, project.id)).toEqual(initial);
  await expect(turn).toHaveAttribute("data-state", "failed", {
    timeout: 15000,
  });
  await turn.getByText("Detalhes da falha", { exact: true }).click();
  await expect(turn).toContainText("GENERATION_LIMIT");
  await expect(turn).toContainText("Nenhuma alteração parcial foi aplicada");
  await expect(
    turn.getByRole("button", { name: "Revisar alterações" }),
  ).toHaveCount(0);
  expect(await savedProject(page, project.id)).toEqual(initial);
  await page.screenshot({
    path: info.outputPath("generation-tenth-call-preserved.png"),
  });
  await turn.getByRole("button", { name: "Reutilizar pedido" }).click();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue(request);
  expect(await savedProject(page, project.id)).toEqual(initial);
});
