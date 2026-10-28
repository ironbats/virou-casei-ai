import { expect, type BrowserContext, type Page } from "@playwright/test";

let sessionCookies: Awaited<ReturnType<BrowserContext["cookies"]>> | undefined;
export async function workspace(page: Page, guided = false) {
  if (sessionCookies) {
    await page.context().addCookies(sessionCookies);
    await page.goto("/app/projetos");
  } else {
    await page.goto("/login");
    await page.getByLabel("Login", { exact: true }).fill("student-e2e");
    await page
      .getByLabel("Senha", { exact: true })
      .fill("e2e-student-only-password");
    await page.getByRole("button", { name: "Entrar", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Meus projetos", exact: true }),
    ).toBeVisible();
    sessionCookies = await page.context().cookies();
  }
  const project = await page.evaluate(async () => {
    const me = await (await fetch("/api/v1/me")).json();
    const existing = await (await fetch("/api/v1/projects")).json();
    if (existing.items.length) {
      const project = existing.items[0];
      const draft = await (
        await fetch(`/api/v1/projects/${project.id}/draft`)
      ).json();
      const reset = await fetch(`/api/v1/projects/${project.id}/draft`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": me.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": String(draft.version),
        },
        body: JSON.stringify({
          base_revision_id: project.current_revision_id,
          files: null,
          prompt: draft.data.prompt,
        }),
      });
      if (!reset.ok) throw new Error(await reset.text());
      return project;
    }
    const response = await fetch("/api/v1/projects", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        name: "Projeto de construção",
        objective: "Criar aplicação com acompanhamento de progresso",
      }),
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  });
  if (!guided)
    await page.evaluate(async (project) => {
      const me = await (await fetch("/api/v1/me")).json();
      const headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
      };
      const operations = await (
        await fetch(`/api/v1/operations?project_id=${project.id}&latest=true`)
      ).json();
      for (const op of operations.items) {
        if (
          op.state === "succeeded" &&
          op.result_ref.candidate &&
          !op.result_ref.applied_revision_id &&
          !op.result_ref.discarded_at
        )
          await fetch(
            `/api/v1/projects/${project.id}/proposals/${op.id}/discard`,
            {
              method: "POST",
              headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
              body: "{}",
            },
          );
      }
      const draft = await (
        await fetch(`/api/v1/projects/${project.id}/build-draft`)
      ).json();
      const response = await fetch(
        `/api/v1/projects/${project.id}/build-draft`,
        {
          method: "PUT",
          headers: {
            ...headers,
            "Idempotency-Key": crypto.randomUUID(),
            "If-Match": String(draft.version),
          },
          body: JSON.stringify({
            stage: "review",
            objective: project.objective,
            message_text: "",
            attachments: [],
            recommendation_id: null,
          }),
        },
      );
      if (!response.ok) throw Error(await response.text());
    }, project);
  await page.goto(`/app/projetos/${project.id}`);
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  return project;
}
