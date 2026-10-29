import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Attachment, BuildDraft } from "../src/features/guidedTypes";
import { mergeBuildDraft } from "../src/features/mergeBuildDraft";
import { fieldProblem, renderTemplate } from "../src/features/templateFields";
import {
  authTemplate,
  preparationWorkspace,
} from "./templatePreparationFixture";

test.use({ viewport: { width: 1660, height: 950 } });

const prepare = async (page: Page) => {
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
};
const authCard = (page: Page) =>
  page.locator(".template-card").filter({ hasText: authTemplate.title });

async function selectAuth(page: Page) {
  await authCard(page).getByRole("checkbox").check();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
  );
}

async function reviewEmpty(page: Page) {
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Confirmar e revisar", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
}

test("recomendação não seleciona, seleção preenche automaticamente, revisão congela e próximo pedido começa em Ideia", async ({
  page,
}, info) => {
  const { state } = await preparationWorkspace(page);
  expect(state.fills).toHaveLength(0);
  await expect(
    page.getByRole("button", { name: "Revisar sem templates", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("O que vamos fazer?").press("Control+Enter");
  expect(state.submitted).toHaveLength(0);
  await expect(page.locator(".cm-content")).toBeVisible();
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await page.screenshot({
    path: info.outputPath("01-ideia.png"),
    fullPage: true,
  });
  await prepare(page);
  await expect(authCard(page).getByRole("checkbox")).not.toBeChecked();
  expect(state.fills).toHaveLength(0);
  await expect(
    page.getByRole("button", {
      name: "Preparar templates com IA",
      exact: true,
    }),
  ).toHaveCount(0);
  await selectAuth(page);
  expect(state.fills).toHaveLength(1);
  await authCard(page)
    .getByRole("button", { name: `Abrir ${authTemplate.title}`, exact: true })
    .click();
  await expect(authCard(page)).toContainText("Origem: seu pedido");
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await page.screenshot({
    path: info.outputPath("02-templates.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page
    .locator(".attachment-card")
    .getByRole("button", { name: "Abrir", exact: true })
    .click();
  const content = await page.getByRole("dialog").locator("pre").innerText();
  expect(content).toBe(
    state.draft.review_snapshot?.attachments[0].edited_content,
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Fechar", exact: true })
    .click();
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await page.screenshot({
    path: info.outputPath("03-revisao.png"),
    fullPage: true,
  });
  await page.reload();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  const hash = state.draft.review_snapshot?.hash;
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  await expect(page.getByText("Etapa 1 de 3")).toBeVisible();
  expect(state.submitted).toHaveLength(1);
  expect(
    state.requests.find((request) => request.path.endsWith("/messages"))?.body
      .review_snapshot_hash,
  ).toBe(hash);
  expect(
    state.submitted[0].review_snapshot?.attachments[0].edited_content,
  ).toBe(content);
  await expect(page.getByLabel("O que vamos fazer?")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Enviar para construir", exact: true }),
  ).toHaveCount(0);
  expect(state.fills).toHaveLength(1);
});

test("zero anexos exige confirmação desmarcada e persistida; cancelar não conclui Templates", async ({
  page,
}, info) => {
  const { state } = await preparationWorkspace(page);
  await prepare(page);
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(
    page.getByRole("dialog").getByRole("checkbox"),
  ).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "Confirmar e revisar", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Cancelar", exact: true }).click();
  expect(
    state.requests.filter((request) => request.path.endsWith("/build-review")),
  ).toHaveLength(0);
  await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
  await reviewEmpty(page);
  expect(state.draft.review_snapshot?.empty_selection_confirmed).toBe(true);
  expect(state.draft.review_snapshot?.attachments).toEqual([]);
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await page.screenshot({
    path: info.outputPath("04-vazio-confirmado.png"),
    fullPage: true,
  });
  expect(state.fills).toHaveLength(0);
});

test("nova seleção preenche somente novo anexo e remover durante chamada impede aplicação tardia", async ({
  page,
}) => {
  const { state, ready } = await preparationWorkspace(page);
  await prepare(page);
  await selectAuth(page);
  await page
    .getByRole("button", { name: "Explorar catálogo", exact: true })
    .click();
  const second = page
    .locator(".template-card")
    .filter({ hasText: "Interação e acesso" });
  state.autoFill = false;
  await second
    .getByRole("button", { name: "Adicionar este template", exact: true })
    .click();
  await expect.poll(() => state.fills.length).toBe(2);
  expect(state.fills[1].ids).toEqual(["interacao"]);
  await second.getByRole("checkbox").uncheck();
  await expect
    .poll(
      () =>
        state.draft.attachments.find((item) => item.template_id === "interacao")
          ?.enabled,
    )
    .toBe(false);
  ready();
  await page.reload();
  await expect(
    page.getByText("1 template selecionado", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Revisar pedido", exact: true }),
  ).toBeEnabled();
  expect(
    state.draft.attachments.find((item) => item.template_id === "interacao")
      ?.fields,
  ).toEqual({});
});

test("refresh retoma preenchimento e voltar sem alterar não chama IA novamente", async ({
  page,
}) => {
  const { state, ready } = await preparationWorkspace(page);
  state.autoFill = false;
  await prepare(page);
  await authCard(page).getByRole("checkbox").check();
  await expect.poll(() => state.fills.length).toBe(1);
  await page.reload();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Aguardando",
  );
  ready();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
  );
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page
    .getByRole("button", { name: "Voltar aos templates", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  expect(state.fills).toHaveLength(1);
});

test("edição humana aberta durante resposta atrasada é preservada com demais campos automáticos", async ({
  page,
}) => {
  const { state, ready, output } = await preparationWorkspace(page);
  state.autoFill = false;
  await prepare(page);
  await authCard(page).getByRole("checkbox").check();
  await expect.poll(() => state.fills.length).toBe(1);
  await authCard(page)
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByLabel("Interação desejada *", { exact: true })
    .fill("Texto humano preservado durante a resposta.");
  ready();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.draft.attachments[0].fields.interaction).toBe(
    "Texto humano preservado durante a resposta.",
  );
  expect(state.draft.attachments[0].fields.field_8).toBe(output.field_8);
  expect(
    state.draft.attachments[0].field_values?.interaction.edited_by_user,
  ).toBe(true);
  expect(state.fills).toHaveLength(1);
});

test("preenchimento parcial pergunta somente pendência essencial e guarda os campos resolvidos", async ({
  page,
}, info) => {
  const { state, output } = await preparationWorkspace(page);
  output.field_9 = null;
  await prepare(page);
  await authCard(page).getByRole("checkbox").check();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Precisa de informação",
  );
  await expect
    .poll(() => state.draft.attachments[0]?.fields.interaction)
    .not.toBeUndefined();
  state.draft.attachments[0].fill_questions = [
    {
      id: "missing",
      question: "Qual mensagem deve aparecer quando o recurso não existe?",
      field_ids: ["field_9"],
      required: true,
    },
  ];
  await page.reload();
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await expect(page.locator(".cm-content")).toBeVisible();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Precisa de informação",
  );
  await page.screenshot({
    path: info.outputPath("05-pendencia.png"),
    fullPage: true,
  });
  await authCard(page)
    .getByRole("button", {
      name: "Qual mensagem deve aparecer quando o recurso não existe?",
      exact: true,
    })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByLabel("Resultado para recurso inexistente *", { exact: true }),
  ).toBeFocused();
  await expect(
    page
      .getByRole("dialog")
      .getByLabel("Interação desejada *", { exact: true }),
  ).not.toHaveValue("");
  await page
    .getByRole("dialog")
    .getByLabel("Resultado para recurso inexistente *", { exact: true })
    .fill("Informar que o recurso não está disponível.");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Revisar pedido", exact: true }),
  ).toBeEnabled();
  expect(state.fills).toHaveLength(1);
});

test("falha terminal oferece tentativa contextual; resultado incerto bloqueia nova chamada", async ({
  page,
}, info) => {
  const { state } = await preparationWorkspace(page);
  state.autoFill = false;
  await prepare(page);
  await authCard(page).getByRole("checkbox").check();
  await expect.poll(() => state.fills.length).toBe(1);
  state.draft.attachments[0].fill_state = "reconciling";
  state.draft.attachments[0].fill_message =
    "Aguardando conciliação do resultado externo.";
  state.version++;
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Tentar preencher novamente",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Revisar pedido", exact: true }),
  ).toBeDisabled();
  state.draft.attachments[0].fill_state = "failed";
  state.draft.attachments[0].fill_message =
    "Não foi possível preencher este template. Seu conteúdo foi preservado.";
  state.version++;
  await page.reload();
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.querySelector(".guided-body")?.scrollTo(0, 0);
  });
  await expect(page.locator(".cm-content")).toBeVisible();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Falha no preenchimento",
  );
  await page.screenshot({
    path: info.outputPath("06-falha.png"),
    fullPage: true,
  });
  await expect(
    page.getByRole("button", {
      name: "Tentar preencher novamente",
      exact: true,
    }),
  ).toBeVisible();
  state.autoFill = true;
  await page
    .getByRole("button", { name: "Tentar preencher novamente", exact: true })
    .click();
  await page
    .getByRole("dialog", { name: "Tentar preencher novamente?", exact: true })
    .getByRole("button", { name: "Confirmar nova tentativa", exact: true })
    .click();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
  );
  expect(state.fills).toHaveLength(2);
});

test("falha Jev é explícita e mantém seleção livre no catálogo sem preseleção", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  state.recommendationFails = true;
  await prepare(page);
  await expect(page.getByText(/O Jev está indisponível/)).toBeVisible();
  await expect(page.locator(".template-card")).toHaveCount(2);
  expect(state.draft.attachments).toEqual([]);
  expect(state.fills).toHaveLength(0);
  await selectAuth(page);
  expect(state.fills).toHaveLength(1);
});

test("remover último anexo da revisão volta a Templates e exige confirmar a seleção vazia", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  await prepare(page);
  await selectAuth(page);
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  await page
    .locator(".attachment-card")
    .getByRole("button", { name: "Remover", exact: true })
    .click();
  await expect(page.getByText("Etapa 2 de 3")).toBeVisible();
  await reviewEmpty(page);
  expect(state.draft.review_snapshot?.attachments).toEqual([]);
  expect(state.draft.review_snapshot?.empty_selection_confirmed).toBe(true);
});

test("ideia alterada invalida revisão e volta pela preparação sem chamadas em cada tecla", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  await prepare(page);
  await selectAuth(page);
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Editar pedido", exact: true })
    .click();
  await page
    .getByLabel("O que vamos fazer?")
    .fill("Corrigir o login preservando regras existentes.");
  await expect.poll(() => state.draft.review_snapshot).toBeNull();
  expect(state.fills).toHaveLength(1);
  await expect(
    page.getByRole("button", { name: "Revisão, etapa futura", exact: true }),
  ).toBeDisabled();
  await prepare(page);
  await expect.poll(() => state.fills.length).toBe(2);
});

test("perda de resposta de salvamento recupera estado sem repetir seleção ou operação", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  await prepare(page);
  state.loseNextSave = true;
  await authCard(page).getByRole("checkbox").check();
  await expect.poll(() => state.fills.length).toBe(1);
  await page.reload();
  await expect(authCard(page).locator(".template-fill-state")).toHaveText(
    "Preenchido pela IA",
  );
  expect(state.fills).toHaveLength(1);
  expect(
    state.requests.filter((item) =>
      item.path.endsWith("/template-preparations"),
    ),
  ).toHaveLength(0);
});

test("erro autoritativo não apaga revisão nem anexo e recebe foco", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  await prepare(page);
  await selectAuth(page);
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  const snapshot = structuredClone(state.draft.review_snapshot);
  state.sendError = {
    attachment_id: authTemplate.id,
    field_id: "interaction",
    code: "FIELD_REQUIRED",
    message: "Confirme o comportamento esperado do botão.",
    action: "Editar anexo",
  };
  await page
    .getByRole("button", { name: "Enviar para construir", exact: true })
    .click();
  await expect(page.locator("#build-workflow-error")).toBeFocused();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  expect(state.draft.review_snapshot).toEqual(snapshot);
  expect(state.submitted).toHaveLength(0);
});

test("teclado, celulares, zoom e texto hostil preservam leitura sem executar markup", async ({
  page,
}) => {
  const { state, output } = await preparationWorkspace(page);
  output.interaction =
    '<img src=x onerror="window.templateInjected=true"> Abrir o modal.';
  await prepare(page);
  await selectAuth(page);
  await authCard(page)
    .getByRole("button", { name: `Abrir ${authTemplate.title}`, exact: true })
    .click();
  await expect(authCard(page)).toContainText("<img src=x");
  expect(await page.evaluate(() => "templateInjected" in window)).toBe(false);
  expect(
    (
      await new AxeBuilder({ page })
        .include(".guided-build")
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 900)
      await page.getByRole("tab", { name: "Construção", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await expect(
      page.getByRole("button", { name: "Revisar pedido", exact: true }),
    ).toBeVisible();
  }
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(state.fills).toHaveLength(1);
});

test("merge por campo preserva edição humana concorrente e rejeita conflito no mesmo campo", () => {
  const baseAttachment: Attachment = {
    template_id: authTemplate.id,
    source_version_id: authTemplate.version_id,
    fields: {},
    edited_content: authTemplate.content,
    enabled: true,
  };
  const base: BuildDraft = {
    stage: "templates",
    objective: "Login",
    message_text: "Login",
    recommendation_id: null,
    attachments: [baseAttachment],
  };
  const local = structuredClone(base);
  local.attachments[0].fields.field_8 = "Texto humano durante a solicitação.";
  local.attachments[0].edited_content = renderTemplate(
    authTemplate,
    local.attachments[0].fields,
  );
  const remote = structuredClone(base);
  remote.attachments[0].fields.interaction =
    "Sugestão aceita para abrir modal.";
  remote.attachments[0].edited_content = renderTemplate(
    authTemplate,
    remote.attachments[0].fields,
  );
  const merged = mergeBuildDraft(local, base, remote, {
    [authTemplate.version_id]: authTemplate,
  });
  expect(merged?.attachments[0].fields).toEqual({
    interaction: "Sugestão aceita para abrir modal.",
    field_8: "Texto humano durante a solicitação.",
  });
  expect(merged?.attachments[0].edited_content).toContain(
    "Texto humano durante a solicitação.",
  );
  remote.attachments[0].fields.field_8 = "Outra edição remota.";
  remote.attachments[0].edited_content = renderTemplate(
    authTemplate,
    remote.attachments[0].fields,
  );
  expect(
    mergeBuildDraft(local, base, remote, {
      [authTemplate.version_id]: authTemplate,
    }),
  ).toBeNull();
});

test("renderização segue o contrato de whitespace, limites por item e booleano falso", () => {
  expect(
    renderTemplate(authTemplate, { interaction: "  Abrir modal  " }),
  ).toContain("Interação:   Abrir modal  ");
  expect(
    fieldProblem(
      {
        key: "flag",
        label: "Ativo",
        type: "boolean",
        required: true,
        max_length: 1,
      },
      false,
    ),
  ).toBe("");
  expect(
    fieldProblem(
      {
        key: "items",
        label: "Itens",
        type: "string_list",
        required: true,
        max_length: 3,
      },
      ["abc", "def"],
    ),
  ).toBe("");
  expect(
    fieldProblem(
      {
        key: "items",
        label: "Itens",
        type: "string_list",
        required: true,
        max_length: 3,
      },
      ["abcd"],
    ),
  ).toContain("3 caracteres por item");
});

test("validação distingue placeholders de enum de negócio, unicode e campos opcionais", () => {
  const text = {
    key: "value",
    label: "Valor",
    required: true,
    max_length: 500,
    type: "text" as const,
  };
  for (const value of [
    "A definir",
    "TBD",
    "Não se aplica",
    "{{campo_pendente}}",
    "???",
  ])
    expect(fieldProblem(text, value)).not.toBe("");
  expect(fieldProblem(text, "東京")).toBe("");
  expect(
    fieldProblem(
      {
        ...text,
        type: "enum",
        constraints: { enum: ["Pendente", "Concluído"] },
      },
      "Pendente",
    ),
  ).toBe("");
  expect(
    renderTemplate(
      {
        ...authTemplate,
        content: "Obrigatório: {{interaction}}\nOpcional: {{extra}}",
        fields: [
          authTemplate.fields[0],
          { ...text, key: "extra", required: false },
        ],
      },
      { interaction: "Abrir modal" },
    ),
  ).toBe("Obrigatório: Abrir modal\nOpcional: ");
});

test("conteúdo humano inteiro durante resposta automática conserva texto e campos compatíveis", () => {
  const attachment: Attachment = {
    template_id: authTemplate.id,
    source_version_id: authTemplate.version_id,
    fields: {},
    edited_content: authTemplate.content,
    enabled: true,
  };
  const saved: BuildDraft = {
    stage: "templates",
    objective: "Login",
    message_text: "Login",
    recommendation_id: null,
    attachments: [attachment],
  };
  const local = structuredClone(saved);
  local.attachments[0].edited_content =
    "Pedido completo escrito por mim durante o preenchimento.";
  const remote = structuredClone(saved);
  remote.attachments[0].fields.interaction = "Abrir modal de login.";
  remote.attachments[0].field_values = {
    interaction: {
      field_id: "interaction",
      value: "Abrir modal de login.",
      state: "applied",
      source_type: "request",
      source_refs: ["request:objective"],
      edited_by_user: false,
      updated_at: "2026-10-28T12:00:00Z",
    },
  };
  remote.attachments[0].edited_content = renderTemplate(
    authTemplate,
    remote.attachments[0].fields,
  );
  const merged = mergeBuildDraft(local, saved, remote, {
    [authTemplate.version_id]: authTemplate,
  });
  expect(merged?.attachments[0].edited_content).toBe(
    local.attachments[0].edited_content,
  );
  expect(merged?.attachments[0].fields.interaction).toBe(
    "Abrir modal de login.",
  );
});

test("nova revisão externa exige atualização explícita do contexto antes de recomendar", async ({
  page,
}) => {
  const { state } = await preparationWorkspace(page);
  state.contextRevision = "revision-external";
  await page
    .getByRole("button", { name: "Preparar construção", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Atualizar contexto salvo", exact: true }),
  ).toBeVisible();
  expect(state.fills).toHaveLength(0);
  expect(state.draft.recommendation_status).toBeNull();
  await page
    .getByRole("button", { name: "Atualizar contexto salvo", exact: true })
    .click();
  await prepare(page);
  expect(state.draft.context_revision).toBe("revision-external");
});
