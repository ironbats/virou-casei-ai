import { showConversation } from "./workspaceHelper";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import type { Operation } from "../src/shared/types";

const pid = "code-analysis-project";
const rid = "code-analysis-revision";
const policy = "analysis-policy-test";
const before = "export const amount = ;";
const after = "export const amount = 1;";
const hash = (value: string) =>
  createHash("sha256").update(value).digest("hex");

function operation(
  id: string,
  type: string,
  result: Operation["result_ref"] = {},
): Operation {
  return {
    id,
    type,
    project_id: pid,
    state: "queued",
    progress_stage: "queued",
    billing_status: "reserved",
    pending_reconciliation: false,
    cancel_requested: false,
    error_code: null,
    created_at: new Date().toISOString(),
    result_ref: result,
  };
}

async function fixture(
  page: Page,
  allowedActions = ["read", "write", "publish", "export"],
) {
  const publication = operation("failed-publication", "publication", {
    revision_id: rid,
    publication_failure: {
      stage: "publication_building",
      message: "A preparação encontrou um problema no código.",
    },
  });
  publication.state = "failed";
  publication.error_code = "BUILD_FAILED";
  const state = {
    operations: [publication],
    writes: [] as {
      path: string;
      body: Record<string, unknown>;
      csrf?: string;
      idempotency?: string;
    }[],
    analysisFailures: 0,
    lostAnalysisResponse: false,
    hiddenAnalysisReads: 0,
    heldReads: 0,
    heldOperation: null as null | { id: string; wait: Promise<void> },
    correctionFailures: 0,
    lostCorrectionResponse: false,
    heldCorrectionResponse: null as Promise<void> | null,
    releasedCorrectionResponses: 0,
    unavailableCorrectionReads: false,
    hiddenCorrectionReads: 0,
    analysisEnabled: true,
    currentRevision: rid,
    dirty: false,
    buildMessage: "",
    files: {
      "src/index.ts": before,
      "public/index.html": "<h1>Projeto da análise</h1>",
    } as Record<string, string>,
    canPublish: true,
    automatic: null as null | Record<string, unknown>,
  };
  const correctionIntents = new Map<string, Operation>();
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    const body = request.method() === "GET" ? {} : request.postDataJSON();
    if (request.method() !== "GET")
      state.writes.push({
        path,
        body,
        csrf: request.headers()["x-csrf-token"],
        idempotency: request.headers()["idempotency-key"],
      });
    if (path === "/me")
      return route.fulfill({
        json: {
          id: "analysis-user",
          name: "Aluno",
          role: "student",
          csrf_token: "analysis-csrf",
          access: {
            status: "active",
            allowed_actions: allowedActions,
            period_id: "analysis-period",
          },
        },
      });
    if (path === "/wallet")
      return route.fulfill({
        json: {
          available_ratio: 1,
          reserved_ratio: 0,
          consumed_ratio: 0,
          can_start_operation: true,
          pending_reconciliation: false,
          usage_status: "available",
        },
      });
    if (path === `/projects/${pid}`)
      return route.fulfill({
        json: {
          id: pid,
          name: "Projeto da análise",
          objective: "Corrigir erro preservando revisão",
          stack: "hono-typescript-d1",
          version: 1,
          current_revision_id: state.currentRevision,
          state: "active",
          created_at: "2026-10-29T10:00:00Z",
        },
      });
    if (path === "/capabilities")
      return route.fulfill({
        json: {
          policy_version: policy,
          code_analysis: { enabled: state.analysisEnabled },
          generation: { enabled: true },
          publication: { enabled: true },
          preview: { enabled: true },
          visual_preview: {
            enabled: true,
            mode: "browser_static",
            backend_execution: false,
          },
          guided_build: {
            max_objective_chars: 12000,
            max_message_chars: 12000,
            max_attachments: 10,
            max_attachment_chars: 12000,
            max_context_bytes: 100000,
            autosave_ms: 700,
          },
          limits: {
            max_projects: 10,
            max_files: 100,
            max_file_bytes: 100000,
            max_project_bytes: 1000000,
            max_path_length: 200,
            max_prompt_chars: 12000,
            max_revisions: 100,
          },
        },
      });
    if (path === "/templates") return route.fulfill({ json: { items: [] } });
    if (path === `/projects/${pid}/draft`)
      return route.fulfill({
        json: {
          version: 1,
          data: {
            base_revision_id: rid,
            files: state.dirty
              ? { ...state.files, "src/index.ts": "unsaved source" }
              : null,
            prompt: "",
          },
        },
      });
    if (path === `/projects/${pid}/build-draft`)
      return route.fulfill({
        json: {
          version: 1,
          data: {
            stage: "idea",
            objective: "Analisar projeto",
            message_text: state.buildMessage,
            attachments: [],
            recommendation_id: null,
          },
        },
      });
    if (path === `/projects/${pid}/revisions/${rid}`)
      return route.fulfill({
        json: {
          id: rid,
          project_id: pid,
          origin: "manual",
          candidate: false,
          created_at: "2026-10-29T10:00:00Z",
          files: Object.keys(state.files).map((name, index) => ({
            id: String(index),
            path: name,
            size: state.files[name].length,
          })),
        },
      });
    if (path.startsWith(`/projects/${pid}/revisions/${rid}/files/`)) {
      const name = Object.keys(state.files)[Number(path.split("/").at(-1))];
      return route.fulfill({
        json: { path: name, content: state.files[name] },
      });
    }
    if (path === `/projects/${pid}/publication-readiness`)
      return route.fulfill({
        json: {
          can_publish: state.canPublish,
          revision_id: state.currentRevision,
          configuration_version: 1,
          reasons: state.canPublish
            ? []
            : [
                {
                  code: "CAPABILITY_UNVALIDATED",
                  message: "Aguardando verificação da conexão.",
                },
              ],
          automatic_verification: state.automatic,
          publication: null,
        },
      });
    if (path === "/operations") {
      const kind = url.searchParams.get("kind");
      const active = url.searchParams.get("state") === "active";
      if (
        kind === "code_analysis" &&
        state.hiddenAnalysisReads > 0 &&
        state.operations.some((op) => op.type === "code_analysis")
      ) {
        state.hiddenAnalysisReads -= 1;
        return route.fulfill({ json: { items: [], next_cursor: null } });
      }
      return route.fulfill({
        json: {
          items: state.operations.filter(
            (op) =>
              (!kind || op.type === kind) &&
              (!active ||
                ["queued", "running", "reconciling"].includes(op.state)),
          ),
          next_cursor: null,
        },
      });
    }
    if (path.startsWith("/operations/")) {
      const op = state.operations.find(
        (item) => item.id === path.split("/").at(-1),
      );
      if (
        op?.type === "code_analysis" &&
        op.result_ref.correction_operation_id &&
        state.unavailableCorrectionReads
      )
        return route.fulfill({
          status: 503,
          json: {
            error: { message: "Consulta temporariamente indisponível." },
          },
        });
      if (
        op?.type === "code_analysis" &&
        state.hiddenCorrectionReads > 0 &&
        op.result_ref.correction_operation_id
      ) {
        state.hiddenCorrectionReads -= 1;
        const pending = structuredClone(op);
        delete pending.result_ref.correction_operation_id;
        return route.fulfill({ json: pending });
      }
      if (op && state.heldOperation?.id === op.id) {
        const snapshot = structuredClone(op);
        state.heldReads += 1;
        await state.heldOperation.wait;
        return route.fulfill({ json: snapshot }).catch(() => undefined);
      }
      return route.fulfill({
        status: op ? 200 : 404,
        json: op ?? {
          error: { code: "NOT_FOUND", message: "Operação ausente." },
        },
      });
    }
    if (path === `/projects/${pid}/code-analyses`) {
      if (state.analysisFailures-- > 0)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "ANALYSIS_UNAVAILABLE",
              message: "Não foi possível iniciar a análise agora.",
            },
          },
        });
      const op = operation(
        `analysis-${state.operations.length}`,
        "code_analysis",
        {
          kind: "code_analysis",
          revision_id: String(body.revision_id),
          source: body.source,
          publication_operation_id: body.publication_operation_id,
        },
      );
      state.operations.unshift(op);
      if (state.lostAnalysisResponse) {
        state.lostAnalysisResponse = false;
        return route.abort("failed");
      }
      return route.fulfill({ status: 202, json: op });
    }
    if (/\/messages\/[^/]+\/reply$/.test(path)) {
      const question = state.operations.find(
        (op) => op.id === path.split("/").at(-2),
      )!;
      const response = operation("correction-reply-retried", "generation", {
        revision_id: rid,
      });
      response.reply_to = question.id;
      response.request_text = String(body.message_text);
      response.analysis_id = question.analysis_id;
      response.analysis_revision_id = rid;
      state.operations.unshift(response);
      return route.fulfill({ status: 202, json: response });
    }
    if (/\/code-analyses\/[^/]+\/correction$/.test(path)) {
      if (state.correctionFailures-- > 0)
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: "VERSION_CONFLICT",
              message: "A revisão mudou. Peça uma nova análise.",
            },
          },
        });
      const intent = request.headers()["idempotency-key"];
      const replay = correctionIntents.get(intent);
      if (replay) return route.fulfill({ status: 202, json: replay });
      const analysis = state.operations.find(
        (op) => op.id === path.split("/").at(-2),
      )!;
      const attempt =
        state.operations.filter((op) => op.type === "generation").length + 1;
      const op = operation(
        attempt === 1
          ? "correction-operation"
          : `correction-operation-${attempt}`,
        "generation",
        {
          revision_id: rid,
        },
      );
      op.analysis_id = analysis.id;
      op.analysis_revision_id = rid;
      op.request_text =
        "Corrija os problemas identificados na análise desta revisão.";
      analysis.result_ref.correction_operation_id = op.id;
      state.operations.unshift(op);
      correctionIntents.set(intent, op);
      if (state.lostCorrectionResponse) {
        state.lostCorrectionResponse = false;
        return route.abort("failed");
      }
      if (state.heldCorrectionResponse) {
        await state.heldCorrectionResponse;
        state.releasedCorrectionResponses += 1;
      }
      return route.fulfill({ status: 202, json: op }).catch(() => undefined);
    }
    if (path === `/projects/${pid}/proposals/correction-operation`)
      return route.fulfill({
        json: {
          proposal_id: "correction-operation",
          proposal_version: "correction-v1",
          base_project_revision: rid,
          candidate_revision_id: "corrected-revision",
          current_project_revision: rid,
          expected_project_version: 1,
          state: "generated",
          warnings: [],
          conflict_paths: [],
          files: [
            {
              id: "changed-source",
              path: "src/index.ts",
              change: "modified",
              before_hash: hash(before),
              after_hash: hash(after),
              before_bytes: before.length,
              after_bytes: after.length,
              content_type: "text/typescript",
            },
          ],
          verification_results: [
            {
              kind: "compile",
              state: "not_run",
              message: "Compilação pendente.",
            },
          ],
        },
      });
    if (path.includes("/proposals/correction-operation/files/"))
      return route.fulfill({
        json: {
          proposal_id: "correction-operation",
          proposal_version: "correction-v1",
          id: "changed-source",
          path: "src/index.ts",
          change: "modified",
          before_hash: hash(before),
          after_hash: hash(after),
          before_bytes: before.length,
          after_bytes: after.length,
          content_type: "text/typescript",
          before,
          after,
        },
      });
    return route.fulfill({
      status: 404,
      json: {
        error: { code: "NOT_FOUND", message: "Rota não prevista no teste." },
      },
    });
  });
  return state;
}

function finishAnalysis(
  state: Awaited<ReturnType<typeof fixture>>,
  canCorrect = true,
) {
  const op = state.operations.find((item) => item.type === "code_analysis")!;
  op.state = "succeeded";
  op.result_ref = {
    ...op.result_ref,
    summary: "A declaração de amount está incompleta.",
    issues: [
      {
        severity: "error",
        path: "src/index.ts",
        line: 1,
        title: "Expressão ausente",
        explanation: "A atribuição não fornece uma expressão válida.",
        suggestion: "Informe um valor válido antes do ponto e vírgula.",
      },
    ],
    limitations: ["A análise não executou a compilação."],
    can_request_correction: canCorrect,
  };
  return op;
}

async function ask(page: Page) {
  const panel = page.getByRole("region", { name: "Análise de código com IA" });
  await expect(
    panel.getByRole("button", { name: "Pedir Análise IA", exact: true }),
  ).toBeEnabled();
  await panel
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "Analisando código…" }),
  ).toBeDisabled();
  return panel;
}

test("correção da publicação abre diretamente o chat e restaura a proposta sem duplicar painéis ou pedidos", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  await expect(
    page.getByText(/Esta tentativa não possui um diagnóstico detalhado/),
  ).toBeVisible();
  const panel = await ask(page);
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({
    path: `/projects/${pid}/code-analyses`,
    body: {
      revision_id: rid,
      source: "publication",
      publication_operation_id: "failed-publication",
      policy_version: policy,
    },
    csrf: "analysis-csrf",
  });
  expect(state.writes[0].idempotency).toBeTruthy();
  const analysis = finishAnalysis(state);
  await expect(
    panel.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  await expect(panel).toContainText("src/index.ts:1");
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/app/projetos/${pid}\\?conversation=correction-operation$`),
  );
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toBeVisible();
  await expect(chat).toBeFocused();
  await expect(chat).toContainText(
    "Corrija os problemas identificados na análise desta revisão.",
  );
  await expect(page.locator(".ide-panel .code-analysis")).toHaveCount(0);
  await expect(page.locator(".code-analysis-correction")).toHaveCount(0);
  expect(state.writes).toHaveLength(2);
  expect(state.writes[1]).toMatchObject({
    path: `/projects/${pid}/code-analyses/${analysis.id}/correction`,
    body: { base_project_revision: rid, policy_version: policy },
  });
  const correction = state.operations.find((op) => op.type === "generation")!;
  correction.state = "succeeded";
  correction.progress_message = "Proposta pronta para revisão.";
  correction.result_ref = {
    kind: "implementation",
    candidate: true,
    revision_id: "corrected-revision",
    changed_files: [{ path: "src/index.ts", change: "modified" }],
  };
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(chat).toBeVisible();
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toHaveCount(0);
  await chat
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("src/index.ts");
  expect(state.currentRevision).toBe(rid);
  expect(state.writes).toHaveLength(2);
});

test("resposta perdida da correção recupera a mesma operação por GET e abre o chat", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  state.lostCorrectionResponse = true;
  state.hiddenCorrectionReads = 2;
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  await expect(page).toHaveURL(
    new RegExp(`conversation=correction-operation$`),
  );
  expect(
    state.operations.filter((op) => op.type === "generation"),
  ).toHaveLength(1);
  expect(
    state.writes.filter((write) => write.path.endsWith("/correction")),
  ).toHaveLength(1);
  await page.reload();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  expect(state.writes).toHaveLength(2);
});

test("resposta pendente da correção recupera por consulta após o prazo sem reenviar o pedido", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  let release: () => void = () => {};
  state.heldCorrectionResponse = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
    await expect
      .poll(
        () =>
          state.writes.filter((write) => write.path.endsWith("/correction"))
            .length,
      )
      .toBe(1);
    await expect(
      page.getByRole("region", { name: "Conversa de construção" }),
    ).toBeVisible({ timeout: 30000 });
    await expect(page).toHaveURL(/conversation=correction-operation$/);
    expect(
      state.operations.filter((op) => op.type === "generation"),
    ).toHaveLength(1);
    expect(
      state.writes.filter((write) => write.path.endsWith("/correction")),
    ).toHaveLength(1);
  } finally {
    release();
  }
});

test("confirmação tardia após timeout não remove a intenção quando a consulta falha e o usuário tenta novamente", async ({
  page,
}) => {
  test.setTimeout(90000);
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  let release: () => void = () => {};
  state.heldCorrectionResponse = new Promise<void>((resolve) => {
    release = resolve;
  });
  state.unavailableCorrectionReads = true;
  try {
    await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
    await expect(panel.getByRole("alert")).toContainText(
      "O recebimento da correção ainda não foi confirmado.",
      { timeout: 40000 },
    );
    const pendingIntents = () =>
      page.evaluate(() =>
        Object.keys(sessionStorage)
          .filter((key) => key.startsWith("vira-intent:"))
          .map((key) => sessionStorage.getItem(key)),
      );
    const writes = () =>
      state.writes.filter((write) => write.path.endsWith("/correction"));
    expect(writes()).toHaveLength(1);
    expect(await pendingIntents()).toContain(writes()[0].idempotency);
    release();
    await expect.poll(() => state.releasedCorrectionResponses).toBe(1);
    expect(await pendingIntents()).toContain(writes()[0].idempotency);
    state.unavailableCorrectionReads = false;
    state.hiddenCorrectionReads = 50;
    await panel
      .getByRole("button", { name: "Atualizar análise", exact: true })
      .click();
    await expect(
      panel.getByRole("button", { name: "Solicitar correção IA" }),
    ).toBeEnabled();
    await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
    await expect(page).toHaveURL(/conversation=correction-operation$/);
    await expect(
      page.getByRole("region", { name: "Conversa de construção" }),
    ).toBeVisible();
    expect(writes()).toHaveLength(2);
    expect(writes()[1].idempotency).toBe(writes()[0].idempotency);
    expect(
      state.operations.filter((operation) => operation.type === "generation"),
    ).toHaveLength(1);
  } finally {
    release();
  }
});

test("correção antiga sem metadados usa o vínculo confirmado da análise para tentar novamente", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  const analysis = finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  const correction = state.operations.find((op) => op.type === "generation")!;
  delete correction.analysis_id;
  delete correction.analysis_revision_id;
  correction.state = "failed";
  correction.billing_status = "settled";
  await page.reload();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await chat.getByRole("button", { name: "Tentar correção novamente" }).click();
  await expect
    .poll(
      () =>
        state.writes.filter((write) => write.path.endsWith("/correction"))
          .length,
    )
    .toBe(2);
  expect(state.writes.at(-1)?.path).toBe(
    `/projects/${pid}/code-analyses/${analysis.id}/correction`,
  );
  expect(
    state.writes.some((write) => write.path.endsWith("/generations")),
  ).toBe(false);
});

test("correção em reconciliação não permite repetir a cobrança pelo chat", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  const correction = state.operations.find((op) => op.type === "generation")!;
  correction.state = "failed";
  correction.pending_reconciliation = true;
  correction.billing_status = "reconciliation_pending";
  await page.reload();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toContainText(
    "Aguarde a conferência da tentativa anterior",
  );
  await expect(
    chat.getByRole("button", { name: "Tentar correção novamente" }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", { name: "Reutilizar pedido" }),
  ).toHaveCount(0);
  expect(state.writes).toHaveLength(2);
});

test("revisão alterada impede retry do diagnóstico antigo dentro do chat", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  const correction = state.operations.find((op) => op.type === "generation")!;
  correction.state = "failed";
  state.currentRevision = "changed-revision";
  await page.reload();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(
    chat.getByRole("button", { name: "Tentar correção novamente" }),
  ).toBeDisabled();
  await expect(chat).toContainText("A revisão mudou. Peça uma nova análise");
  expect(state.writes).toHaveLength(2);
});

test("em mobile a correção abre a aba de conversa, recebe foco e preserva o rascunho de construção", async ({
  page,
}, info) => {
  const state = await fixture(page);
  state.buildMessage = "Rascunho do próximo pedido preservado.";
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toBeVisible();
  await expect(chat).toBeFocused();
  await expect(
    page.getByRole("tab", { name: "Chat", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".ide-panel")).toBeHidden();
  expect(
    state.writes.filter((write) => write.path.endsWith("/build-draft")),
  ).toHaveLength(0);
  const results = await new AxeBuilder({ page })
    .include(".conversation-panel")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await page.screenshot({
    path: info.outputPath("correction-chat-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.screenshot({
    path: info.outputPath("correction-chat-mobile.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Nova construção", exact: true })
    .click();
  await expect(
    page.getByLabel("O que vamos fazer?", { exact: true }),
  ).toHaveValue(state.buildMessage);
});

test("retry de resposta a esclarecimento mantém a decisão no chat e não reinicia a correção", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  const question = state.operations.find((op) => op.type === "generation")!;
  question.state = "succeeded";
  question.result_ref = {
    kind: "clarification",
    message: "Posso remover o arquivo obsoleto?",
  };
  const failed = operation("failed-correction-reply", "generation");
  failed.state = "failed";
  failed.billing_status = "settled";
  failed.analysis_id = question.analysis_id;
  failed.analysis_revision_id = rid;
  failed.reply_to = question.id;
  failed.request_text = "Sim, remova somente o arquivo obsoleto.";
  state.operations.unshift(failed);
  await page.goto(`/app/projetos/${pid}?conversation=${failed.id}`);
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await chat
    .getByRole("button", { name: "Tentar responder novamente" })
    .click();
  await expect(page.getByLabel("Sua resposta ao agente")).toHaveValue(
    failed.request_text,
  );
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  await expect
    .poll(
      () =>
        state.writes.filter((write) => write.path.endsWith("/reply")).length,
    )
    .toBe(1);
  expect(
    state.writes.find((write) => write.path.endsWith("/reply")),
  ).toMatchObject({
    path: `/projects/${pid}/messages/${question.id}/reply`,
    body: { message_text: failed.request_text, base_project_revision: rid },
  });
  expect(
    state.writes.filter((write) => write.path.endsWith("/correction")),
  ).toHaveLength(1);
  await expect(
    chat.getByRole("button", { name: "Tentar correção novamente" }),
  ).toHaveCount(0);
});

test("acesso somente leitura pode acompanhar a correção mas não repetir a solicitação", async ({
  page,
}) => {
  const actions = ["read", "write", "publish"];
  const state = await fixture(page, actions);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(
    page.getByRole("region", { name: "Conversa de construção" }),
  ).toBeVisible();
  state.operations.find((op) => op.type === "generation")!.state = "failed";
  actions.splice(1);
  await page.reload();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(
    chat.getByRole("button", { name: "Tentar correção novamente" }),
  ).toBeDisabled();
  await expect(chat).toContainText(
    "Seu acesso não permite solicitar uma correção.",
  );
  expect(state.writes).toHaveLength(2);
});

test("link de conversa rejeita operação de outro projeto sem disparar correção", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.route("**/api/v1/operations/other-operation", (route) =>
    route.fulfill({
      json: {
        ...operation("other-operation", "generation"),
        project_id: "another-project",
        request_text: "Conteúdo de outro projeto",
      },
    }),
  );
  await page.goto(`/app/projetos/${pid}?conversation=other-operation`);
  await expect(page.getByRole("alert")).toContainText(
    "A conversa solicitada não pertence a este projeto.",
  );
  await expect(page.getByText("Conteúdo de outro projeto")).toHaveCount(0);
  expect(state.writes).toHaveLength(0);
});

test("falha ao abrir o link da conversa permite recuperar por GET sem reenviar correção", async ({
  page,
}) => {
  const state = await fixture(page);
  const restored = operation("restored-correction", "generation", {
    kind: "implementation",
    message: "Resultado preservado da correção.",
  });
  restored.state = "succeeded";
  restored.request_text = "Correção já solicitada.";
  state.operations.unshift(restored);
  let lookups = 0;
  await page.route("**/api/v1/operations/restored-correction", (route) => {
    lookups += 1;
    return lookups === 1
      ? route.fulfill({
          status: 503,
          json: {
            error: { message: "A conversa está temporariamente indisponível." },
          },
        })
      : route.fulfill({ json: restored });
  });
  await page.goto(`/app/projetos/${pid}?conversation=${restored.id}`);
  await expect(page.getByRole("alert")).toContainText(
    "temporariamente indisponível",
  );
  await page
    .getByRole("button", { name: "Tentar abrir conversa novamente" })
    .click();
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(chat).toContainText(restored.request_text);
  await expect(chat).toBeFocused();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(lookups).toBe(2);
  expect(state.writes).toHaveLength(0);
});

test("impede envio duplicado e recupera análise aceita com resposta perdida", async ({
  page,
}) => {
  const state = await fixture(page);
  state.lostAnalysisResponse = true;
  state.hiddenAnalysisReads = 1;
  await page.goto(`/app/projetos/${pid}/publicar`);
  const action = page.getByRole("button", {
    name: "Pedir Análise IA",
    exact: true,
  });
  await expect(action).toBeEnabled();
  await action.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(
    page.getByRole("button", { name: "Analisando código…" }),
  ).toBeDisabled();
  expect(state.writes).toHaveLength(1);
  finishAnalysis(state);
  await expect(
    page.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  expect(state.writes).toHaveLength(1);
  await page.reload();
  await page.getByRole("button", { name: "Pedir nova análise IA" }).click();
  await expect.poll(() => state.writes.length).toBe(2);
  expect(state.writes[1].idempotency).not.toBe(state.writes[0].idempotency);
});

test("falha ao iniciar permite tentar novamente e preserva chave idempotente", async ({
  page,
}) => {
  const state = await fixture(page);
  state.analysisFailures = 1;
  await page.goto(`/app/projetos/${pid}/publicar`);
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Não foi possível iniciar a análise agora.",
  );
  await ask(page);
  expect(state.writes).toHaveLength(2);
  expect(state.writes[0].idempotency).toBe(state.writes[1].idempotency);
});

test("nova análise com resposta perdida recupera o novo resultado sem reutilizar diagnóstico antigo", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  const previous = finishAnalysis(state);
  await expect(
    panel.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  state.lostAnalysisResponse = true;
  await panel.getByRole("button", { name: "Pedir nova análise IA" }).click();
  await expect(
    panel.getByRole("button", { name: "Analisando código…" }),
  ).toBeDisabled();
  const current = finishAnalysis(state);
  expect(current.id).not.toBe(previous.id);
  current.result_ref.summary = "Diagnóstico atualizado após nova solicitação.";
  await expect(panel).toContainText(
    "Diagnóstico atualizado após nova solicitação.",
  );
  await expect(panel.getByRole("alert")).toHaveCount(0);
  expect(state.writes).toHaveLength(2);
});

test("consulta antiga atrasada não substitui a nova análise aceita", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  const previous = finishAnalysis(state);
  await expect(
    panel.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  state.heldOperation = { id: previous.id, wait };
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect.poll(() => state.heldReads).toBe(1);
  await panel.getByRole("button", { name: "Pedir nova análise IA" }).click();
  await expect(
    panel.getByRole("button", { name: "Analisando código…" }),
  ).toBeDisabled();
  const latest = finishAnalysis(state);
  latest.result_ref.summary = "Este é o resultado da nova análise.";
  state.heldOperation = null;
  release();
  await expect(panel).toContainText("Este é o resultado da nova análise.");
  await expect(panel).not.toContainText(
    "A declaração de amount está incompleta.",
  );
  expect(state.writes).toHaveLength(2);
});

test("correção falha pode ser repetida sem recobrar análise e com nova chave idempotente", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state);
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect
    .poll(
      () => state.operations.filter((op) => op.type === "generation").length,
    )
    .toBe(1);
  const correction = state.operations.find((op) => op.type === "generation")!;
  correction.state = "failed";
  correction.billing_status = "settled";
  correction.progress_message = "O provedor interrompeu a geração.";
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await page.reload();
  const retry = chat.getByRole("button", { name: "Tentar correção novamente" });
  await expect(retry).toBeEnabled();
  await retry.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect
    .poll(
      () => state.operations.filter((op) => op.type === "generation").length,
    )
    .toBe(2);
  const writes = state.writes.filter((write) =>
    write.path.endsWith("/correction"),
  );
  expect(writes).toHaveLength(2);
  expect(writes[0].idempotency).not.toBe(writes[1].idempotency);
  expect(
    state.operations.filter((op) => op.type === "code_analysis"),
  ).toHaveLength(1);
  expect(state.currentRevision).toBe(rid);
});

test("não solicita correção quando resultado não autoriza mudança ou revisão conflita", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto(`/app/projetos/${pid}/publicar`);
  const panel = await ask(page);
  finishAnalysis(state, false);
  await expect(panel).toContainText("não identificou uma correção de código");
  await expect(
    panel.getByRole("button", { name: "Solicitar correção IA" }),
  ).toHaveCount(0);
  await panel.getByRole("button", { name: "Pedir nova análise IA" }).click();
  await expect
    .poll(
      () => state.operations.filter((op) => op.type === "code_analysis").length,
    )
    .toBe(2);
  finishAnalysis(state);
  state.correctionFailures = 1;
  await panel.getByRole("button", { name: "Solicitar correção IA" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "A revisão mudou. Peça uma nova análise.",
  );
  expect(
    state.operations.filter((op) => op.type === "generation"),
  ).toHaveLength(0);
  expect(state.currentRevision).toBe(rid);
});

test("editor preserva rascunho e impede análise de conteúdo não salvo", async ({
  page,
}) => {
  const state = await fixture(page);
  state.dirty = true;
  await page.goto(`/app/projetos/${pid}`);
  await expect(
    page.getByRole("button", { name: "Pedir Análise IA" }),
  ).toBeDisabled();
  await expect(page.locator("#code-analysis-entry-reason")).toBeVisible();
  await expect(page.locator("#code-analysis-entry-reason")).toContainText(
    "Salve as alterações em uma revisão",
  );
  await expect(page.locator(".ide-panel .code-analysis")).toHaveCount(0);
  expect(state.writes).toHaveLength(0);
});

test("acesso somente leitura não pode disparar análise", async ({ page }) => {
  const state = await fixture(page, ["read"]);
  await page.goto(`/app/projetos/${pid}/publicar`);
  await expect(
    page.getByRole("button", { name: "Pedir Análise IA" }),
  ).toBeDisabled();
  expect(state.writes).toHaveLength(0);
});

test("agente indisponível informa o impedimento sem iniciar operação", async ({
  page,
}) => {
  const state = await fixture(page);
  state.analysisEnabled = false;
  await page.goto(`/app/projetos/${pid}/publicar`);
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "A análise de código está indisponível.",
  );
  expect(state.writes).toHaveLength(0);
});

test("analisa erro da prévia da revisão salva e mantém mensagens de IA como texto", async ({
  page,
}) => {
  const state = await fixture(page);
  state.files["public/index.html"] =
    '<h1>Projeto da análise</h1><script>throw new Error("preview-context-error")</script>';
  await page.goto(`/app/projetos/${pid}`);
  await page.getByRole("tab", { name: "Prévia", exact: true }).click();
  await expect(page.locator(".preview-issues")).toContainText(
    "preview-context-error",
  );
  await page
    .getByRole("button", { name: "Pedir Análise IA", exact: true })
    .click();
  const panel = page.getByRole("region", { name: "Análise de código com IA" });
  await expect.poll(() => state.writes.length).toBe(1);
  await expect(panel).toBeVisible();
  await expect(page.locator(".build-conversation .code-analysis")).toHaveCount(
    1,
  );
  await expect(page.locator(".ide-panel .code-analysis")).toHaveCount(0);
  expect(state.writes[0].body.source).toBe("preview");
  expect(state.writes[0].body.runtime_error).toContain("preview-context-error");
  const op = finishAnalysis(state);
  op.result_ref.summary = '<img src=x onerror="window.analysisInjected=true">';
  await expect(panel).toContainText(
    '<img src=x onerror="window.analysisInjected=true">',
  );
  await expect(panel.locator("img")).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as Window & { analysisInjected?: boolean }).analysisInjected,
    ),
  ).toBeUndefined();
});

test("renovação automática informa andamento sem remover validação do botão publicar", async ({
  page,
}) => {
  const state = await fixture(page);
  state.canPublish = false;
  state.automatic = {
    enabled: true,
    state: "running",
    message: "A verificação automática está em andamento.",
    next_attempt_at: null,
    last_attempt_at: "2026-10-29T10:00:00Z",
    last_succeeded_at: null,
    last_error_code: null,
    consecutive_failures: 0,
  };
  await page.clock.install();
  await page.goto(`/app/projetos/${pid}/publicar`);
  await expect(
    page.getByText("A verificação automática está em andamento.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeDisabled();
  state.canPublish = true;
  await page.clock.fastForward(15000);
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão" }),
  ).toBeEnabled();
  expect(state.writes).toHaveLength(0);
});

test("diagnóstico IA funciona por teclado em telas pequenas e sem falhas de acessibilidade", async ({
  page,
}, info) => {
  const state = await fixture(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`/app/projetos/${pid}/publicar`);
  const action = page.getByRole("button", {
    name: "Pedir Análise IA",
    exact: true,
  });
  await expect(action).toBeEnabled();
  await action.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => state.writes.length).toBe(1);
  finishAnalysis(state);
  const panel = page.getByRole("region", { name: "Análise de código com IA" });
  await expect(
    panel.getByRole("heading", { name: "Diagnóstico da IA" }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "Solicitar correção IA" }),
  ).toBeEnabled();
  const results = await new AxeBuilder({ page })
    .include(".code-analysis")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement)
      document.activeElement.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({
    path: info.outputPath("code-analysis-mobile.png"),
    fullPage: true,
  });
  for (const width of [320, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: info.outputPath("code-analysis-desktop.png"),
    fullPage: true,
  });
});

function clarificationScenario(
  state: Awaited<ReturnType<typeof fixture>>,
  replies: Operation[] = [],
) {
  const question = operation("correction-question", "generation", {
    kind: "clarification",
    message: "Posso remover apenas o arquivo obsoleto durante a correção?",
  });
  question.state = "succeeded";
  question.billing_status = "settled";
  question.analysis_id = "saved-analysis";
  question.analysis_revision_id = rid;
  question.created_at = "2026-10-29T14:40:00Z";
  question.request_text = "Corrija o problema encontrado na publicação.";
  state.operations.unshift(...replies, question);
  return question;
}

function failedClarification(id: string, created: string) {
  const reply = operation(id, "generation");
  reply.state = "failed";
  reply.billing_status = "settled";
  reply.analysis_id = "saved-analysis";
  reply.analysis_revision_id = rid;
  reply.reply_to = "correction-question";
  reply.request_text = "Sim, remova somente o arquivo obsoleto.";
  reply.created_at = created;
  reply.error_code = "GENERATION_DELETE_MISMATCH";
  reply.progress_message =
    "A exclusão proposta não corresponde ao código desta revisão. Sua resposta e o código salvo foram preservados.";
  return reply;
}

test("resposta recebida com falha mantém somente a última retomada e informa o estado correto", async ({
  page,
}, info) => {
  const state = await fixture(page);
  const first = failedClarification("reply-first", "2026-10-29T14:41:00Z");
  const last = failedClarification("reply-last", "2026-10-29T14:42:00Z");
  clarificationScenario(state, [last, first]);
  await page.goto(`/app/projetos/${pid}?conversation=${last.id}`);
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  const question = chat.locator('[data-operation-id="correction-question"]');
  await expect(question).toContainText("Resposta recebida");
  await expect(question).not.toContainText("Aguardando sua resposta");
  await expect(
    question.getByRole("button", { name: "Responder", exact: true }),
  ).toHaveCount(0);
  await expect(
    chat.getByRole("button", { name: "Tentar responder novamente" }),
  ).toHaveCount(1);
  await expect(chat.locator('[data-operation-id="reply-first"]')).toContainText(
    "Esta pergunta já possui uma continuação nesta conversa.",
  );
  await chat
    .locator('[data-operation-id="reply-last"]')
    .getByText("Detalhes da falha")
    .click();
  await expect(chat).toContainText("GENERATION_DELETE_MISMATCH");
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 950 });
    await showConversation(page);
    await expect(
      chat.getByRole("button", { name: "Tentar responder novamente" }),
    ).toBeEnabled();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    if (width === 375 || width === 1440)
      await page.screenshot({
        path: info.outputPath(`correction-followup-${width}.png`),
        fullPage: true,
      });
  }
  const audit = await new AxeBuilder({ page })
    .include(".conversation-panel")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(audit.violations).toEqual([]);
  await chat
    .getByRole("button", { name: "Tentar responder novamente" })
    .click();
  await expect(page.getByLabel("Sua resposta ao agente")).toHaveValue(
    last.request_text!,
  );
  await page
    .getByRole("button", { name: "Enviar resposta", exact: true })
    .click();
  await expect
    .poll(
      () => state.writes.filter((item) => item.path.endsWith("/reply")).length,
    )
    .toBe(1);
  await expect(
    chat.getByRole("button", { name: "Tentar responder novamente" }),
  ).toHaveCount(0);
  expect(
    state.writes.find((item) => item.path.endsWith("/reply")),
  ).toMatchObject({
    path: `/projects/${pid}/messages/correction-question/reply`,
    body: { message_text: last.request_text, base_project_revision: rid },
  });
  expect(
    state.writes.filter((item) => item.path.endsWith("/correction")),
  ).toHaveLength(0);
});

test("continuação concluída remove a retomada da resposta que falhou anteriormente", async ({
  page,
}) => {
  const state = await fixture(page);
  const failed = failedClarification("reply-first", "2026-10-29T14:41:00Z");
  const completed = operation("reply-completed", "generation", {
    kind: "implementation",
    candidate: true,
    revision_id: "candidate-revision",
    changed_files: [{ path: "src/index.ts", change: "modified" }],
  });
  completed.state = "succeeded";
  completed.billing_status = "settled";
  completed.reply_to = "correction-question";
  completed.analysis_id = "saved-analysis";
  completed.analysis_revision_id = rid;
  completed.created_at = "2026-10-29T14:42:00Z";
  completed.request_text = failed.request_text;
  clarificationScenario(state, [completed, failed]);
  await page.goto(`/app/projetos/${pid}?conversation=${failed.id}`);
  const chat = page.getByRole("region", { name: "Conversa de construção" });
  await expect(
    chat.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByRole("button", { name: "Tentar responder novamente" }),
  ).toHaveCount(0);
  await expect(
    chat.locator('[data-operation-id="correction-question"]'),
  ).toContainText("Resposta enviada");
  expect(state.writes).toHaveLength(0);
});

for (const blocker of ["revision", "permission", "draft"] as const) {
  test(`retomada de esclarecimento respeita bloqueio de ${blocker}`, async ({
    page,
  }) => {
    const actions =
      blocker === "permission" ? ["read"] : ["read", "write", "publish"];
    const state = await fixture(page, actions);
    const failed = failedClarification("reply-blocked", "2026-10-29T14:41:00Z");
    clarificationScenario(state, [failed]);
    if (blocker === "revision") state.currentRevision = "changed-revision";
    if (blocker === "draft") state.dirty = true;
    await page.goto(`/app/projetos/${pid}?conversation=${failed.id}`);
    const chat = page.getByRole("region", { name: "Conversa de construção" });
    await expect(
      chat.getByRole("button", { name: "Tentar responder novamente" }),
    ).toBeDisabled();
    await expect(chat).toContainText(
      blocker === "revision"
        ? "A revisão mudou. Peça uma nova análise"
        : blocker === "permission"
          ? "Seu acesso não permite solicitar uma correção."
          : "Salve as alterações da revisão antes de solicitar outra correção.",
    );
    expect(state.writes).toHaveLength(0);
  });
}

test("pergunta inicial da correção explica o bloqueio quando a revisão mudou", async ({
  page,
}, info) => {
  const state = await fixture(page);
  const question = clarificationScenario(state);
  state.currentRevision = "changed-revision";
  await page.goto(`/app/projetos/${pid}?conversation=${question.id}`);
  const turn = page.locator(`[data-operation-id="${question.id}"]`);
  await expect(
    turn.getByRole("button", { name: "Responder", exact: true }),
  ).toBeDisabled();
  await expect(turn).toContainText(
    "A revisão mudou. Peça uma nova análise do código salvo antes de solicitar outra correção.",
  );
  for (const width of [375, 1440]) {
    await page.setViewportSize({ width, height: 950 });
    await showConversation(page);
    await turn.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: info.outputPath(`correction-question-blocked-${width}.png`),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
  expect(state.writes).toHaveLength(0);
});
