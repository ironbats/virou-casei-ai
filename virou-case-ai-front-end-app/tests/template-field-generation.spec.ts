import { expect, test, type Locator, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Attachment, FieldValue } from "../src/features/guidedTypes";
import { fieldIsEmpty, renderTemplate } from "../src/features/templateFields";
import {
  authTemplate,
  manualTemplate,
  preparationProject,
  preparationWorkspace,
} from "./templatePreparationFixture";

test.use({ viewport: { width: 1440, height: 950 } });

const fillPath = `**/api/v1/projects/${preparationProject}/build-fill`;
const generatedValue =
  "Proposta: solicitar novo login e preservar o pedido atual.";
const generateButton = (
  page: Page | Locator,
  label = "Resultado para sessão expirada",
) => page.getByRole("button", { name: `Gerar · ${label}`, exact: true });
const editor = (page: Page) => page.getByRole("dialog");

async function pendingField(page: Page, secondEmpty = false) {
  const fixture = await preparationWorkspace(page, "templates");
  const { state, output, metadata } = fixture;
  state.autoFill = false;
  await page
    .getByRole("checkbox", { name: `Selecionar ${authTemplate.title}` })
    .check();
  await expect.poll(() => state.fills.length).toBe(1);
  const initial = state.draft.attachments[0];
  initial.fields = {
    interaction: "Meu contexto manual preservado.",
    field_8: "",
    field_9: secondEmpty ? "" : output.field_9,
  };
  initial.field_values = {
    interaction: {
      field_id: "interaction",
      value: initial.fields.interaction,
      state: "user_edited",
      source_type: "manual",
      source_refs: [],
      edited_by_user: true,
      updated_at: "2026-10-28T12:00:00Z",
    },
  };
  initial.edited_content = renderTemplate(authTemplate, initial.fields);
  initial.fill_state = "needs_input";
  initial.fill_questions = [
    {
      id: "session-question",
      question: "O que deve acontecer quando a sessão expirar?",
      field_ids: ["field_8"],
      required: true,
    },
  ];
  state.version++;
  const requests: { body: any; key: string; draft: typeof state.draft }[] = [];
  const operations: { fieldId: string; original: FieldValue | undefined }[] =
    [];
  const replies = new Map<string, unknown>();
  const control = {
    rejectNext: false,
    loseNext: false,
    finishBeforeLoss: false,
    delayResponse: null as Promise<void> | null,
  };
  const attachment = () => state.draft.attachments[0];
  function complete(value: FieldValue = generatedValue, fail = false) {
    const operation = operations.at(-1)!;
    const current = attachment();
    const automatic =
      current.edited_content === renderTemplate(authTemplate, current.fields);
    if (!fail && fieldIsEmpty(current.fields[operation.fieldId])) {
      current.fields[operation.fieldId] = value;
      current.field_values = {
        ...current.field_values,
        [operation.fieldId]: {
          field_id: operation.fieldId,
          value,
          state: "applied",
          source_type: "policy_suggestion",
          source_refs: [],
          edited_by_user: false,
          updated_at: "2026-10-28T12:05:00Z",
        },
      };
      if (automatic)
        current.edited_content = renderTemplate(authTemplate, current.fields);
      current.fill_questions = current.fill_questions?.filter(
        (question) => !question.field_ids.includes(operation.fieldId),
      );
    }
    current.field_generations = {
      ...current.field_generations,
      [operation.fieldId]: {
        state: fail ? "failed" : "succeeded",
        operation_id: `field-operation-${operations.length}`,
        ...(fail
          ? {
              message: "Não foi possível gerar o campo. Tente novamente.",
              error_code: "PROVIDER_CONFIGURATION_INVALID",
            }
          : {}),
      },
    };
    current.fill_state = fail ? "failed" : "ready";
    state.version++;
    metadata();
  }
  await page.route(fillPath, async (route) => {
    const request = route.request();
    const body = request.postDataJSON();
    if (!body.target_field_id) return route.fallback();
    const key = request.headers()["idempotency-key"];
    requests.push({ body, key, draft: structuredClone(state.draft) });
    if (replies.has(key))
      return route.fulfill({ status: 202, json: replies.get(key) });
    if (control.rejectNext) {
      control.rejectNext = false;
      return route.fulfill({
        status: 400,
        json: {
          error: {
            code: "GENERATION_UNAVAILABLE",
            message:
              "A geração está indisponível. Tente novamente ou edite o campo.",
            request_id: "field-test-request-1",
          },
        },
      });
    }
    if (body.expected_draft_revision !== state.version)
      return route.fulfill({
        status: 412,
        json: { error: { code: "DRAFT_CONFLICT", message: "O pedido mudou." } },
      });
    expect(body.attachment_ids).toEqual([authTemplate.id]);
    const current = attachment();
    expect(fieldIsEmpty(current.fields[body.target_field_id])).toBe(true);
    operations.push({
      fieldId: body.target_field_id,
      original: current.fields[body.target_field_id],
    });
    current.field_generations = {
      ...current.field_generations,
      [body.target_field_id]: {
        state: "queued",
        operation_id: `field-operation-${operations.length}`,
      },
    };
    state.version++;
    const response = {
      version: state.version,
      data: metadata(),
      updated_at: null,
    };
    replies.set(key, response);
    if (control.delayResponse) await control.delayResponse;
    if (control.loseNext) {
      control.loseNext = false;
      if (control.finishBeforeLoss) complete(generatedValue, true);
      return route.abort("failed");
    }
    return route.fulfill({ status: 202, json: response });
  });
  await page.reload();
  await expect(generateButton(page)).toBeEnabled();
  return { ...fixture, requests, operations, control, attachment, complete };
}

test("cartão gera só o campo vazio com o pedido salvo e retoma o progresso após recarregar", async ({
  page,
}, info) => {
  const fixture = await pendingField(page);
  const initial = structuredClone(fixture.attachment());
  await expect(page.getByText(/Gerar usa o pedido salvo/)).toBeVisible();
  await generateButton(page).click();
  await expect.poll(() => fixture.operations.length).toBe(1);
  expect(fixture.requests[0].body).toEqual({
    expected_draft_revision: fixture.state.version - 1,
    attachment_ids: [authTemplate.id],
    target_field_id: "field_8",
  });
  expect(fixture.requests[0].draft.message_text).toBe(
    fixture.state.draft.message_text,
  );
  await page.reload();
  await expect(
    page.getByRole("button", {
      name: "Gerando · Resultado para sessão expirada",
      exact: true,
    }),
  ).toBeDisabled();
  expect(fixture.requests).toHaveLength(1);
  fixture.complete();
  await expect(generateButton(page)).toHaveCount(0);
  expect(fixture.attachment().fields).toEqual({
    ...initial.fields,
    field_8: generatedValue,
  });
  expect(fixture.attachment().field_values?.interaction).toEqual(
    initial.field_values?.interaction,
  );
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await expect(
    editor(page).getByLabel("Resultado para sessão expirada *", {
      exact: true,
    }),
  ).toHaveValue(generatedValue);
  await expect(
    editor(page).getByText("Sugestão da IA · revise", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("01-campo-gerado-editor.png"),
  });
  await editor(page)
    .getByRole("button", { name: "Cancelar edição", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Revisar pedido", exact: true })
    .click();
  await expect(page.getByText("Etapa 3 de 3")).toBeVisible();
  expect(
    fixture.state.draft.review_snapshot?.attachments[0].fields.field_8,
  ).toBe(generatedValue);
});

test("editor salva antes de gerar e preserva novas edições e conteúdo personalizado durante a resposta", async ({
  page,
}) => {
  const fixture = await pendingField(page, true);
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  const dialog = editor(page);
  await dialog
    .getByLabel("Interação desejada *", { exact: true })
    .fill("Edição antes de gerar.");
  let release!: () => void;
  fixture.control.delayResponse = new Promise<void>((resolve) => {
    release = resolve;
  });
  await generateButton(editor(page)).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => fixture.requests.length).toBe(1);
  expect(fixture.requests[0].draft.attachments[0].fields.interaction).toBe(
    "Edição antes de gerar.",
  );
  await expect(
    generateButton(editor(page), "Resultado para recurso inexistente"),
  ).toBeDisabled();
  const manual = dialog.getByLabel("Resultado para recurso inexistente *", {
    exact: true,
  });
  await expect(manual).toBeEditable();
  await manual.fill("Meu resultado manual durante a geração.");
  await dialog
    .getByLabel("Conteúdo que será enviado", { exact: true })
    .fill("Conteúdo personalizado preservado enquanto a IA trabalha.");
  await expect(
    dialog.getByRole("button", {
      name: "Gerando · Resultado para sessão expirada",
      exact: true,
    }),
  ).toBeDisabled();
  release();
  fixture.complete();
  await expect(
    dialog.getByLabel("Resultado para sessão expirada *", { exact: true }),
  ).toHaveValue(generatedValue);
  await expect(
    dialog.getByLabel("Conteúdo que será enviado", { exact: true }),
  ).toBeFocused();
  await expect(manual).toHaveValue("Meu resultado manual durante a geração.");
  await expect(
    dialog.getByLabel("Conteúdo que será enviado", { exact: true }),
  ).toHaveValue("Conteúdo personalizado preservado enquanto a IA trabalha.");
  expect(fixture.operations).toHaveLength(1);
  await dialog
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(fixture.attachment().fields.field_9).toBe(
    "Meu resultado manual durante a geração.",
  );
  expect(fixture.attachment().edited_content).toBe(
    "Conteúdo personalizado preservado enquanto a IA trabalha.",
  );
});

test("digitação no campo alvo prevalece sobre a proposta recebida", async ({
  page,
}) => {
  const fixture = await pendingField(page);
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await generateButton(editor(page)).click();
  await expect.poll(() => fixture.operations.length).toBe(1);
  const target = editor(page).getByLabel("Resultado para sessão expirada *", {
    exact: true,
  });
  await target.fill("Minha decisão manual para este campo.");
  fixture.complete();
  await expect(
    editor(page).getByText("Gerando este campo com o pedido atual…", {
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(target).toHaveValue("Minha decisão manual para este campo.");
  await editor(page)
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(editor(page)).toHaveCount(0);
  expect(fixture.attachment().fields.field_8).toBe(
    "Minha decisão manual para este campo.",
  );
  expect(fixture.attachment().field_values?.field_8.edited_by_user).toBe(true);
});

test("falha conclusiva fica junto do campo e permite tentar novamente sem perder valores", async ({
  page,
}, info) => {
  const fixture = await pendingField(page);
  fixture.control.rejectNext = true;
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  const dialog = editor(page);
  await dialog
    .getByLabel("Interação desejada *", { exact: true })
    .fill("Valor preservado mesmo na falha.");
  await generateButton(editor(page)).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "A geração está indisponível",
  );
  await expect(dialog.getByRole("alert")).toContainText("field-test-request-1");
  await expect(
    dialog.getByLabel("Interação desejada *", { exact: true }),
  ).toHaveValue("Valor preservado mesmo na falha.");
  await expect(generateButton(editor(page))).toBeEnabled();
  await page.screenshot({
    path: info.outputPath("02-erro-recuperavel-por-campo.png"),
  });
  await generateButton(editor(page)).click();
  await expect.poll(() => fixture.operations.length).toBe(1);
  fixture.complete();
  await expect(
    dialog.getByLabel("Resultado para sessão expirada *", { exact: true }),
  ).toHaveValue(generatedValue);
  await expect(
    dialog.getByLabel("Resultado para sessão expirada *", { exact: true }),
  ).toBeFocused();
  expect(fixture.requests).toHaveLength(2);
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});

test("resposta perdida conserva corpo e idempotência após recarregar e editar outro campo", async ({
  page,
}) => {
  const fixture = await pendingField(page);
  fixture.control.loseNext = true;
  fixture.control.finishBeforeLoss = true;
  await generateButton(page).click();
  await expect(
    page.getByRole("button", {
      name: "Verificar tentativa · Resultado para sessão expirada",
      exact: true,
    }),
  ).toBeEnabled();
  expect(fixture.operations).toHaveLength(1);
  await page.reload();
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await editor(page)
    .getByLabel("Interação desejada *", { exact: true })
    .fill("Outra edição após a resposta perdida.");
  await editor(page)
    .getByRole("button", { name: "Salvar edição", exact: true })
    .click();
  await expect(editor(page)).toHaveCount(0);
  await page
    .getByRole("button", {
      name: "Verificar tentativa · Resultado para sessão expirada",
      exact: true,
    })
    .click();
  await expect(generateButton(page)).toBeEnabled();
  expect(fixture.requests).toHaveLength(2);
  expect(fixture.requests[1].body).toEqual(fixture.requests[0].body);
  expect(fixture.requests[1].key).toBe(fixture.requests[0].key);
  expect(fixture.operations).toHaveLength(1);
  expect(fixture.attachment().fields.interaction).toBe(
    "Outra edição após a resposta perdida.",
  );
  await generateButton(page).click();
  await expect.poll(() => fixture.operations.length).toBe(2);
  expect(fixture.requests[2].body.expected_draft_revision).toBeGreaterThan(
    fixture.requests[0].body.expected_draft_revision,
  );
  expect(fixture.requests[2].key).not.toBe(fixture.requests[0].key);
  fixture.complete();
  await expect(generateButton(page)).toHaveCount(0);
});

test("pedido ou contexto indisponível bloqueia geração e mantém edição manual", async ({
  page,
}) => {
  const fixture = await pendingField(page);
  fixture.state.contextRevision = "nova-revisao-salva";
  fixture.state.version++;
  await page.reload();
  await expect(generateButton(page)).toBeDisabled();
  expect(fixture.requests).toHaveLength(0);
  fixture.state.contextRevision = fixture.state.draft.context_revision!;
  fixture.state.enabled = false;
  await page.reload();
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await expect(generateButton(editor(page))).toBeDisabled();
  await expect(
    editor(page).getByLabel("Resultado para sessão expirada *", {
      exact: true,
    }),
  ).toBeEditable();
});

test("false e valores existentes não oferecem geração; listas vazias oferecem e campos secretos não", async ({
  page,
}) => {
  const fixture = await preparationWorkspace(page, "templates");
  const { state } = fixture;
  state.autoFill = false;
  const template = state.catalog.find((item) => item.id === manualTemplate.id)!;
  template.fields.push({
    key: "secret",
    label: "Senha privada",
    required: false,
    max_length: 100,
    sensitivity: "secret",
    type: "text",
  });
  const attachment: Attachment = {
    template_id: template.id,
    source_version_id: template.version_id,
    enabled: true,
    fields: {
      modal: false,
      entry: "Menu",
      criteria: ["Critério já preenchido"],
    },
    fill_state: "needs_input",
    edited_content: "Conteúdo salvo para conferir.",
  };
  state.draft.attachments = [attachment];
  state.version++;
  const received: unknown[] = [];
  await page.route(fillPath, async (route) => {
    const body = route.request().postDataJSON();
    received.push(body);
    expect(body.target_field_id).toBe("criteria");
    expect(body.expected_draft_revision).toBe(state.version);
    expect(state.draft.attachments[0].fields.criteria).toEqual([]);
    const current = state.draft.attachments[0];
    current.fields.criteria = ["Proposta gerada apenas para a lista limpa"];
    current.field_values = {
      ...current.field_values,
      criteria: {
        field_id: "criteria",
        value: current.fields.criteria,
        state: "applied",
        source_type: "policy_suggestion",
        source_refs: [],
        edited_by_user: false,
        updated_at: "2026-10-29T00:00:00Z",
      },
    };
    current.field_generations = { criteria: { state: "succeeded" } };
    state.version++;
    await route.fulfill({
      status: 202,
      json: { version: state.version, data: fixture.metadata() },
    });
  });
  await page.reload();
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  await expect(generateButton(editor(page), "Abrir modal?")).toHaveCount(0);
  await expect(generateButton(editor(page), "Onde está o botão?")).toHaveCount(
    0,
  );
  await expect(
    generateButton(editor(page), "Critérios de verificação"),
  ).toHaveCount(0);
  await editor(page)
    .getByLabel("Critérios de verificação *", { exact: true })
    .fill("  \n  ");
  await expect(
    generateButton(editor(page), "Critérios de verificação"),
  ).toBeEnabled();
  await expect(generateButton(editor(page), "Senha privada")).toHaveCount(0);
  await expect(
    editor(page).getByLabel("Abrir modal? *", { exact: true }),
  ).toHaveValue("false");
  await generateButton(editor(page), "Critérios de verificação").click();
  await expect(
    editor(page).getByLabel("Critérios de verificação *", { exact: true }),
  ).toHaveValue("Proposta gerada apenas para a lista limpa");
  expect(received).toHaveLength(1);
  expect(state.draft.attachments[0].fields.modal).toBe(false);
  expect(state.draft.attachments[0].fields.entry).toBe("Menu");
});

test("ação por campo mantém teclado, contraste e layout sem overflow nos tamanhos suportados", async ({
  page,
}, info) => {
  await pendingField(page, true);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 950 });
    if (width <= 900)
      await page.getByRole("tab", { name: "Construção", exact: true }).click();
    await expect(generateButton(page)).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await page.setViewportSize({ width: 375, height: 812 });
  await page
    .getByRole("button", { name: "Editar conteúdo", exact: true })
    .click();
  const dialog = editor(page);
  await generateButton(dialog).focus();
  await expect(generateButton(dialog)).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    dialog.getByLabel("Resultado para sessão expirada *", { exact: true }),
  ).toBeFocused();
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  const accessibility = await new AxeBuilder({ page })
    .include("dialog")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  await page.screenshot({ path: info.outputPath("03-gerar-campo-mobile.png") });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});
