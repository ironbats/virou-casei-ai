import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { productionPolicy } from "./productionPolicy";

test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Use backend/script/test-workspace.py para iniciar o laboratório de construção.",
);

import {
  projectAction,
  editorAction,
  reviewRequest,
  showBuildHistory,
  workspace,
} from "./workspaceHelper";

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

test("pedido, progresso real, aplicação revisada e histórico após recarga", async ({
  page,
}, info) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(
    page,
    "Melhorar o título e o conteúdo da página inicial",
  );
  await expect(turn).toHaveAttribute("data-state", "running");
  await expect(turn).toContainText("Construindo as alterações solicitadas.");
  await page.screenshot({
    path: info.outputPath("construction-running.png"),
    fullPage: true,
  });
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  await expect(page.locator(".code-editor")).toHaveValue(original);
  await apply(page, turn);
  await expect(turn).toContainText("Melhorei a página inicial");
  await expect(page.locator(".code-editor")).not.toHaveValue(original);
  await expect(turn).toContainText("Estrutura dos arquivos conferida.");
  await expect(page.locator("body")).not.toContainText(/gpt-|gemini|openai/i);
  await page.reload();
  await showBuildHistory(page);
  await expect(page.locator(".build-turn").last()).toContainText(
    "Melhorar o título e o conteúdo da página inicial",
  );
  await editorAction(page, "Usar texto simples");
  await expect(page.locator(".code-editor")).toHaveValue(/Alteração E2E/);
  await page.screenshot({
    path: info.outputPath("construction-complete.png"),
    fullPage: true,
  });
  const audit = await new AxeBuilder({ page })
    .include(".ide-layout")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(audit.violations).toEqual([]);
});

test("edição em outra sessão durante construção preserva rascunho e exige comparação", async ({
  page,
}) => {
  const project = await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(
    page,
    "Adicionar uma melhoria preservando edição local",
  );
  await expect(turn).toHaveAttribute("data-state", "running");
  await expect(page.locator(".code-editor")).toHaveAttribute("readonly", "");
  await page.evaluate(
    async ({ pid, original }) => {
      const me = await (await fetch("/api/v1/me")).json();
      const draft = await (await fetch(`/api/v1/projects/${pid}/draft`)).json();
      const revision = await (
        await fetch(
          `/api/v1/projects/${pid}/revisions/${draft.data.base_revision_id}`,
        )
      ).json();
      const entries = await Promise.all(
        revision.files.map(async (file: { id: string }) =>
          (
            await fetch(
              `/api/v1/projects/${pid}/revisions/${draft.data.base_revision_id}/files/${file.id}`,
            )
          ).json(),
        ),
      );
      const files = Object.fromEntries(
        entries.map((file: { path: string; content: string }) => [
          file.path,
          file.content,
        ]),
      );
      files["public/index.html"] = original.replace(
        "</body>",
        "<p>Minha edição local não pode ser perdida</p></body>",
      );
      const response = await fetch(`/api/v1/projects/${pid}/draft`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": me.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": String(draft.version),
        },
        body: JSON.stringify({
          base_revision_id: draft.data.base_revision_id,
          files,
          prompt: draft.data.prompt,
        }),
      });
      if (!response.ok) throw Error(await response.text());
    },
    { pid: project.id, original },
  );
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  await expect(turn).toContainText("mudou durante a geração");
  await page.reload();
  await editorAction(page, "Usar texto simples");
  await expect(page.locator(".code-editor")).toHaveValue(
    /Minha edição local não pode ser perdida/,
  );
  await showBuildHistory(page);
  await page
    .locator(".build-turn")
    .last()
    .getByRole("button", { name: "Revisar alterações" })
    .click();
  await expect(
    page.getByRole("button", { name: "Aplicar alterações revisadas" }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Fechar revisão", exact: true })
    .click();
});

test("resposta truncada preserva revisão e permite reutilizar pedido", async ({
  page,
}) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(
    page,
    "SIMULAR_TRUNCADO para validar recuperação do pedido",
  );
  await expect(turn).toHaveAttribute("data-state", "failed", {
    timeout: 15000,
  });
  await expect(turn).toContainText("limite de tamanho");
  await expect(page.locator(".code-editor")).toHaveValue(original);
  page.once("dialog", (d) => d.accept());
  await turn.getByRole("button", { name: "Reutilizar pedido" }).click();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("SIMULAR_TRUNCADO para validar recuperação do pedido");
  await expect(
    page.getByRole("button", { name: "Preparar construção", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Enviar para construir", exact: true }),
  ).toHaveCount(0);
});

test("cancelamento durante construção não aplica os arquivos", async ({
  page,
}) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(
    page,
    "Construir página para validar cancelamento em andamento",
  );
  await expect(turn).toHaveAttribute("data-state", "running");
  await turn.getByRole("button", { name: "Cancelar construção" }).click();
  await expect(turn).toHaveAttribute("data-state", "cancelled");
  await expect(turn).toContainText("não foi aplicada");
  await expect(page.locator(".code-editor")).toHaveValue(original);
});

test("reconecta sem repetir a geração após falha de acompanhamento", async ({
  page,
}) => {
  await workspace(page);
  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/messages"))
      posts += 1;
  });
  const turn = await send(
    page,
    "Atualizar página para validar reconexão sem duplicar operação",
  );
  await page.route("**/api/v1/operations?**", (route) => route.abort("failed"));
  await expect(
    page.getByRole("alert").filter({ hasText: "último estado foi preservado" }),
  ).toBeVisible();
  await expect(turn.locator(".build-request")).toContainText(
    "validar reconexão",
  );
  await page.unroute("**/api/v1/operations?**");
  await page.getByRole("button", { name: "Reconectar", exact: true }).click();
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  expect(posts).toBe(1);
});

test("conversa acessível em mobile, tablet e desktop sem overflow", async ({
  page,
}, info) => {
  await workspace(page);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 900)
      await page.getByRole("tab", { name: "Chat", exact: true }).click();
    await expect(page.locator(".guided-build")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollHeight <=
          Math.max(
            innerHeight,
            document.querySelector("#main")!.getBoundingClientRect().bottom +
              scrollY +
              2,
          ),
      ),
    ).toBe(true);
    if (width === 375 || width === 1440)
      await page.screenshot({
        path: info.outputPath(`workspace-${width}.png`),
        fullPage: true,
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

test("acompanha até concluir uma operação fora da primeira página", async ({
  page,
}) => {
  await workspace(page);
  await page.route("**/api/v1/operations?**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.has("state")) return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    body.items = body.items.filter(
      (item: { request_text?: string }) =>
        !item.request_text?.includes("FORA_DA_PAGINA"),
    );
    await route.fulfill({ response, json: body });
  });
  const turn = await send(
    page,
    "FORA_DA_PAGINA melhorar a experiência da página inicial",
  );
  await expect(turn).toHaveAttribute("data-state", "running");
  await expect(turn).toHaveAttribute("data-state", "succeeded");
});

test("recupera corte confirmado e aplica código completo", async ({ page }) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(
    page,
    "RECUPERAR_CORTE implementar a melhoria completa",
  );
  await expect(turn).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  await expect(page.locator(".code-editor")).toHaveValue(original);
  await apply(page, turn);
  await expect(page.locator(".code-editor")).toHaveValue(/Alteração E2E/);
});

test("guia SDD, edição manual, recarga e transferência sem gerar IA", async ({
  page,
  context,
}, info) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const project = await workspace(page);
  let generations = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/messages")) generations++;
  });
  await projectAction(page, "Explorar guias");
  await expect(page.locator(".guide-card")).toHaveCount(14);
  await page.getByRole("tab", { name: "2 Adapte" }).click();
  await page
    .getByLabel("Objetivo do projeto")
    .fill("Criar um organizador de tarefas da equipe");
  await page.getByLabel("Para quem é o projeto?").fill("Equipe de operações");
  await page
    .getByLabel("Critérios de aceite")
    .fill("Cadastrar tarefa, concluir tarefa e ver a lista atualizada.");
  await page.getByRole("button", { name: "Revisar e aplicar" }).click();
  await page
    .getByLabel("Revise e edite o texto")
    .fill(
      "Pedido manual: implemente tarefas completas e preserve o histórico.",
    );
  await page.getByRole("tab", { name: "2 Adapte" }).click();
  await page
    .getByLabel("Objetivo do projeto")
    .fill("Objetivo atualizado sem apagar meu prompt");
  await page.getByRole("tab", { name: "3 Aplique" }).click();
  await expect(page.getByLabel("Revise e edite o texto")).toHaveValue(
    "Pedido manual: implemente tarefas completas e preserve o histórico.",
  );
  await expect(page.getByText("Rascunho salvo", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("tab", { name: "3 Aplique" }).click();
  await page.getByRole("button", { name: "Copiar template" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "Pedido manual: implemente tarefas completas e preserve o histórico.",
  );
  await page.screenshot({
    path: info.outputPath("learning-apply-desktop.png"),
    fullPage: true,
  });
  expect(
    (
      await new AxeBuilder({ page })
        .include(".learning-workspace")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page
    .getByRole("button", { name: "Levar rascunho para o projeto" })
    .click();
  await expect(page).toHaveURL(new RegExp(`/app/projetos/${project.id}`));
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("");
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .fill("Implementar o organizador de tarefas com o guia revisado");
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(
    page.getByText("1 template selecionado", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.locator(".attachment-card")).toHaveCount(1);
  await page.getByRole("button", { name: "Abrir", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Pedido manual: implemente tarefas completas e preserve o histórico.",
  );
  await page.getByRole("button", { name: "Fechar", exact: true }).click();
  expect(generations).toBe(0);
  await projectAction(page, "Explorar guias");
  await page.getByRole("tab", { name: "3 Aplique" }).click();
  await page
    .getByRole("button", { name: "Levar rascunho para o projeto" })
    .click();
  await expect(
    page.getByText("1 template selecionado", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.locator(".attachment-card")).toHaveCount(1);
  await expect(page.locator(".guided-objective")).toHaveText(
    "Implementar o organizador de tarefas com o guia revisado",
  );
  expect(generations).toBe(0);
});

test("conflito de rascunho preserva as duas versões e permite comparação", async ({
  page,
}) => {
  const project = await workspace(page);
  await page.evaluate(async (pid) => {
    const me = await (await fetch("/api/v1/me")).json();
    const draft = await (
      await fetch(`/api/v1/projects/${pid}/build-draft`)
    ).json();
    const r = await fetch(`/api/v1/projects/${pid}/build-draft`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
        "Idempotency-Key": crypto.randomUUID(),
        "If-Match": String(draft.version),
      },
      body: JSON.stringify({
        ...draft.data,
        message_text: "Texto salvo em outra sessão",
      }),
    });
    if (!r.ok) throw new Error(await r.text());
  }, project.id);
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .fill("Meu texto nesta sessão deve ser preservado");
  await expect(page.getByRole("dialog")).toContainText(
    "Texto salvo em outra sessão",
  );
  await expect(page.getByRole("dialog")).toContainText(
    "Meu texto nesta sessão deve ser preservado",
  );
  await page.getByRole("button", { name: "Reaplicar meu rascunho" }).click();
  await expect(
    page.locator(".guided-build").getByText("Rascunho salvo", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("Meu texto nesta sessão deve ser preservado");
});

test("Aprender em mobile preserva filtros e rascunho", async ({
  page,
}, info) => {
  await workspace(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/app/aprendizado");
  await page.getByLabel("Buscar guias").fill("SDD");
  await expect(page.locator(".guide-card")).toHaveCount(1);
  await page.getByRole("link", { name: "Ver guia" }).click();
  await page.getByRole("tab", { name: "2 Adapte" }).click();
  await expect(page.getByLabel("Objetivo do projeto")).toHaveValue(
    "Objetivo atualizado sem apagar meu prompt",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("learning-mobile.png"),
    fullPage: true,
  });
  expect(
    (
      await new AxeBuilder({ page })
        .include(".learning-workspace")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.getByRole("link", { name: "Voltar aos guias" }).click();
  await expect(page.getByLabel("Buscar guias")).toHaveValue("SDD");
});

test("editor com destaque, abas e teclado preserva conteúdo", async ({
  page,
}, info) => {
  await workspace(page);
  await editorAction(page, "Editor com destaque");
  await expect(page.locator(".cm-lineNumbers")).toBeVisible();
  await page.getByRole("treeitem", { name: "app.js", exact: true }).click();
  await expect(page.locator(".cm-content")).toHaveAttribute(
    "aria-label",
    "Conteúdo de public/app.js",
  );
  const before = await page.locator(".cm-content").innerText();
  await page.locator(".cm-content").click();
  await page.keyboard.press("Control+End");
  await page.keyboard.insertText("\n// Edição preservada no rascunho");
  await page.getByRole("treeitem", { name: "index.html", exact: true }).click();
  await page.getByRole("tab", { name: /app.js/ }).click();
  await expect(page.locator(".cm-content")).toContainText(
    "Edição preservada no rascunho",
  );
  await page.locator(".cm-content").click();
  await page.keyboard.press("Escape");
  await page.keyboard.press("Tab");
  await expect(page.locator(".cm-content")).not.toBeFocused();
  expect(
    (
      await new AxeBuilder({ page })
        .include(".ide-layout")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: info.outputPath("editor-highlighted.png"),
    fullPage: true,
  });
  await editorAction(page, "Usar texto simples");
  await page.locator(".code-editor").fill(before);
  await expect(
    page.getByText("Rascunho salvo", { exact: false }).first(),
  ).toBeVisible();
});

test("pedido acima do limite é preservado e não dispara geração", async ({
  page,
}) => {
  await workspace(page);
  const text = "x".repeat(12001);
  let posts = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/messages")) posts++;
  });
  await page.getByLabel("O que vamos fazer?", { exact: true }).fill(text);
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue(text);
  await expect(
    page.getByRole("button", { name: "Preparar construção", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .press("Control+Enter");
  expect(posts).toBe(0);
  await page.getByLabel("O que vamos fazer?", { exact: true }).fill("");
  await expect(
    page.getByText("Rascunho salvo", { exact: false }).first(),
  ).toBeVisible();
});

test("pergunta de esclarecimento, resposta curta e aplicação com contexto", async ({
  page,
}) => {
  const project = await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const question = await send(page, "CONFIRMAR_OBJETIVO aprimorar a homepage");
  await expect(question).toContainText("Aguardando sua resposta");
  await expect(question).toContainText("Posso priorizar");
  await expect(
    question.getByRole("link", { name: "Ver revisão no histórico" }),
  ).toHaveCount(0);
  await expect(page.locator(".code-editor")).toHaveValue(original);
  await page.reload();
  await expect(
    page.getByLabel("Sua resposta ao agente", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Sua resposta ao agente", { exact: true }).fill("sim");
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  const answered = page.locator(".build-turn").last();
  await expect(answered.locator(".build-request")).toContainText("sim");
  await expect(answered).toHaveAttribute("data-state", "succeeded");
  await apply(page, answered);
  await editorAction(page, "Usar texto simples");
  await expect(page.locator(".code-editor")).not.toHaveValue(original);
  expect(
    await page.evaluate(
      async (pid) =>
        (await (await fetch(`/api/v1/projects/${pid}`)).json())
          .current_revision_id,
      project.id,
    ),
  ).not.toBe(project.current_revision_id);
});

test("corrige resposta inválida e preserva o próximo pedido digitado durante a geração", async ({
  page,
}) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  const turn = await send(page, "RECUPERAR_FORMATO melhorar a homepage");
  await expect(turn).toHaveAttribute("data-state", "running");
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  await page
    .getByLabel("O que vamos fazer?", { exact: true })
    .fill("Meu próximo pedido ainda não deve ser enviado");
  await expect(turn).toContainText("Ajustando a resposta recebida", {
    timeout: 12000,
  });
  await expect(turn).toHaveAttribute("data-state", "succeeded", {
    timeout: 15000,
  });
  await expect(page.locator(".code-editor")).toHaveValue(original);
  await showBuildHistory(page);
  await apply(page, turn);
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("Meu próximo pedido ainda não deve ser enviado");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue("Meu próximo pedido ainda não deve ser enviado");
});

test("salva edição manual explicitamente antes da preparação e exige aplicação da proposta", async ({
  page,
}) => {
  await workspace(page);
  const editor = page.locator(".code-editor");
  const source = await editor.inputValue();
  await editor.fill(
    source.replace("</body>", "<p>Conteúdo manual preservado</p></body>"),
  );
  await page
    .getByRole("button", { name: "Salvar revisão do projeto", exact: true })
    .click();
  const turn = await send(
    page,
    "Aprimorar a página preservando o conteúdo manual",
  );
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  await apply(page, turn);
  await expect(editor).toHaveValue(/Conteúdo manual preservado/);
  await expect(editor).toHaveValue(/Alteração E2E/);
  await expect(
    page.getByRole("button", { name: "Salvar revisão", exact: true }),
  ).toBeDisabled();
});

test("retoma a atualização da IDE após falha transitória na leitura da nova revisão", async ({
  page,
}) => {
  await workspace(page);
  const original = await page.locator(".code-editor").inputValue();
  let failures = 0;
  await page.route("**/api/v1/projects/*/revisions/**", async (route) => {
    if (failures === 0) {
      failures++;
      return route.abort("failed");
    }
    return route.continue();
  });
  const turn = await send(
    page,
    "Melhorar homepage e recuperar sincronização da IDE",
  );
  await expect(turn).toHaveAttribute("data-state", "succeeded");
  await apply(page, turn);
  await expect(page.locator(".code-editor")).not.toHaveValue(original, {
    timeout: 20000,
  });
  await expect(page.locator(".code-editor")).toHaveValue(/Alteração E2E/);
  expect(failures).toBe(1);
});

test("resultado incerto bloqueia reenvio e persiste após recarga", async ({
  page,
}) => {
  await workspace(page);
  const turn = await send(
    page,
    "SIMULAR_INCERTO para validar resultado em apuração",
  );
  await expect(turn).toHaveAttribute("data-state", "reconciling");
  await expect(turn).toContainText("resultado está em apuração");
  await expect(
    turn.getByRole("button", { name: "Reutilizar pedido" }),
  ).toHaveCount(0);
  await page.reload();
  await showBuildHistory(page);
  await expect(page.locator(".build-turn").last()).toHaveAttribute(
    "data-state",
    "reconciling",
  );
  await reviewRequest(page, "Novo pedido que precisa aguardar a apuração");
  await expect(
    page.getByRole("button", { name: "Enviar para construir", exact: true }),
  ).toBeDisabled();
});
