import { expect, test, type Page } from "@playwright/test";
import { productionPolicy } from "./productionPolicy";
import { reviewRequest, showBuildHistory, workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Use backend/script/test-workspace.py para iniciar o laboratório de construção.",
);

test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

async function readyProposal(page: Page, request: string) {
  const project = await workspace(page);
  await reviewRequest(page, request);
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  const turn = page.locator(".build-turn").last();
  await expect(turn.locator(".build-request")).toContainText(request);
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  await turn
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(page.getByRole("region", { name: /^Diff de/ })).toBeVisible();
  return project;
}

async function appliedState(page: Page, pid: string, oid: string) {
  return page.evaluate(
    async ({ pid, oid }) => {
      const project = await (await fetch(`/api/v1/projects/${pid}`)).json();
      const proposal = await (
        await fetch(`/api/v1/projects/${pid}/proposals/${oid}`)
      ).json();
      const draft = await (await fetch(`/api/v1/projects/${pid}/draft`)).json();
      return { project, proposal, draft };
    },
    { pid, oid },
  );
}

test("resposta perdida depois do commit é reconciliada sem repetir a aplicação", async ({
  page,
}) => {
  const project = await readyProposal(
    page,
    "Melhorar a página e preservar a confirmação após falha de rede",
  );
  let mutations = 0;
  let operation = "";
  await page.route("**/proposals/*/apply", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    mutations++;
    operation = new URL(route.request().url()).pathname.split("/").at(-2)!;
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    await route.abort("connectionreset");
  });
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByText("Alterações aplicadas à revisão salva.", { exact: true }),
  ).toBeVisible();
  const state = await appliedState(page, project.id, operation);
  expect(mutations).toBe(1);
  expect(state.proposal.state).toBe("applied");
  expect(state.project.current_revision_id).toBe(
    state.proposal.applied_revision_id,
  );
  expect(state.project.current_revision_id).not.toBe(
    project.current_revision_id,
  );
  expect(state.draft.data.base_revision_id).toBe(
    state.proposal.applied_revision_id,
  );
  expect(state.draft.data.files).toBeNull();
  await expect(page.locator(".code-editor")).toHaveValue(/Alteração E2E/);
});

test("recarregar durante a confirmação restaura a revisão aplicada sem um segundo POST", async ({
  page,
}) => {
  const project = await readyProposal(
    page,
    "Melhorar o conteúdo para validar recarga durante aplicação",
  );
  let mutations = 0;
  let operation = "";
  let committed!: () => void;
  let release!: () => void;
  const commit = new Promise<void>((resolve) => {
    committed = resolve;
  });
  const hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/proposals/*/apply", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    mutations++;
    operation = new URL(route.request().url()).pathname.split("/").at(-2)!;
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    committed();
    await hold;
    await route.fulfill({ response }).catch(() => undefined);
  });
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await commit;
  const reload = page.reload({ waitUntil: "domcontentloaded" });
  release();
  await reload;
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  await expect(page.locator(".code-editor")).toHaveValue(/Alteração E2E/);
  const state = await appliedState(page, project.id, operation);
  expect(mutations).toBe(1);
  expect(state.proposal.state).toBe("applied");
  expect(state.project.current_revision_id).toBe(
    state.proposal.applied_revision_id,
  );
  expect(state.draft.data.base_revision_id).toBe(
    state.proposal.applied_revision_id,
  );
  await showBuildHistory(page);
  await expect(page.locator(".build-turn").last()).toContainText(
    "Alterações revisadas e aplicadas.",
  );
});
