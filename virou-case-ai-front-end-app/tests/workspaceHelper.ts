import { expect, type BrowserContext, type Page } from "@playwright/test";

let sessionCookies: Awaited<ReturnType<BrowserContext["cookies"]>> | undefined;

export async function editorAction(page: Page, name: string) {
  const action = page.getByRole("button", { name, exact: true });
  if (!(await action.isVisible()))
    await page
      .getByRole("button", { name: "Opções do editor", exact: true })
      .click();
  await action.click();
}

export async function showConversation(page: Page) {
  const narrow = (page.viewportSize()?.width ?? 1280) < 1180;
  await expect
    .poll(() =>
      page
        .locator(".ide-layout")
        .evaluate((node) => node.classList.contains("is-narrow")),
    )
    .toBe(narrow);
  if (narrow)
    await page
      .getByRole("tablist", { name: "Área do projeto" })
      .getByRole("tab", { name: "Chat", exact: true })
      .click();
}

export async function projectAction(page: Page, name: string) {
  await page
    .getByRole("button", { name: "Mais ações do projeto", exact: true })
    .click();
  await page.getByRole("link", { name, exact: true }).click();
}

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
    const project = existing.items.find(
      (item: { name: string }) => item.name === "Projeto de construção",
    );
    if (project) {
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
            stage: "idea",
            objective: "",
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
  await editorAction(page, "Usar texto simples");
  return project;
}

export async function reviewRequest(page: Page, request: string) {
  await expect
    .poll(
      async () =>
        (await page
          .getByLabel("O que vamos fazer?", { exact: true })
          .isVisible()) ||
        (await page
          .getByRole("button", { name: "Nova construção", exact: true })
          .isVisible()),
    )
    .toBe(true);
  if (
    !(await page.getByLabel("O que vamos fazer?", { exact: true }).isVisible())
  )
    await page
      .getByRole("button", { name: "Nova construção", exact: true })
      .click();
  await page.getByLabel("O que vamos fazer?", { exact: true }).fill(request);
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Templates para este pedido",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  const confirmation = page.getByRole("dialog", {
    name: "Revisar sem anexos?",
  });
  await expect(confirmation.getByRole("checkbox")).not.toBeChecked();
  await confirmation.getByRole("checkbox").check();
  await confirmation
    .getByRole("button", { name: "Confirmar e revisar", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Revise antes de construir",
      exact: true,
    }),
  ).toBeVisible();
}

export async function showBuildHistory(page: Page) {
  const conversation = page.getByRole("region", {
    name: "Conversa de construção",
    exact: true,
  });
  const history = page.locator("details.guided-history");
  await expect
    .poll(
      async () =>
        (await conversation.isVisible()) || (await history.isVisible()),
    )
    .toBe(true);
  if (await conversation.isVisible()) return;
  await expect(history).toBeVisible();
  if (
    !(await history.evaluate((element) => (element as HTMLDetailsElement).open))
  )
    await history.locator(":scope > summary").click();
}
