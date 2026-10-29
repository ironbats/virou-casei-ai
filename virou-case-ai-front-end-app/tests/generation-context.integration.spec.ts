import { expect, test, type Page } from "@playwright/test";
import type { Project } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";
import { editorAction, reviewRequest, workspace } from "./workspaceHelper";

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

async function catalogProject(page: Page, products: number) {
  const project = await workspace(page);
  const initial = await savedProject(page, project.id);
  const catalog = JSON.stringify({
    products: Array.from({ length: products }, (_, index) => ({
      id: index + 1,
      name: `Produto artesanal da padaria ${index + 1}`,
      price_cents: 500 + index,
      available: true,
    })),
  });
  const files: Record<string, string> = {
    ...initial.files,
    "public/catalogo.json": catalog,
  };
  delete files["public/carrossel.js"];
  const result = await page.evaluate(
    async ({ pid, version, base, files }) => {
      const me = await (await fetch("/api/v1/me")).json();
      const headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
      };
      const saved = await fetch(`/api/v1/projects/${pid}/revisions`, {
        method: "POST",
        headers: {
          ...headers,
          "If-Match": String(version),
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({ base_revision_id: base, files }),
      });
      if (!saved.ok) throw new Error(await saved.text());
      const result = await saved.json();
      const draft = await (await fetch(`/api/v1/projects/${pid}/draft`)).json();
      const reset = await fetch(`/api/v1/projects/${pid}/draft`, {
        method: "PUT",
        headers: {
          ...headers,
          "If-Match": String(draft.version),
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          base_revision_id: result.project.current_revision_id,
          files: null,
          prompt: "",
        }),
      });
      if (!reset.ok) throw new Error(await reset.text());
      return result;
    },
    {
      pid: project.id,
      version: initial.project.version,
      base: initial.project.current_revision_id,
      files,
    },
  );
  await page.reload();
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await editorAction(page, "Usar texto simples");
  await expect(page.locator(".code-editor")).toHaveValue(/<!doctype html>/i);
  return { project: result.project as Project, files };
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

async function apply(page: Page, turn: ReturnType<Page["locator"]>) {
  await turn
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

test("projeto acima de 60 KB gera, aplica e aceita novo pedido preservando arquivos", async ({
  page,
}, info) => {
  const initial = await catalogProject(page, 850);
  expect(
    new TextEncoder().encode(JSON.stringify(initial.files)).length,
  ).toBeGreaterThan(65_000);
  const first = await send(
    page,
    "Melhorar a página inicial preservando o catálogo existente da padaria.",
  );
  await expect(first).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  expect((await savedProject(page, initial.project.id)).files).toEqual(
    initial.files,
  );
  await expect(
    first.getByRole("button", { name: "Revisar alterações" }),
  ).toBeEnabled();
  await apply(page, first);
  const applied = await savedProject(page, initial.project.id);
  expect(applied.project.current_revision_id).not.toBe(
    initial.project.current_revision_id,
  );
  expect(applied.files["public/index.html"]).toContain("Alteração E2E");
  for (const [path, content] of Object.entries(initial.files))
    if (path !== "public/index.html") expect(applied.files[path]).toBe(content);
  const second = await send(
    page,
    "Agora aprimorar os botões da página inicial mantendo todo o catálogo.",
  );
  await expect(second).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  expect((await savedProject(page, initial.project.id)).files).toEqual(
    applied.files,
  );
  await apply(page, second);
  const followup = await savedProject(page, initial.project.id);
  expect(followup.files["public/index.html"]).not.toBe(
    applied.files["public/index.html"],
  );
  for (const [path, content] of Object.entries(initial.files))
    if (path !== "public/index.html")
      expect(followup.files[path]).toBe(content);
  await page.screenshot({
    path: info.outputPath("generation-large-project-followup.png"),
  });
});

test("continuação cruza 60 KB sem aplicar lote parcial e entrega todos os arquivos", async ({
  page,
}, info) => {
  const initial = await catalogProject(page, 400);
  const initialBytes = new TextEncoder().encode(
    JSON.stringify(initial.files),
  ).length;
  expect(initialBytes).toBeLessThan(55_000);
  const turn = await send(
    page,
    "CONTEXTO_CRESCENTE criar lista de produtos e aprimorar a página inicial da padaria.",
  );
  await expect(turn).toHaveAttribute("data-state", "running");
  await expect(turn.getByLabel("Arquivos em construção")).toContainText(
    "public/carrossel.js",
    { timeout: 15000 },
  );
  expect((await savedProject(page, initial.project.id)).files).toEqual(
    initial.files,
  );
  await expect(turn).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  expect((await savedProject(page, initial.project.id)).files).toEqual(
    initial.files,
  );
  await apply(page, turn);
  const applied = await savedProject(page, initial.project.id);
  expect(applied.files["public/carrossel.js"]).toContain(
    "export const products =",
  );
  expect(applied.files["public/index.html"]).toContain("Alteração E2E");
  expect(
    new TextEncoder().encode(JSON.stringify(applied.files)).length,
  ).toBeGreaterThan(60_000);
  for (const [path, content] of Object.entries(initial.files))
    if (path !== "public/index.html") expect(applied.files[path]).toBe(content);
  await page.screenshot({
    path: info.outputPath("generation-growing-context-complete.png"),
  });
});
