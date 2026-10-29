import { expect, test, type Page } from "@playwright/test";
import type { Operation, Project } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";

test.skip(
  process.env.VIRA_E2E_BUILD_DIAGNOSTIC !== "true",
  "Requer o laboratório isolado com falha real de esbuild persistida na operação.",
);

async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`/api/v1${path}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

test("erro real de esbuild chega à interface, persiste após recarga e preserva o rascunho ao revisar código", async ({
  page,
  context,
}, info) => {
  await productionPolicy(page);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/login");
  await page.getByLabel("Login", { exact: true }).fill("student-e2e");
  await page
    .getByLabel("Senha", { exact: true })
    .fill("e2e-student-only-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Meus projetos", exact: true }),
  ).toBeVisible();
  const projects = await read<{ items: Project[] }>(page, "/projects");
  const project = projects.items.find(
    (item) => item.name === "Diagnóstico real de compilação",
  );
  expect(
    project,
    "O laboratório deve criar uma falha de compilação real antes de iniciar o navegador.",
  ).toBeDefined();
  const pid = project!.id;
  const operations = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&latest=true`,
  );
  const operation = operations.items.find(
    (item) =>
        item.type === "publication" &&
      item.state === "failed" &&
      item.error_code === "BUILD_FAILED",
  );
  expect(
    operation,
    "A operação de publicação com falha deve estar persistida e pertencer ao aluno.",
  ).toBeDefined();
  const build = operation!.result_ref.publication_failure?.build;
  expect(build).toMatchObject({
    format_version: 1,
    category: "source",
    tool: "esbuild",
    operation_id: operation!.id,
    correlation_id: operation!.id,
    revision_id: project!.current_revision_id,
  });
  expect(build!.issues).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        code: "SYNTAX_ERROR",
        path: "src/index.ts",
        line: 3,
        column: 7,
      }),
    ]),
  );
  expect(JSON.stringify(build)).not.toContain("synthetic-private-source");
  const savedDraft = await page.evaluate(
    async ({ pid, revisionId }) => {
      const json = async (path: string) => {
        const response = await fetch(`/api/v1${path}`);
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        return response.json();
      };
      const user = await json("/me");
      const draft = await json(`/projects/${pid}/draft`);
      const revision = await json(`/projects/${pid}/revisions/${revisionId}`);
      const entries = await Promise.all(
        revision.files.map(async (file: { id: string }) => {
          const result = await json(
            `/projects/${pid}/revisions/${revisionId}/files/${file.id}`,
          );
          return [result.path, result.content];
        }),
      );
      const files = Object.fromEntries(entries);
      files["src/index.ts"] += '\nconst reviewMarker = "draft-preserved";\n';
      const response = await fetch(`/api/v1/projects/${pid}/draft`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": user.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": String(draft.version),
        },
        body: JSON.stringify({
          base_revision_id: revisionId,
          files,
          prompt: "Contexto de revisão preservado durante o diagnóstico.",
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      return json(`/projects/${pid}/draft`);
    },
    { pid, revisionId: project!.current_revision_id },
  );
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() !== "GET" &&
      request.url().includes(`/projects/${pid}/`)
    )
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await page.goto(`/app/projetos/${pid}/publicar?operation=${operation!.id}`);
  await expect(
    page.getByRole("heading", {
      name: "Publicação não concluída",
      exact: true,
    }),
  ).toBeVisible();
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da compilação",
    exact: true,
  });
  await expect(diagnostic).toContainText("src/index.ts:3:7");
  await expect(diagnostic).toContainText("SYNTAX_ERROR");
  await expect(diagnostic).toContainText('Expected identifier but found "="');
  await expect(diagnostic).toContainText(operation!.id);
  await expect(diagnostic).toContainText(project!.current_revision_id!);
  await expect(diagnostic).not.toContainText("synthetic-private-source");
  await diagnostic.getByRole("button", { name: "Copiar diagnóstico" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain(`Correlação: ${operation!.id}`);
  expect(copied).toContain("src/index.ts:3:7");
  expect(copied).not.toContain("synthetic-private-source");
  await page.screenshot({
    path: info.outputPath("real-esbuild-diagnostic.png"),
    fullPage: true,
  });
  await page.reload();
  await expect(diagnostic).toContainText("src/index.ts:3:7");
  await diagnostic
    .getByRole("link", { name: "Revisar código do projeto" })
    .click();
  await expect(page).toHaveURL(`/app/projetos/${pid}`);
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  const after = await read<Project>(page, `/projects/${pid}`);
  expect(after.current_revision_id).toBe(project!.current_revision_id);
  expect(await read(page, `/projects/${pid}/draft`)).toEqual(savedDraft);
  expect(writes).toEqual([]);
  expect(
    (await read<Operation>(page, `/operations/${operation!.id}`)).result_ref
      .publication_failure?.build,
  ).toEqual(build);
});
