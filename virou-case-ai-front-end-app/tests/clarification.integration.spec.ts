import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Operation, Project } from "../src/shared/types";
import { productionPolicy } from "./productionPolicy";
import { reviewRequest, showBuildHistory, workspace } from "./workspaceHelper";

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
      ),
    };
  }, pid);
}

async function send(page: Page, request: string) {
  await reviewRequest(page, request);
  const response = page.waitForResponse(
    (value) =>
      value.request().method() === "POST" && value.url().endsWith("/messages"),
  );
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  return accepted.json() as Promise<Operation>;
}

async function reply(page: Page, text: string) {
  const response = page.waitForResponse(
    (value) =>
      value.request().method() === "POST" &&
      /\/messages\/[^/]+\/reply$/.test(value.url()),
  );
  await page.getByLabel("Sua resposta ao agente", { exact: true }).fill(text);
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  return accepted.json() as Promise<Operation>;
}

async function applyLastProposal(page: Page) {
  await page
    .locator(".build-turn")
    .last()
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}

async function currentOperation(page: Page, id: string) {
  const response = await page.request.get(`/api/v1/operations/${id}`);
  expect(response.ok()).toBe(true);
  return response.json() as Promise<Operation>;
}

test("Responder mantém a pergunta no chat e abre a resposta sem refazer as etapas", async ({
  page,
}, info) => {
  const project = await workspace(page);
  const original = await savedProject(page, project.id);
  const request =
    "CONFIRMAR_OBJETIVO aprimorar a homepage preservando o contexto";
  const operation = await send(page, request);
  const question = page.locator(".build-turn").last();
  await expect(question).toContainText("Aguardando sua resposta");
  await expect(question).toContainText("Posso priorizar");
  await question
    .getByRole("button", { name: "Responder", exact: true })
    .click();
  await page.screenshot({
    path: info.outputPath("clarification-answer-open.png"),
  });
  await expect(question).toBeVisible();
  const answer = page.getByLabel("Sua resposta ao agente", { exact: true });
  await expect(answer).toBeFocused();
  await expect(answer).toBeEditable();
  await expect(
    page.getByRole("button", { name: "Enviar resposta", exact: true }),
  ).toBeDisabled();
  await answer.fill("sim");
  await question
    .getByRole("button", { name: "Responder", exact: true })
    .click();
  await expect(answer).toBeFocused();
  await expect(answer).toHaveValue("sim");
  const replies: { body: Record<string, unknown>; url: string }[] = [];
  page.on("request", (value) => {
    if (value.method() === "POST" && /\/reply$/.test(value.url()))
      replies.push({ body: value.postDataJSON(), url: value.url() });
  });
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .evaluate((element) => {
      (element as HTMLButtonElement).click();
      (element as HTMLButtonElement).click();
    });
  await expect(page.locator(".build-turn").last()).toContainText("sim");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
  );
  expect(replies).toHaveLength(1);
  expect(replies[0].url).toContain(`/messages/${operation.id}/reply`);
  expect(replies[0].body).toEqual({
    message_text: "sim",
    base_project_revision: original.project.current_revision_id,
    policy_version: "2026-10-v1",
  });
  expect(await savedProject(page, project.id)).toEqual(original);
  await applyLastProposal(page);
  expect(
    (await savedProject(page, project.id)).files["public/index.html"],
  ).not.toBe(original.files["public/index.html"]);
});

for (const recoverTruncation of [false, true]) {
  test(
    recoverTruncation
      ? "recupera dois cortes ao responder preservando pedido, decisões e arquivos parciais"
      : "retoma duas perguntas após recarga com pedido, anexos e arquivos parciais integrais",
    async ({ page }, info) => {
      test.setTimeout(90000);
      const project = await workspace(page);
      const original = await savedProject(page, project.id);
      const request = `ESCLARECER_PARCIAL ${recoverTruncation ? "RECUPERAR_DOIS_CORTES " : ""}Criar carrinho autenticado preservando todas as decisões da padaria.`;
      const attachment =
        "ANEXO_ESCLARECIMENTO_INTEGRAL: manter a autenticação, os preços e as permissões já definidos pelo usuário.";
      await page
        .getByLabel("O que vamos fazer?", { exact: true })
        .fill(request);
      await page
        .getByRole("button", { name: "Preparar construção", exact: true })
        .click();
      await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
      const explore = page.getByRole("button", {
        name: "Explorar catálogo",
        exact: true,
      });
      if (await explore.count()) await explore.click();
      const card = page
        .locator(".template-card")
        .filter({ hasText: "SDD: da ideia à especificação" });
      await card.getByRole("checkbox").check();
      await expect(card.locator(".template-fill-state")).toHaveText(
        "Preenchido pela IA",
        { timeout: 30000 },
      );
      await card
        .getByRole("button", { name: "Editar conteúdo", exact: true })
        .click();
      const editor = page.getByRole("dialog");
      await editor
        .getByLabel("Conteúdo que será enviado", { exact: true })
        .fill(attachment);
      await editor
        .getByRole("button", { name: "Salvar edição", exact: true })
        .click();
      await expect(editor).toHaveCount(0);
      await page
        .getByRole("button", { name: "Revisar pedido", exact: true })
        .click();
      const firstAccepted = page.waitForResponse(
        (value) =>
          value.request().method() === "POST" &&
          value.url().endsWith("/messages"),
      );
      await page
        .getByRole("button", { name: "Enviar para construir", exact: true })
        .click();
      const first = (await (await firstAccepted).json()) as Operation;
      await expect(page.locator(".build-turn").last()).toContainText(
        "Como confirmar a sessão",
        { timeout: 15000 },
      );
      expect(await savedProject(page, project.id)).toEqual(original);
      const firstState = await currentOperation(page, first.id);
      expect(firstState.attachment_snapshots?.[0].content).toBe(attachment);
      await page
        .getByLabel("Sua resposta ao agente", { exact: true })
        .fill("Opção 2");
      await page.reload();
      await expect(
        page.getByLabel("Sua resposta ao agente", { exact: true }),
      ).toHaveValue("Opção 2");
      await expect(
        page.getByRole("region", {
          name: "Conversa de construção",
          exact: true,
        }),
      ).toBeVisible();
      await expect(page.locator(".build-turn").last()).toContainText(request);
      const second = await reply(page, "Opção 2");
      await expect(page.locator(".build-turn").last()).toContainText(
        "Posso manter a sessão confirmada por 30 minutos?",
        { timeout: 15000 },
      );
      expect(await savedProject(page, project.id)).toEqual(original);
      await page.reload();
      await expect(
        page.getByLabel("Sua resposta ao agente", { exact: true }),
      ).toHaveValue("");
      const third = await reply(page, "sim");
      await expect(page.locator(".build-turn").last()).toHaveAttribute(
        "data-state",
        "succeeded",
        { timeout: 30000 },
      );
      const completed = await currentOperation(page, third.id);
      expect(completed.error_code).toBeNull();
      expect(
        completed.activity?.filter((event) => event.stage === "resizing_code"),
      ).toHaveLength(recoverTruncation ? 2 : 0);
      expect(new Set([first.id, second.id, third.id]).size).toBe(3);
      expect(await savedProject(page, project.id)).toEqual(original);
      await page
        .locator(".build-turn")
        .last()
        .getByRole("button", { name: "Revisar alterações", exact: true })
        .click();
      await expect(page.getByRole("dialog")).toContainText(
        "public/carrinho-parcial.js",
      );
      await page
        .getByRole("button", {
          name: "Aplicar alterações revisadas",
          exact: true,
        })
        .click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      const applied = await savedProject(page, project.id);
      expect(applied.files["public/carrinho-parcial.js"]).toContain(
        "preparado antes da pergunta",
      );
      expect(applied.files["public/index.html"]).toContain(
        "Carrinho confirmado",
      );
      const evidence = JSON.parse(
        applied.files["public/confirmacao-contexto.json"],
      );
      const context = JSON.stringify(evidence);
      expect(context).toContain(request);
      expect(context).toContain(attachment);
      expect(context).toContain("Opção 2");
      expect(context).toContain("Como confirmar a sessão");
      expect(context).toContain("Posso manter a sessão confirmada");
      expect(evidence.request).toContain("sim");
      expect(evidence.partial).toBe(
        applied.files["public/carrinho-parcial.js"],
      );
      for (const [path, content] of Object.entries(original.files))
        if (path !== "public/index.html")
          expect(applied.files[path]).toBe(content);
      await page.screenshot({
        path: info.outputPath("clarification-context-complete.png"),
      });
    },
  );
}

test("falha recuperável preserva a resposta e uma repetição usa a mesma intenção", async ({
  page,
}) => {
  const project = await workspace(page);
  await send(
    page,
    "CONFIRMAR_OBJETIVO validar recuperação do envio da resposta",
  );
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toBeVisible();
  const original = await savedProject(page, project.id);
  const keys: string[] = [];
  const bodies: unknown[] = [];
  await page.route("**/api/v1/projects/*/messages/*/reply", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    bodies.push(route.request().postDataJSON());
    if (keys.length === 1) {
      await route.fulfill({
        status: 503,
        json: {
          error: {
            code: "UNAVAILABLE",
            message: "Falha temporária sintética ao enviar a resposta.",
          },
        },
      });
    } else {
      await route.continue();
    }
  });
  await page.getByLabel("Sua resposta ao agente", { exact: true }).fill("sim");
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Falha temporária sintética",
  );
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toHaveValue("sim");
  expect(await savedProject(page, project.id)).toEqual(original);
  await page
    .getByRole("button", { name: "Verificar envio", exact: true })
    .click();
  await expect(
    page.locator(".build-turn").last().locator(".build-request"),
  ).toContainText("sim");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
  );
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
  expect(bodies[1]).toEqual(bodies[0]);
  expect(await savedProject(page, project.id)).toEqual(original);
});

test("chat acompanha a construção e respeita a leitura manual do histórico", async ({
  page,
}, info) => {
  await workspace(page);
  await page.setViewportSize({ width: 1440, height: 800 });
  const request =
    "CONSTRUIR_DEZ_ETAPAS Acompanhar a construção sem perder a leitura anterior. " +
    "Preservar todas as etapas do pedido. ".repeat(30);
  await send(page, request);
  const scroller = page.getByRole("region", {
    name: "Conversa de construção",
    exact: true,
  });
  const gap = () =>
    scroller.evaluate(
      (element) =>
        element.scrollHeight - element.clientHeight - element.scrollTop,
    );
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "running",
  );
  await expect.poll(gap).toBeLessThan(8);
  await expect
    .poll(() =>
      scroller.evaluate(
        (element) => element.scrollHeight - element.clientHeight,
      ),
    )
    .toBeGreaterThan(180);
  await scroller.hover();
  await page.mouse.wheel(0, -600);
  await expect.poll(gap).toBeGreaterThan(160);
  const held = await scroller.evaluate((element) => element.scrollTop);
  await expect(
    page.locator(".build-turn").last().getByLabel("Arquivos em construção"),
  ).toContainText("public/construcao-etapa-05.js", { timeout: 15000 });
  expect(
    Math.abs((await scroller.evaluate((element) => element.scrollTop)) - held),
  ).toBeLessThan(8);
  const latest = page.getByRole("button", {
    name: "Ir para a última mensagem",
    exact: true,
  });
  await expect(latest).toBeVisible();
  await latest.click();
  await expect.poll(gap).toBeLessThan(8);
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
    { timeout: 15000 },
  );
  await expect.poll(gap).toBeLessThan(8);
  await page.screenshot({
    path: info.outputPath("conversation-scroll-latest.png"),
  });
});

test("resposta aceita com confirmação de rede perdida é recuperada sem nova geração", async ({
  page,
}) => {
  const project = await workspace(page);
  await send(
    page,
    "CONFIRMAR_OBJETIVO recuperar o aceite após perder a resposta HTTP",
  );
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toBeVisible();
  const original = await savedProject(page, project.id);
  let accepted: Operation | undefined;
  let posts = 0;
  await page.route("**/api/v1/projects/*/messages/*/reply", async (route) => {
    posts++;
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    accepted = (await response.json()) as Operation;
    await route.abort("connectionreset");
  });
  await page.getByLabel("Sua resposta ao agente", { exact: true }).fill("sim");
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  await expect(
    page.locator(".build-turn").last().locator(".build-request"),
  ).toContainText("sim");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
  );
  expect(posts).toBe(1);
  expect(accepted?.id).toBeTruthy();
  expect((await currentOperation(page, accepted!.id)).state).toBe("succeeded");
  expect(await savedProject(page, project.id)).toEqual(original);
  await page.reload();
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.locator(".build-turn").last().locator(".build-request"),
  ).toContainText("sim");
  expect(posts).toBe(1);
});

test("resposta acessível em mobile e desktop mantém texto, contexto e largura", async ({
  page,
}, info) => {
  await workspace(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await send(
    page,
    "CONFIRMAR_OBJETIVO confirmar apresentação e ações acessíveis",
  );
  const answer = page.getByLabel("Sua resposta ao agente", { exact: true });
  await expect(answer).toBeVisible();
  await answer.fill("Sim, preservar os comportamentos existentes.");
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 900)
      await page.getByRole("tab", { name: "Chat", exact: true }).click();
    await expect(answer).toBeVisible();
    await expect(answer).toHaveValue(
      "Sim, preservar os comportamentos existentes.",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const submit = page.getByRole("button", {
      name: "Enviar resposta",
      exact: true,
    });
    await page
      .locator(".build-turn")
      .last()
      .getByRole("button", { name: "Responder", exact: true })
      .click();
    await expect(answer).toBeFocused();
    await expect(submit).toBeInViewport({ ratio: 0.95 });
    if (width === 375 || width === 1440)
      await page.screenshot({
        path: info.outputPath(`clarification-responsive-${width}.png`),
      });
  }
  await page.setViewportSize({ width: 375, height: 900 });
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  const audit = await new AxeBuilder({ page })
    .include(".ide-layout")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(audit.violations).toEqual([]);
});

test("pergunta recebida durante novo rascunho preserva o pedido e Responder retoma a conversa", async ({
  page,
}) => {
  const project = await workspace(page);
  const original = await savedProject(page, project.id);
  await send(
    page,
    "CONFIRMAR_OBJETIVO preservar o próximo pedido enquanto aguarda o agente",
  );
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "running",
  );
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  const next =
    "Meu próximo pedido independente deve permanecer integral e não será a resposta do agente.";
  const idea = page.getByLabel("O que vamos fazer?", { exact: true });
  await idea.fill(next);
  await expect(page.locator(".build-turn").last()).toContainText(
    "Aguardando sua resposta",
    { timeout: 15000 },
  );
  await expect(idea).toBeVisible();
  await expect(idea).toHaveValue(next);
  await showBuildHistory(page);
  await page
    .locator(".build-turn")
    .last()
    .getByRole("button", { name: "Responder", exact: true })
    .click();
  const answer = page.getByLabel("Sua resposta ao agente", { exact: true });
  await expect(answer).toBeFocused();
  await expect(answer).toHaveValue("");
  await reply(page, "sim");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
  );
  expect(await savedProject(page, project.id)).toEqual(original);
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  await expect(idea).toHaveValue(next);
  await page.reload();
  await expect(idea).toHaveValue(next);
});

test("continuação com falha permite responder novamente à mesma pergunta sem perder o contexto", async ({
  page,
}) => {
  const project = await workspace(page);
  const original = await savedProject(page, project.id);
  const question = await send(
    page,
    "CONFIRMAR_OBJETIVO preservar o vínculo quando a continuação falhar",
  );
  await expect(
    page.locator(".build-turn").last().locator(".build-request"),
  ).toContainText(
    "CONFIRMAR_OBJETIVO preservar o vínculo quando a continuação falhar",
  );
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-kind",
    "clarification",
    { timeout: 15000 },
  );
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toBeVisible();
  const failed = await reply(
    page,
    "SIMULAR_TRUNCADO confirmar a primeira opção",
  );
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "failed",
    { timeout: 15000 },
  );
  expect(await savedProject(page, project.id)).toEqual(original);
  await page
    .locator(".build-turn")
    .last()
    .getByRole("button", { name: "Tentar responder novamente", exact: true })
    .click();
  const answer = page.getByLabel("Sua resposta ao agente", { exact: true });
  await expect(answer).toBeFocused();
  await expect(answer).toHaveValue(
    "SIMULAR_TRUNCADO confirmar a primeira opção",
  );
  const retried = await reply(page, "sim");
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "succeeded",
  );
  expect(retried.id).not.toBe(failed.id);
  expect(retried.reply_to).toBe(question.id);
  expect((await currentOperation(page, failed.id)).state).toBe("failed");
  expect(await savedProject(page, project.id)).toEqual(original);
  await applyLastProposal(page);
  expect(
    (await savedProject(page, project.id)).files["public/index.html"],
  ).not.toBe(original.files["public/index.html"]);
});
