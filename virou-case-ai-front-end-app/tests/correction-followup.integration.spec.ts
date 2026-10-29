import { expect, test, type Page } from "@playwright/test";
import type { Operation, Project } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";

test.skip(
  process.env.VIRA_E2E_CORRECTION_FOLLOWUP !== "true",
  "Requer o backend isolado com VIRA_E2E_CODE_ANALYSIS=true e VIRA_E2E_CORRECTION_FOLLOWUP=true.",
);

async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`/api/v1${path}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

async function source(page: Page, pid: string, revision: string) {
  const manifest = await read<{ files: { id: string }[] }>(
    page,
    `/projects/${pid}/revisions/${revision}`,
  );
  const files = await Promise.all(
    manifest.files.map((file) =>
      read<{ path: string; content: string }>(
        page,
        `/projects/${pid}/revisions/${revision}/files/${file.id}`,
      ),
    ),
  );
  return Object.fromEntries(files.map((file) => [file.path, file.content]));
}

test("falha de build segue por análise, pergunta, resposta com exclusões repetidas e aplicação revisada", async ({
  page,
}, info) => {
  test.setTimeout(120000);
  await productionPolicy(page);
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
    (item) => item.name === "Correção após pergunta",
  );
  expect(project).toBeDefined();
  const pid = project!.id;
  const revision = project!.current_revision_id!;
  const before = await source(page, pid, revision);
  expect(Object.keys(before)).toHaveLength(40);
  const initial = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&latest=true`,
  );
  const publication = initial.items.find((item) => item.type === "publication");
  expect(publication).toMatchObject({
    state: "failed",
    error_code: "BUILD_FAILED",
  });
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().includes(`/projects/${pid}/`)
    )
      writes.push(new URL(request.url()).pathname);
  });
  await page.goto(`/app/projetos/${pid}/publicar?operation=${publication!.id}`);
  await expect(
    page.getByRole("heading", {
      name: "Publicação não concluída",
      exact: true,
    }),
  ).toBeVisible();
  const analysisPanel = page.getByRole("region", {
    name: "Análise de código com IA",
    exact: true,
  });
  await analysisPanel
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    analysisPanel.getByRole("heading", {
      name: "Diagnóstico da IA",
      exact: true,
    }),
  ).toBeVisible({ timeout: 30000 });
  await expect(analysisPanel).toContainText("Evolução incremental do banco");
  const analyses = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&kind=code_analysis&latest=true`,
  );
  const analysis = analyses.items[0];
  expect(analysis.result_ref.publication_operation_id).toBe(publication!.id);
  await analysisPanel
    .getByRole("button", { name: "Solicitar correção IA", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/app/projetos/${pid}\\?conversation=`),
  );
  const chat = page.getByRole("region", {
    name: "Conversa de construção",
    exact: true,
  });
  await expect(chat).toContainText(
    "Podemos manter os dados atuais e concluir a correção do cadastro?",
    { timeout: 30000 },
  );
  const history = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&latest=true`,
  );
  const question = history.items.find(
    (item) => item.result_ref.kind === "clarification",
  );
  expect(question).toMatchObject({
    state: "succeeded",
    analysis_id: analysis.id,
  });
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(revision);
  expect(await source(page, pid, revision)).toEqual(before);
  await page.reload();
  await expect(chat).toContainText(
    "Podemos manter os dados atuais e concluir a correção do cadastro?",
  );
  await chat.getByRole("button", { name: "Responder", exact: true }).click();
  await page
    .getByLabel("Sua resposta ao agente", { exact: true })
    .fill("Sim, mantenha os dados atuais e conclua a correção.");
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  const afterReply = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&latest=true`,
  );
  const correction = afterReply.items.find(
    (item) => item.reply_to === question!.id,
  );
  expect(correction).toMatchObject({
    state: "succeeded",
    analysis_id: analysis.id,
    billing_status: "settled",
  });
  expect(correction!.result_ref.candidate).toBe(true);
  expect(correction!.result_ref.applied_revision_id).toBeFalsy();
  expect(correction!.result_ref.changed_files).toHaveLength(6);
  expect(
    afterReply.items.filter((item) => item.type === "publication"),
  ).toHaveLength(1);
  expect(
    afterReply.items.filter((item) => item.type === "generation"),
  ).toHaveLength(2);
  expect(
    afterReply.items.some(
      (item) => item.type === "generation" && item.state === "failed",
    ),
  ).toBe(false);
  expect(
    (await read<Operation>(page, `/operations/${analysis.id}`)).result_ref
      .correction_operation_id,
  ).toBe(correction!.id);
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(revision);
  await page.reload();
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeEnabled();
  await expect(
    chat.getByRole("button", { name: "Responder", exact: true }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", {
      name: "Tentar responder novamente",
      exact: true,
    }),
  ).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("correction-followup-proposal.png"),
    fullPage: true,
  });
  await chat
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("migrations/0002.sql");
  await expect(dialog).toContainText("migrations/0003.sql");
  expect(writes.some((path) => path.endsWith("/apply"))).toBe(false);
  await dialog
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const applied = await read<Project>(page, `/projects/${pid}`);
  expect(applied.current_revision_id).not.toBe(revision);
  const files = await source(page, pid, applied.current_revision_id!);
  expect(Object.keys(files)).toHaveLength(37);
  const removed = [
    "src/domain/followup-obsolete.ts",
    "src/application/followup-obsolete.ts",
    "src/infrastructure/followup-obsolete.ts",
    "public/followup-obsolete.js",
  ];
  const originalSql =
    "CREATE TABLE correction_requests (id TEXT PRIMARY KEY, title TEXT NOT NULL);\n";
  const incrementalSql =
    "ALTER TABLE correction_requests ADD COLUMN note TEXT;\n";
  expect(files["migrations/0002.sql"]).toBe(originalSql);
  expect(files["migrations/0003.sql"]).toBe(incrementalSql);
  expect(files["public/index.html"]).toBe(
    before["public/index.html"].replace(
      "</body>",
      "<p>Correção após resposta concluída</p></body>",
    ),
  );
  for (const [path, content] of Object.entries(before)) {
    if (removed.includes(path)) expect(Object.hasOwn(files, path)).toBe(false);
    else if (path !== "public/index.html")
      expect(files[path]).toBe(content);
  }
  expect(
    (await read<Operation>(page, `/operations/${correction!.id}`)).result_ref
      .applied_revision_id,
  ).toBe(applied.current_revision_id);
  await page.reload();
  await expect(chat).toContainText("Alterações revisadas e aplicadas.");
  const tree = page.getByRole("tree", {
    name: "Arquivos do projeto",
    exact: true,
  });
  await expect(tree).toContainText("0003.sql");
  await expect(tree).not.toContainText("followup-obsolete");
  expect(writes.filter((path) => path.endsWith("/code-analyses"))).toHaveLength(
    1,
  );
  expect(writes.filter((path) => path.endsWith("/correction"))).toHaveLength(1);
  expect(writes.filter((path) => path.endsWith("/reply"))).toHaveLength(1);
  expect(writes.filter((path) => path.endsWith("/apply"))).toHaveLength(1);
  expect(writes.some((path) => path.endsWith("/publish"))).toBe(false);
  await page.screenshot({
    path: info.outputPath("correction-followup-applied.png"),
    fullPage: true,
  });
});
