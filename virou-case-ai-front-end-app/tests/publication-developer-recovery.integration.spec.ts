import { expect, test, type Page } from "@playwright/test";
import type { Operation, Project } from "../src/shared/types";
import type { RecoverySnapshot } from "../src/features/publicationRecovery";
import type { PublicationReadiness } from "../src/features/usePublication";
import { productionPolicy } from "./productionPolicy";

test.skip(
  process.env.VIRA_E2E_DEVELOPER_RECOVERY !== "true",
  "Requer o laboratório isolado de recuperação com API, banco e worker reais e somente o provedor D1 simulado.",
);

async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`/api/v1${path}`);
  expect(response.ok(), `${path}: ${response.status()}`).toBe(true);
  return response.json();
}

test("proprietário recupera a tentativa pela interface pública real, retoma após reload e preserva revisão, histórico e quota", async ({
  page,
}, info) => {
  await productionPolicy(page);
  await page.goto("/login");
  await page
    .getByLabel("Login", { exact: true })
    .fill(process.env.VIRA_E2E_RECOVERY_LOGIN ?? "student0");
  await page
    .getByLabel("Senha", { exact: true })
    .fill(process.env.VIRA_E2E_RECOVERY_PASSWORD ?? "test-student-password");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Meus projetos", exact: true }),
  ).toBeVisible();
  const projects = await read<{ items: Project[] }>(page, "/projects");
  const project =
    projects.items.find((item) =>
      process.env.VIRA_E2E_RECOVERY_PROJECT_ID
        ? item.id === process.env.VIRA_E2E_RECOVERY_PROJECT_ID
        : /recupera/i.test(item.name),
    ) ?? projects.items[0];
  expect(project).toBeDefined();
  const operations = await read<{ items: Operation[] }>(
    page,
    `/operations?project_id=${project.id}&latest=true&kind=publication`,
  );
  const operation = operations.items.find(
    (item) => item.type === "publication" && item.state === "reconciling",
  );
  expect(operation).toBeDefined();
  const path = `/operations/${operation!.id}/publication-recovery`;
  const before = await read<RecoverySnapshot>(page, path);
  expect(before.capabilities?.can_check_and_resolve).toBe(true);
  const readinessBefore = await read<PublicationReadiness>(
    page,
    `/projects/${project.id}/publication-readiness`,
  );
  const walletBefore = await read<Record<string, unknown>>(page, "/wallet");
  const mutations: { path: string; body: unknown }[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/api/v1/"))
      mutations.push({ path, body: request.postDataJSON() });
  });
  await page.goto(
    `/app/projetos/${project.id}/publicar?operation=${operation!.id}`,
  );
  const panel = page.getByRole("region", {
    name: "Recuperar publicação",
    exact: true,
  });
  const resolve = panel.getByRole("button", {
    name: "Conferir e resolver",
    exact: true,
  });
  await expect(resolve).toBeEnabled();
  await expect(panel).toContainText("Nenhum comando de banco será repetido.");
  const diagnostic = page.getByRole("region", {
    name: "Diagnóstico da tentativa",
    exact: true,
  });
  expect(before.migration?.name).toBeTruthy();
  await expect(diagnostic).toContainText(before.migration!.name);
  await expect(diagnostic).toContainText(
    "A causa exata da recusa não está confirmada.",
  );
  await diagnostic
    .getByText("Dados técnicos da recusa", { exact: true })
    .click();
  await expect(diagnostic).toContainText("400");
  await expect(diagnostic).toContainText("7500");
  await diagnostic
    .getByText("Dados técnicos da recusa", { exact: true })
    .click();
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({
    path: info.outputPath("developer-recovery-fullstack-pending.png"),
    fullPage: true,
  });
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/v1${path}/resolve`,
  );
  await resolve.click();
  expect((await accepted).status()).toBe(202);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.reload();
  await expect(panel).toContainText(
    "Pendência encerrada. Nenhuma migration foi reaplicada.",
    { timeout: 30000 },
  );
  const after = await read<RecoverySnapshot>(page, path);
  const finished = await read<Operation>(page, `/operations/${operation!.id}`);
  const currentProject = await read<Project>(page, `/projects/${project.id}`);
  const readinessAfter = await read<PublicationReadiness>(
    page,
    `/projects/${project.id}/publication-readiness`,
  );
  const walletAfter = await read<Record<string, unknown>>(page, "/wallet");
  expect(after.classification).toBe("resolved");
  expect(after.request?.state).toBe("resolved");
  expect(after.request?.id).toBe(after.recovery_request_id);
  expect(finished.state).toBe("failed");
  expect(finished.result_ref.revision_id).toBe(
    operation!.result_ref.revision_id,
  );
  expect(currentProject.current_revision_id).toBe(project.current_revision_id);
  expect(readinessAfter.publication).toEqual(readinessBefore.publication);
  expect(after.diagnostic.initial_error).toEqual(
    before.diagnostic.initial_error,
  );
  for (const field of [
    "available_ratio",
    "reserved_ratio",
    "consumed_ratio",
    "pending_reconciliation",
  ])
    expect(walletAfter[field]).toEqual(walletBefore[field]);
  expect(mutations).toHaveLength(1);
  expect(mutations[0].path).toBe(`/api/v1${path}/resolve`);
  expect(mutations[0].body).toMatchObject({
    action: "check_and_close_if_no_effect",
    consent: "verify_and_close_failed_attempt_when_proven_safe",
  });
  const publish = page.getByRole("button", {
    name: "Publicar esta revisão",
    exact: true,
  });
  if (readinessAfter.can_publish) await expect(publish).toBeEnabled();
  else await expect(publish).toBeDisabled();
  const phases = page.getByRole("list", {
    name: "Etapas da publicação",
    exact: true,
  });
  await expect(
    phases.getByRole("listitem").filter({ hasText: "Enviar à hospedagem" }),
  ).not.toContainText("Concluída");
  await expect(
    phases.getByRole("listitem").filter({ hasText: "Verificar publicação" }),
  ).not.toContainText("Concluída");
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({
    path: info.outputPath("developer-recovery-fullstack-resolved.png"),
    fullPage: true,
  });
});
