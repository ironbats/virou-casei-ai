import { expect, test, type Page } from "@playwright/test";
import type { Operation, Project, ProjectDraft } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";
import { workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_CODE_ANALYSIS !== "true",
  "Requer o backend isolado com VIRA_E2E_ISOLATED=true e VIRA_E2E_CODE_ANALYSIS=true.",
);

async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`/api/v1${path}`);
  expect(response.ok()).toBe(true);
  return response.json();
}

test("API e worker reais analisam revisão, geram correção e aplicam somente após revisão explícita", async ({
  page,
}, info) => {
  test.setTimeout(90000);
  await productionPolicy(page);
  const initial = await workspace(page);
  const seeded = await page.evaluate(async (pid) => {
    const read = async (path: string) => {
      const response = await fetch(`/api/v1${path}`);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    };
    const user = await read("/me");
    const project = await read(`/projects/${pid}`);
    const revision = await read(
      `/projects/${pid}/revisions/${project.current_revision_id}`,
    );
    const entries = await Promise.all(
      revision.files.map((file: { id: string }) =>
        read(`/projects/${pid}/revisions/${revision.id}/files/${file.id}`),
      ),
    );
    const files = Object.fromEntries(
      entries.map((file: { path: string; content: string }) => [
        file.path,
        file.content,
      ]),
    );
    if (typeof files["src/index.ts"] !== "string")
      throw new Error("O fixture deve fornecer src/index.ts.");
    files["src/index.ts"] += "\nconst = 1;\n";
    const response = await fetch(`/api/v1/projects/${pid}/revisions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": user.csrf_token,
        "Idempotency-Key": crypto.randomUUID(),
        "If-Match": String(project.version),
      },
      body: JSON.stringify({ base_revision_id: revision.id, files }),
    });
    if (!response.ok) throw new Error(await response.text());
    const saved = await response.json();
    const draft = await read(`/projects/${pid}/draft`);
    const reset = await fetch(`/api/v1/projects/${pid}/draft`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": user.csrf_token,
        "Idempotency-Key": crypto.randomUUID(),
        "If-Match": String(draft.version),
      },
      body: JSON.stringify({
        base_revision_id: saved.revision.id,
        files: null,
        prompt: draft.data.prompt,
      }),
    });
    if (!reset.ok) throw new Error(await reset.text());
    return { project: saved.project, files };
  }, initial.id);
  const pid = initial.id;
  await page.reload();
  const panel = page.getByRole("region", { name: "Análise de código com IA" });
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() !== "GET" &&
      request.url().includes(`/projects/${pid}/`)
    )
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    panel.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible({ timeout: 30000 });
  await expect(panel).toContainText("Declaração inválida");
  await expect(panel).toContainText("src/index.ts");
  const analyses = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&kind=code_analysis&latest=true`,
  );
  const analysis = analyses.items[0];
  expect(analysis.state).toBe("succeeded");
  expect(analysis.result_ref.revision_id).toBe(
    seeded.project.current_revision_id,
  );
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(seeded.project.current_revision_id);
  expect(
    (await read<{ data: ProjectDraft }>(page, `/projects/${pid}/draft`)).data
      .files,
  ).toBeNull();
  await page.reload();
  await expect(panel).toContainText("Declaração inválida");
  await page.screenshot({
    path: info.outputPath("analysis-integrated-desktop.png"),
    fullPage: true,
  });
  await panel
    .getByRole("button", { name: "Solicitar correção IA", exact: true })
    .click();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toBeVisible();
  await expect(page.locator(".ide-panel .code-analysis")).toHaveCount(0);
  await expect(page.locator(".code-analysis-correction")).toHaveCount(0);
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  const completed = await read<Operation>(page, `/operations/${analysis.id}`);
  expect(completed.result_ref.correction_operation_id).toBeTruthy();
  const correction = await read<Operation>(
    page,
    `/operations/${completed.result_ref.correction_operation_id}`,
  );
  expect(correction.state).toBe("succeeded");
  expect(correction.result_ref.candidate).toBe(true);
  expect(correction.result_ref.applied_revision_id).toBeFalsy();
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(seeded.project.current_revision_id);
  await page.reload();
  await expect(chat).toBeVisible();
  await chat
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("src/index.ts");
  await expect(page.getByRole("region", { name: /^Diff de/ })).toContainText(
    "diagnosticValue",
  );
  expect(writes.some((path) => path.endsWith("/apply"))).toBe(false);
  await page.screenshot({
    path: info.outputPath("analysis-integrated-proposal.png"),
    fullPage: true,
  });
  await dialog
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const current = await read<Project>(page, `/projects/${pid}`);
  expect(current.current_revision_id).not.toBe(
    seeded.project.current_revision_id,
  );
  const updated = await read<Operation>(page, `/operations/${correction.id}`);
  expect(updated.result_ref.applied_revision_id).toBe(
    current.current_revision_id,
  );
  const source = await page.evaluate(
    async ({ pid, revisionId }) => {
      const revision = await (
        await fetch(`/api/v1/projects/${pid}/revisions/${revisionId}`)
      ).json();
      const files = await Promise.all(
        revision.files.map(async (file: { id: string }) =>
          (
            await fetch(
              `/api/v1/projects/${pid}/revisions/${revisionId}/files/${file.id}`,
            )
          ).json(),
        ),
      );
      return Object.fromEntries(
        files.map((file: { path: string; content: string }) => [
          file.path,
          file.content,
        ]),
      );
    },
    { pid, revisionId: current.current_revision_id },
  );
  expect(source["src/index.ts"]).toBe(
    seeded.files["src/index.ts"].replace(
      "const = 1;",
      "const diagnosticValue = 1;",
    ),
  );
  for (const [path, content] of Object.entries(seeded.files))
    if (path !== "src/index.ts") expect(source[path]).toBe(content);
  expect(writes.filter((path) => path.endsWith("/apply"))).toHaveLength(1);
  expect(writes.filter((path) => path.endsWith("/code-analyses"))).toHaveLength(
    1,
  );
  expect(writes.filter((path) => path.endsWith("/correction"))).toHaveLength(1);
});

test("40 arquivos: recuperação de alvo inválido propõe quatro exclusões, revisa o arquivo vazio e preserva os demais", async ({
  page,
}, info) => {
  test.setTimeout(90000);
  await productionPolicy(page);
  await workspace(page);
  const seeded = await page.evaluate(async () => {
    const get = async (path: string) => {
      const response = await fetch(`/api/v1${path}`);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    };
    const user = await get("/me");
    const send = async (
      path: string,
      method: string,
      body: unknown,
      version?: number,
    ) => {
      const response = await fetch(`/api/v1${path}`, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": user.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          ...(version === undefined ? {} : { "If-Match": String(version) }),
        },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    };
    const project = await send("/projects", "POST", {
      name: "Correção de quarenta arquivos",
      objective:
        "Validar a remoção revisada de arquivos obsoletos preservando as implementações atuais",
    });
    const revision = await get(
      `/projects/${project.id}/revisions/${project.current_revision_id}`,
    );
    const entries = await Promise.all(
      revision.files.map((file: { id: string }) =>
        get(
          `/projects/${project.id}/revisions/${revision.id}/files/${file.id}`,
        ),
      ),
    );
    const files: Record<string, string> = Object.fromEntries(
      entries.map((file: { path: string; content: string }) => [
        file.path,
        file.content,
      ]),
    );
    Object.assign(files, {
      "src/domain/user.ts": "",
      "src/application/authenticate-user.ts":
        'import type { User } from "../domain/user";\nexport const diagnosticFixtureObsoleteAuthentication = true;\n',
      "src/infrastructure/d1-user-repository.ts":
        'import type { UserRepository } from "../domain/user";\nexport const diagnosticFixtureObsoleteRepository = true;\n',
      "src/infrastructure/health-check-service.ts":
        "export const diagnosticFixtureObsoleteHealthCheck = true;\n",
      "src/domain/entities/user.ts": "export interface User { id: string; }\n",
      "src/domain/repositories/user-repository.ts":
        "export interface UserRepository { find(id: string): Promise<unknown>; }\n",
      "src/application/services/authenticate-user.ts":
        'export const authenticateUser = async () => ({ id: "current-user" });\n',
      "src/infrastructure/repositories/d1/d1-user-repository.ts":
        "export const currentUserRepository = { find: async (id: string) => ({ id }) };\n",
      "src/infrastructure/services/health-check-service.ts":
        'export const currentHealthCheck = () => "ok";\n',
    });
    for (let index = 0; Object.keys(files).length < 40; index++)
      files[`src/context/fixture-${index}.ts`] =
        `export const context${index} = ${index};\n`;
    if (Object.keys(files).length !== 40)
      throw new Error("O cenário exige exatamente quarenta arquivos.");
    const saved = await send(
      `/projects/${project.id}/revisions`,
      "POST",
      {
        base_revision_id: project.current_revision_id,
        files,
      },
      project.version,
    );
    const draft = await get(`/projects/${project.id}/draft`);
    await send(
      `/projects/${project.id}/draft`,
      "PUT",
      {
        base_revision_id: saved.revision.id,
        files: null,
        prompt: draft.data.prompt,
      },
      draft.version,
    );
    return { project: saved.project, files };
  });
  const pid = seeded.project.id;
  await page.goto(`/app/projetos/${pid}`);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() !== "GET" &&
      request.url().includes(`/projects/${pid}/`)
    )
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  const panel = page.getByRole("region", { name: "Análise de código com IA" });
  await expect(panel).toContainText(
    "Foram encontrados quatro arquivos obsoletos",
    { timeout: 30000 },
  );
  await expect(panel).toContainText("Arquivo de domínio vazio");
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(seeded.project.current_revision_id);
  await panel
    .getByRole("button", { name: "Solicitar correção IA", exact: true })
    .click();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toBeVisible();
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeEnabled({ timeout: 30000 });
  await expect(chat).toContainText("4 excluído(s)");
  await expect(chat).not.toContainText('"summary":');
  const analyses = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${pid}&kind=code_analysis&latest=true`,
  );
  const analysis = analyses.items[0];
  const correction = await read<Operation>(
    page,
    `/operations/${analysis.result_ref.correction_operation_id}`,
  );
  const deleted = [
    "src/domain/user.ts",
    "src/application/authenticate-user.ts",
    "src/infrastructure/d1-user-repository.ts",
    "src/infrastructure/health-check-service.ts",
  ];
  expect(correction.state).toBe("succeeded");
  expect(correction.result_ref.candidate).toBe(true);
  expect(correction.result_ref.applied_revision_id).toBeFalsy();
  expect(correction.analysis_id).toBe(analysis.id);
  expect(
    correction.activity?.some((event) => event.stage === "repairing_response"),
  ).toBe(true);
  expect(
    correction.result_ref.changed_files?.map((file) => file.path).sort(),
  ).toEqual([...deleted].sort());
  expect(
    correction.result_ref.changed_files?.every(
      (file) => file.change === "deleted",
    ),
  ).toBe(true);
  expect(
    (await read<Project>(page, `/projects/${pid}`)).current_revision_id,
  ).toBe(seeded.project.current_revision_id);
  await page.reload();
  await chat
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("conjunto de 4 arquivo(s)");
  await dialog.getByRole("button", { name: /src\/domain\/user.ts/ }).click();
  await expect(dialog).toContainText(
    "Arquivo vazio. A proposta remove este arquivo.",
  );
  await page.screenshot({
    path: info.outputPath("correction-forty-files-empty-deletion.png"),
    fullPage: true,
  });
  expect(writes.some((path) => path.endsWith("/apply"))).toBe(false);
  await dialog
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const current = await read<Project>(page, `/projects/${pid}`);
  expect(current.current_revision_id).not.toBe(
    seeded.project.current_revision_id,
  );
  const result = await page.evaluate(
    async ({ pid, revisionId }) => {
      const revision = await (
        await fetch(`/api/v1/projects/${pid}/revisions/${revisionId}`)
      ).json();
      const entries = await Promise.all(
        revision.files.map(async (file: { id: string }) =>
          (
            await fetch(
              `/api/v1/projects/${pid}/revisions/${revisionId}/files/${file.id}`,
            )
          ).json(),
        ),
      );
      return Object.fromEntries(
        entries.map((file: { path: string; content: string }) => [
          file.path,
          file.content,
        ]),
      );
    },
    { pid, revisionId: current.current_revision_id },
  );
  expect(Object.keys(result)).toHaveLength(36);
  for (const [path, content] of Object.entries(seeded.files)) {
    if (deleted.includes(path))
      expect(Object.prototype.hasOwnProperty.call(result, path)).toBe(false);
    else expect(result[path]).toBe(content);
  }
  const updated = await read<Operation>(page, `/operations/${correction.id}`);
  expect(updated.result_ref.applied_revision_id).toBe(
    current.current_revision_id,
  );
  expect(writes.filter((path) => path.endsWith("/apply"))).toHaveLength(1);
  expect(writes.filter((path) => path.endsWith("/code-analyses"))).toHaveLength(
    1,
  );
  expect(writes.filter((path) => path.endsWith("/correction"))).toHaveLength(1);
});
