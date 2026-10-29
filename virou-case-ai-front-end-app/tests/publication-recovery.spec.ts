import fs from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { Operation } from "../src/shared/types";
import {
  recoveryExport,
  recoverySnapshot,
  type RecoverySnapshot,
} from "../src/features/publicationRecovery";
import { productionPolicy } from "./productionPolicy";

const projectId = "publication-recovery-project";
const revisionId = "publication-recovery-revision";
const operationId = "publication-recovery-operation";
const publishedUrl = "https://last-verified.example.workers.dev/";
const recoveryPath = `/operations/${operationId}/publication-recovery`;
const checkedAt = "2026-10-29T12:01:00Z";

function snapshot(): RecoverySnapshot {
  return {
    format_version: 1,
    operation_id: operationId,
    project_id: projectId,
    revision_id: revisionId,
    operation_state: "reconciling",
    recovery_state: "attention",
    can_check: true,
    blocked_code: null,
    blocked_reason: null,
    requested_at: null,
    last_checked_at: null,
    cooldown_until: null,
    operator_required: true,
    classification: "unknown",
    summary: "O resultado da migration ainda não foi confirmado.",
    required_action: "Confira as evidências disponíveis para esta tentativa.",
    missing_evidence: ["Recibo correspondente ao nome e SHA-256 esperados."],
    migration: {
      name: "migrations/0002_add_salt.sql",
      sha256: "a".repeat(64),
      phase: "sql_sending",
      effect_outcome: "unknown",
    },
    checks: {
      identity: "matched",
      source: "matched",
      history: "matched",
      receipt: "absent",
      execution: "unconfirmed",
    },
    diagnostic: {
      initial_error: {
        code: "PUBLICATION_REJECTED",
        message:
          "O banco recusou a solicitação sem confirmar todos os efeitos.",
        provider: {
          status: 400,
          provider_error_codes: [7500],
          statement_index: 2,
        },
      },
      current_error: {
        code: "MIGRATION_RESULT_UNKNOWN",
        message: "O resultado exige comprovação técnica.",
      },
      phase: "sql_sending",
      effect_status: "unknown",
    },
  };
}

function operation(): Operation {
  return {
    id: operationId,
    project_id: projectId,
    type: "publication",
    state: "reconciling",
    progress_stage: "publication_reconciling",
    progress_message: "O resultado da migration ainda não foi confirmado.",
    billing_status: "not_applicable",
    pending_reconciliation: false,
    cancel_requested: false,
    error_code: "PUBLICATION_REJECTED",
    created_at: "2026-10-29T12:00:00Z",
    updated_at: "2026-10-29T12:00:01Z",
    result_ref: {
      revision_id: revisionId,
      publication_diagnostic: {
        version: 1,
        initial_error: snapshot().diagnostic.initial_error,
        phase: "sql_sending",
        effect_status: "unknown",
        migration: snapshot().migration!,
      },
      publication_recovery: {
        version: 2,
        state: "attention",
        message: "A conferência automática foi interrompida.",
        attempts: 3,
        last_attempt_at: "2026-10-29T12:00:00Z",
        next_attempt_at: null,
        last_checked_at: "2026-10-29T12:00:01Z",
        last_error_code: "MIGRATION_RESULT_UNKNOWN",
        required_action: "Conferir o recibo e as evidências da migration.",
        missing_evidence: [
          "Recibo correspondente ao nome e SHA-256 esperados.",
        ],
        owner: "operator",
      },
    },
  };
}

async function fixture(
  page: Page,
  allowedActions = ["read", "write", "publish", "export"],
  developer = false,
) {
  const state = {
    operation: operation(),
    snapshot: developer ? developerSnapshot() : snapshot(),
    currentRevisionId: revisionId,
    readinessBlocked: false,
    allowPublish: false,
    resolveRequests: [] as {
      body: unknown;
      csrf: string;
      idempotency: string;
    }[],
    publications: [] as unknown[],
    resolveGate: null as Promise<void> | null,
    resolveLostResponses: 0,
    reads: 0,
    statusReads: 0,
    readinessReads: 0,
    readFailures: 0,
    readStatus: 503,
    statusFailures: 0,
    checkFailures: 0,
    checkStatus: 503,
    lostResponses: 0,
    nextReadGate: null as Promise<void> | null,
    checkGate: null as Promise<void> | null,
    blockedAfterError: false,
    requests: [] as { body: unknown; csrf: string; idempotency: string }[],
    otherMutations: [] as string[],
    transform: (value: RecoverySnapshot): unknown => value,
    transformCheck: (value: RecoverySnapshot): unknown => value,
  };
  await productionPolicy(page);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    if (request.method() === "POST" && path === recoveryPath + "/resolve") {
      state.resolveRequests.push({
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
        idempotency: request.headers()["idempotency-key"],
      });
      if (state.resolveGate) await state.resolveGate;
      state.snapshot = {
        ...state.snapshot,
        recovery_state: "scheduled",
        state_version: "server-state-accepted",
        capabilities: {
          can_check: false,
          can_check_and_resolve: false,
          can_close_now: false,
        },
        resolution: {
          ...state.snapshot.resolution!,
          eligibility: "evaluating",
          reason_code: "OPERATION_BUSY",
          next_action: { type: "poll", label: "Acompanhar recuperação" },
          blocked_code: "OPERATION_BUSY",
          blocked_reason: "Esta recuperação está em andamento.",
        },
        recovery_request_id: "developer-request-one",
        request: {
          id: "developer-request-one",
          state: "queued",
          requested_at: checkedAt,
          started_at: null,
          completed_at: null,
          reason_code: null,
        },
      };
      if (state.resolveLostResponses > 0) {
        state.resolveLostResponses--;
        return route.abort("failed");
      }
      return route.fulfill({
        status: 202,
        json: state.transformCheck(state.snapshot),
      });
    }
    if (
      request.method() === "POST" &&
      path === `/projects/${projectId}/publications` &&
      state.allowPublish
    ) {
      state.publications.push(request.postDataJSON());
      state.operation = {
        ...operation(),
        id: "new-publication",
        state: "queued",
        progress_stage: "queued",
        result_ref: { revision_id: state.currentRevisionId },
      };
      return route.fulfill({ status: 202, json: state.operation });
    }
    if (request.method() === "POST" && path === recoveryPath + "/check") {
      state.requests.push({
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
        idempotency: request.headers()["idempotency-key"],
      });
      if (state.checkGate) await state.checkGate;
      if (state.checkFailures > 0) {
        state.checkFailures--;
        if (state.blockedAfterError) {
          state.snapshot.can_check = false;
          state.snapshot.cooldown_until = "2030-10-29T12:02:00Z";
          state.snapshot.blocked_reason =
            "Aguarde o intervalo de conferência antes de repetir.";
        }
        return route.fulfill({
          status: state.checkStatus,
          json: {
            error: {
              code: "RECOVERY_CHECK_BLOCKED",
              message:
                state.checkStatus === 403
                  ? "Seu acesso não permite solicitar a conferência."
                  : "A solicitação de conferência não pode ser repetida agora.",
            },
          },
        });
      }
      state.snapshot = {
        ...state.snapshot,
        requested_at: checkedAt,
        recovery_state: "scheduled",
        can_check: false,
        blocked_code: "RECOVERY_CHECK_SCHEDULED",
        blocked_reason: "A conferência desta tentativa já está programada.",
      };
      state.operation = {
        ...state.operation,
        updated_at: checkedAt,
        result_ref: {
          ...state.operation.result_ref,
          publication_recovery: {
            ...state.operation.result_ref.publication_recovery!,
            state: "scheduled",
            message: "A conferência desta tentativa está programada.",
          },
        },
      };
      if (state.lostResponses > 0) {
        state.lostResponses--;
        return route.abort("failed");
      }
      return route.fulfill({
        status: 202,
        json: state.transformCheck(state.snapshot),
      });
    }
    if (request.method() !== "GET") {
      state.otherMutations.push(request.method() + " " + path);
      return route.fulfill({
        status: 409,
        json: {
          error: {
            code: "UNEXPECTED_MUTATION",
            message: "Ação não esperada neste cenário.",
          },
        },
      });
    }
    if (path === "/me")
      return route.fulfill({
        json: {
          id: "recovery-student",
          name: "Aluno de teste",
          login: "recovery-test",
          role: "student",
          csrf_token: "recovery-test-csrf",
          access: {
            status: "active",
            allowed_actions: allowedActions,
            period_id: "recovery-period",
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
    if (path === `/projects/${projectId}`)
      return route.fulfill({
        json: {
          id: projectId,
          name: "Projeto com publicação pendente",
          objective: "Publicar a revisão salva",
          stack: "hono-typescript-d1",
          version: 1,
          current_revision_id: state.currentRevisionId,
          state: "active",
          created_at: "2026-10-28T11:00:00Z",
        },
      });
    if (path === recoveryPath) {
      state.reads++;
      if (state.readFailures > 0) {
        state.readFailures--;
        return route.fulfill({
          status: state.readStatus,
          json: {
            error: {
              code: "RECOVERY_UNAVAILABLE",
              message: "A consulta de recuperação está indisponível.",
            },
          },
        });
      }
      const result = structuredClone(state.transform(state.snapshot));
      const gate = state.nextReadGate;
      state.nextReadGate = null;
      if (gate) await gate;
      return route.fulfill({ json: result });
    }
    if (
      path === `/operations/${operationId}` ||
      path === "/operations/new-publication"
    ) {
      state.statusReads++;
      if (state.statusFailures > 0) {
        state.statusFailures--;
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "STATUS_UNAVAILABLE",
              message: "O acompanhamento está indisponível.",
            },
          },
        });
      }
      return route.fulfill({ json: state.operation });
    }
    if (path === "/operations")
      return route.fulfill({
        json: {
          items:
            url.searchParams.get("kind") === "publication"
              ? [state.operation]
              : [],
          next_cursor: null,
        },
      });
    if (path === `/projects/${projectId}/publication-readiness`) {
      state.readinessReads++;
      return route.fulfill({
        json: {
          can_publish:
            !state.readinessBlocked &&
            ["failed", "succeeded"].includes(state.operation.state),
          revision_id: state.currentRevisionId,
          configuration_version: 1,
          reasons: state.readinessBlocked
            ? [
                {
                  code: "CONFIGURATION_PENDING",
                  message: "A validação da conexão ainda está pendente.",
                },
              ]
            : [],
          publication: {
            url: publishedUrl,
            revision_id: "previous-verified-revision",
            published_at: "2026-10-28T12:00:00Z",
          },
        },
      });
    }
    return route.fulfill({
      status: 404,
      json: {
        error: { code: "NOT_FOUND", message: "Recurso não encontrado." },
      },
    });
  });
  return state;
}

const panel = (page: Page) =>
  page.getByRole("region", { name: "Recuperar publicação", exact: true });
const checkButton = (page: Page) =>
  panel(page).getByRole("button", { name: "Conferir pendência", exact: true });
const open = async (page: Page) => {
  await page.goto(
    `/app/projetos/${projectId}/publicar?operation=${operationId}`,
  );
  await expect(panel(page)).toBeVisible();
  await expect(
    panel(page).getByText("Consultando recuperação…", { exact: true }),
  ).toHaveCount(0);
};

test("consulta e atualização de status não conferem pendências nem iniciam IA ou outra publicação", async ({
  page,
}) => {
  await page.clock.install();
  const state = await fixture(page);
  await open(page);
  await expect(checkButton(page)).toBeEnabled();
  await expect(panel(page)).toContainText("Evidências ainda necessárias");
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
  const reads = state.reads;
  await page.clock.fastForward(45000);
  await expect.poll(() => state.statusReads).toBeGreaterThan(1);
  expect(state.reads).toBe(reads);
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect.poll(() => state.reads).toBeGreaterThan(reads);
  await expect(checkButton(page)).toBeEnabled();
  expect(state.requests).toHaveLength(0);
  expect(state.otherMutations).toEqual([]);
  await expect(panel(page).getByRole("textbox")).toHaveCount(0);
  await expect(
    panel(page).getByRole("button", { name: /aprovar|limpar|adotar|forçar/i }),
  ).toHaveCount(0);
});

test("clique explícito agenda a mesma operação com CSRF, idempotência e proteção contra cliques repetidos", async ({
  page,
}) => {
  const state = await fixture(page);
  let release!: () => void;
  state.checkGate = new Promise((resolve) => {
    release = resolve;
  });
  await open(page);
  await checkButton(page).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => state.requests.length).toBe(1);
  await expect(
    panel(page).getByRole("button", { name: "Solicitando conferência…" }),
  ).toBeDisabled();
  release();
  await expect(panel(page)).toContainText(
    "Conferência solicitada para esta tentativa.",
  );
  await expect(checkButton(page)).toBeDisabled();
  expect(state.requests).toEqual([
    {
      body: {},
      csrf: "recovery-test-csrf",
      idempotency: expect.stringMatching(/^[a-z0-9-]{36}$/i),
    },
  ]);
  expect(state.otherMutations).toEqual([]);
  await expect(page).toHaveURL(new RegExp(`operation=${operationId}$`));
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
});

test("resultado desconhecido continua bloqueado e orienta a confirmação operacional", async ({
  page,
}) => {
  const state = await fixture(page);
  await open(page);
  await checkButton(page).click();
  await expect(panel(page)).toContainText(
    "Conferência solicitada para esta tentativa.",
  );
  state.operation.updated_at = "2026-10-29T12:02:00Z";
  state.operation.result_ref.publication_recovery!.state = "attention";
  state.snapshot = {
    ...state.snapshot,
    recovery_state: "attention",
    last_checked_at: "2026-10-29T12:02:00Z",
    can_check: false,
    blocked_reason: "Falta comprovação suficiente para continuar.",
    required_action:
      "Solicite ao responsável pela plataforma a comprovação técnica desta migration.",
  };
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect(panel(page)).toContainText(
    "Falta comprovação suficiente para continuar.",
  );
  await expect(panel(page)).toContainText("Resultado ainda não confirmado");
  await expect(panel(page)).toContainText(
    "Conferência concluída. Consulte o resultado",
  );
  await expect(panel(page)).toContainText("Exporte o diagnóstico");
  await expect(checkButton(page)).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
  await expect(panel(page)).not.toContainText("Pendência resolvida");
});

test("resolução recebida pelo acompanhamento atualiza diagnóstico e disponibilidade sem recarregar a página", async ({
  page,
}) => {
  await page.clock.install();
  const state = await fixture(page);
  await open(page);
  await checkButton(page).click();
  await expect(checkButton(page)).toBeDisabled();
  await expect.poll(() => state.reads).toBeGreaterThan(1);
  state.operation = {
    ...state.operation,
    state: "failed",
    progress_stage: "publication_failed",
    updated_at: "2026-10-29T12:03:00Z",
    result_ref: {
      ...state.operation.result_ref,
      publication_recovery: {
        ...state.operation.result_ref.publication_recovery!,
        state: "resolved",
      },
    },
  };
  state.snapshot = {
    ...state.snapshot,
    operation_state: "failed",
    recovery_state: "resolved",
    classification: "resolved",
    summary:
      "A ausência de efeitos foi confirmada e a tentativa foi encerrada.",
    required_action: "Confira a disponibilidade antes de um novo envio.",
    operator_required: false,
    missing_evidence: [],
    blocked_reason: "Esta tentativa já foi encerrada.",
    last_checked_at: "2026-10-29T12:03:00Z",
    cooldown_until: "2030-10-29T12:04:00Z",
  };
  await page.clock.fastForward(15000);
  await expect(panel(page)).toContainText("Pendência resolvida");
  await expect(panel(page)).toContainText(
    "A pendência foi resolvida. Confira a disponibilidade",
  );
  await expect(panel(page)).not.toContainText("Próxima solicitação após");
  await expect(panel(page)).not.toContainText(
    "O resultado será atualizado no acompanhamento.",
  );
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeEnabled();
  await expect(checkButton(page)).toBeDisabled();
  expect(state.requests).toHaveLength(1);
  expect(state.otherMutations).toEqual([]);
});

test("404 de versão anterior preserva acompanhamento, diagnóstico original e último link verificado", async ({
  page,
}) => {
  const state = await fixture(page);
  state.readFailures = 100;
  state.readStatus = 404;
  await open(page);
  await expect(panel(page)).toContainText(
    "A recuperação assistida ainda não está disponível",
  );
  await expect(checkButton(page)).toBeDisabled();
  await expect(
    panel(page).getByRole("button", { name: "Exportar diagnóstico" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("region", { name: "Diagnóstico da tentativa" }),
  ).toContainText("PUBLICATION_REJECTED");
  await expect(
    page.getByRole("link", { name: "Abrir publicação" }),
  ).toHaveAttribute("href", publishedUrl);
  state.readFailures = 0;
  await panel(page)
    .getByRole("button", { name: "Consultar recuperação" })
    .click();
  await expect(checkButton(page)).toBeEnabled();
  expect(state.requests).toHaveLength(0);
});

for (const field of [
  "format_version",
  "operation_id",
  "project_id",
  "revision_id",
  "classification",
  "operation_state",
]) {
  test(`snapshot incompatível em ${field} não libera ação nem exportação`, async ({
    page,
  }) => {
    const state = await fixture(page);
    state.transform = (value) => ({
      ...value,
      [field]: field === "format_version" ? 9 : "unexpected-value",
    });
    await open(page);
    await expect(panel(page).getByRole("alert")).toContainText(
      "não pôde ser validado",
    );
    await expect(checkButton(page)).toBeDisabled();
    await expect(
      panel(page).getByRole("button", { name: "Exportar diagnóstico" }),
    ).toBeDisabled();
    expect(state.requests).toHaveLength(0);
  });
}

test("falha de leitura preserva o último diagnóstico e exige leitura válida antes de nova conferência", async ({
  page,
}) => {
  const state = await fixture(page);
  await open(page);
  state.readFailures = 1;
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "A consulta de recuperação está indisponível.",
  );
  await expect(panel(page)).toContainText(state.snapshot.summary);
  await expect(checkButton(page)).toBeDisabled();
  await expect(
    panel(page).getByRole("button", { name: "Exportar diagnóstico" }),
  ).toBeEnabled();
  await panel(page)
    .getByRole("button", { name: "Consultar recuperação" })
    .click();
  await expect(checkButton(page)).toBeEnabled();
  expect(state.requests).toHaveLength(0);
});

test("falha no acompanhamento impede conferência mesmo com snapshot disponível", async ({
  page,
}) => {
  const state = await fixture(page);
  await open(page);
  state.statusFailures = 1;
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect(panel(page)).toContainText(
    "Atualize o acompanhamento da publicação",
  );
  await expect(checkButton(page)).toBeDisabled();
  await page.getByRole("button", { name: "Reconectar acompanhamento" }).click();
  await expect(checkButton(page)).toBeEnabled();
  expect(state.requests).toHaveLength(0);
});

test("resposta perdida é reconciliada por leitura sem repetir o comando", async ({
  page,
}) => {
  const state = await fixture(page);
  state.lostResponses = 1;
  await open(page);
  await checkButton(page).click();
  await expect(panel(page)).toContainText(
    "A solicitação de conferência foi confirmada.",
  );
  await expect(checkButton(page)).toBeDisabled();
  await expect(panel(page).getByRole("alert")).toHaveCount(0);
  expect(state.requests).toHaveLength(1);
  expect(state.otherMutations).toEqual([]);
});

test("repetição explícita após falha sem recebimento reutiliza a chave idempotente", async ({
  page,
}) => {
  const state = await fixture(page);
  state.checkFailures = 1;
  await open(page);
  await checkButton(page).click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "não pode ser repetida agora",
  );
  await expect(checkButton(page)).toBeEnabled();
  await checkButton(page).click();
  await expect(panel(page)).toContainText(
    "Conferência solicitada para esta tentativa.",
  );
  expect(state.requests).toHaveLength(2);
  expect(state.requests[1].idempotency).toBe(state.requests[0].idempotency);
  expect(state.otherMutations).toEqual([]);
});

for (const status of [403, 409, 429]) {
  test(`erro ${status} mantém bloqueio recebido do servidor e não dispara nova tentativa automática`, async ({
    page,
  }) => {
    const state = await fixture(page);
    state.checkFailures = 1;
    state.checkStatus = status;
    state.blockedAfterError = true;
    await open(page);
    await checkButton(page).click();
    await expect(panel(page).getByRole("alert")).toBeVisible();
    await expect(panel(page)).toContainText(
      "Aguarde o intervalo de conferência",
    );
    await expect(checkButton(page)).toBeDisabled();
    expect(state.requests).toHaveLength(1);
    expect(state.otherMutations).toEqual([]);
  });
}

test("acesso sem permissão de publicação pode consultar diagnóstico mas não solicitar conferência", async ({
  page,
}) => {
  const state = await fixture(page, ["read"]);
  await open(page);
  await expect(checkButton(page)).toBeDisabled();
  await expect(panel(page)).toContainText(
    "Seu acesso atual permite consultar o diagnóstico",
  );
  await expect(
    panel(page).getByRole("button", { name: "Exportar diagnóstico" }),
  ).toBeEnabled();
  expect(state.requests).toHaveLength(0);
});

test("exportação usa somente campos conhecidos e mantém os erros inicial e atual separados", async ({
  page,
}, info) => {
  const state = await fixture(page);
  state.transform = (value) => ({
    ...value,
    token: "top-secret-token",
    sql: "PRIVATE SQL",
    cloudflare_account_id: "private-account",
    checks: {
      ...value.checks,
      token: "private-check-token",
      schema: "private-schema-content",
    },
    migration: { ...value.migration, sql: "PRIVATE SQL" },
    diagnostic: {
      ...value.diagnostic,
      password: "private-password",
      initial_error: {
        ...value.diagnostic.initial_error,
        provider: {
          ...value.diagnostic.initial_error?.provider,
          route: "/accounts/private-account/d1/database/private-database/query",
          cf_ray: "private-request-id",
          message: "PRIVATE SQL",
          authorization: "Bearer secret",
        },
      },
    },
  });
  await open(page);
  const downloading = page.waitForEvent("download");
  await panel(page)
    .getByRole("button", { name: "Exportar diagnóstico" })
    .click();
  const download = await downloading;
  const file = info.outputPath(download.suggestedFilename());
  await download.saveAs(file);
  const raw = await fs.readFile(file, "utf8");
  const exported = JSON.parse(raw);
  expect(exported.export_format).toBe("viroucaseai_publication_recovery_v1");
  expect(exported.operation_id).toBe(operationId);
  expect(exported.diagnostic.initial_error.code).toBe("PUBLICATION_REJECTED");
  expect(exported.diagnostic.current_error.code).toBe(
    "MIGRATION_RESULT_UNKNOWN",
  );
  expect(exported.diagnostic.initial_error.provider.status).toBe(400);
  expect(raw).not.toMatch(
    /PRIVATE SQL|private-account|private-database|private-password|private-request-id|private-check-token|private-schema-content|Bearer secret|top-secret-token/,
  );
  expect(state.requests).toHaveLength(0);
  expect(state.otherMutations).toEqual([]);
});

test("leitura atrasada iniciada durante o envio não reabre uma conferência já agendada", async ({
  page,
}) => {
  const state = await fixture(page);
  let releaseCheck!: () => void;
  let releaseRead!: () => void;
  state.checkGate = new Promise((resolve) => {
    releaseCheck = resolve;
  });
  await open(page);
  await checkButton(page).click();
  await expect.poll(() => state.requests.length).toBe(1);
  state.nextReadGate = new Promise((resolve) => {
    releaseRead = resolve;
  });
  const reads = state.reads;
  await page
    .getByRole("button", { name: "Atualizar status", exact: true })
    .click();
  await expect.poll(() => state.reads).toBeGreaterThan(reads);
  releaseCheck();
  await expect(panel(page)).toContainText(
    "Conferência solicitada para esta tentativa.",
  );
  await expect(panel(page)).toContainText(
    "A conferência desta tentativa já está programada.",
  );
  releaseRead();
  await expect(checkButton(page)).toBeDisabled();
  expect(state.requests).toHaveLength(1);
});

test("resposta do comando incompatível exige consulta válida e não afirma sucesso", async ({
  page,
}) => {
  const state = await fixture(page);
  state.transformCheck = (value) => ({
    ...value,
    operation_id: "other-operation",
  });
  state.transform = (value) =>
    value.requested_at ? { ...value, format_version: 2 } : value;
  await open(page);
  await checkButton(page).click();
  await expect(panel(page)).toContainText(
    "A resposta da conferência não pôde ser validada.",
  );
  await expect(checkButton(page)).toBeDisabled();
  await expect(panel(page)).not.toContainText(
    "Conferência solicitada para esta tentativa.",
  );
  expect(state.requests).toHaveLength(1);
});

test("validação rejeita contratos incompletos e dados não finitos antes da exportação", () => {
  expect(
    recoverySnapshot(
      { ...snapshot(), can_check: "true" },
      operationId,
      projectId,
      revisionId,
    ),
  ).toBeNull();
  expect(
    recoverySnapshot(
      { ...snapshot(), cooldown_until: "invalid" },
      operationId,
      projectId,
      revisionId,
    ),
  ).toBeNull();
  expect(
    recoverySnapshot(
      {
        ...snapshot(),
        migration: { name: "migrations/0002.sql", sha256: "bad" },
      },
      operationId,
      projectId,
      revisionId,
    ),
  ).toBeNull();
  expect(
    recoverySnapshot(
      { ...snapshot(), missing_evidence: [5] },
      operationId,
      projectId,
      revisionId,
    ),
  ).toBeNull();
  expect(
    recoverySnapshot(
      { ...snapshot(), checks: { receipt: {} } },
      operationId,
      projectId,
      revisionId,
    ),
  ).toBeNull();
  const valid = recoverySnapshot(
    snapshot(),
    operationId,
    projectId,
    revisionId,
  );
  expect(valid).not.toBeNull();
  expect(JSON.parse(recoveryExport(valid!)).diagnostic.current_error.code).toBe(
    "MIGRATION_RESULT_UNKNOWN",
  );
  const extended = {
    ...snapshot(),
    token: "private-export-token",
    checks: {
      receipt: "absent",
      schema: "private-schema-content",
      token: "private-check-token",
    },
  };
  expect(recoveryExport(extended)).not.toMatch(
    /private-export-token|private-schema-content|private-check-token/,
  );
});

test("fim do intervalo consulta a disponibilidade uma vez sem agendar outra conferência", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-29T12:00:00Z") });
  const state = await fixture(page);
  state.snapshot = {
    ...state.snapshot,
    can_check: false,
    blocked_code: "RECOVERY_COOLDOWN",
    blocked_reason:
      "Aguarde o intervalo indicado antes de solicitar outra conferência.",
    cooldown_until: "2026-10-29T12:01:00Z",
  };
  await open(page);
  await expect(checkButton(page)).toBeDisabled();
  const reads = state.reads;
  state.snapshot = {
    ...state.snapshot,
    can_check: true,
    blocked_code: null,
    blocked_reason: null,
  };
  await page.clock.fastForward(61000);
  await expect(checkButton(page)).toBeEnabled();
  expect(state.reads).toBe(reads + 1);
  await page.clock.fastForward(61000);
  expect(state.reads).toBe(reads + 1);
  expect(state.requests).toHaveLength(0);
  expect(state.otherMutations).toEqual([]);
});

test("painel é acessível, mantém o tema e adapta controles em 320–1440px e zoom de 200%", async ({
  page,
}, info) => {
  await fixture(page);
  await open(page);
  await panel(page).getByText("Dados do diagnóstico", { exact: true }).click();
  const violations = await new AxeBuilder({ page })
    .include(".publication-recovery")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(violations.violations).toEqual([]);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await expect(checkButton(page)).toBeVisible();
    const layout = await page.evaluate(() => {
      const panel = document.querySelector<HTMLElement>(
        ".publication-recovery",
      )!;
      return {
        overflow:
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth + 1,
        buttons: [...panel.querySelectorAll("button")].map(
          (button) => button.getBoundingClientRect().height,
        ),
        primary: getComputedStyle(panel.querySelector("button.primary")!)
          .backgroundColor,
      };
    });
    expect(layout.overflow, `overflow at ${width}`).toBe(false);
    expect(layout.buttons.every((height) => height >= 44)).toBe(true);
    expect(layout.primary).toBe("rgb(16, 38, 75)");
  }
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth + 1,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "1";
  });
  await page.setViewportSize({ width: 1024, height: 1100 });
  await panel(page).getByText("Dados do diagnóstico", { exact: true }).click();
  await panel(page).screenshot({
    path: info.outputPath("publication-recovery-desktop.png"),
  });
  await page.setViewportSize({ width: 375, height: 900 });
  await panel(page).screenshot({
    path: info.outputPath("publication-recovery-mobile.png"),
  });
});

function developerSnapshot(): RecoverySnapshot {
  return {
    ...snapshot(),
    classification: "schema_unchanged",
    capabilities: {
      can_check: true,
      can_check_and_resolve: true,
      can_close_now: false,
    },
    resolution: {
      mode: "developer_no_effect",
      action: "close_no_effect",
      eligibility: "requires_evaluation",
      reason_code: "PROOF_NOT_YET_EVALUATED",
      missing_evidence: [],
      next_action: { type: "check_and_resolve", label: "Conferir e resolver" },
      blocked_code: null,
      blocked_reason: null,
      cooldown_until: null,
    },
    state_version: "server-state-original",
    checked_at: null,
    recovery_request_id: null,
    request: null,
  };
}

function finishDeveloperRecovery(state: Awaited<ReturnType<typeof fixture>>) {
  state.operation = {
    ...state.operation,
    state: "failed",
    progress_stage: "publication_failed",
    updated_at: "2026-10-29T12:03:00Z",
    result_ref: {
      ...state.operation.result_ref,
      publication_recovery: {
        ...state.operation.result_ref.publication_recovery!,
        state: "resolved",
      },
    },
  };
  state.snapshot = {
    ...state.snapshot,
    operation_state: "failed",
    recovery_state: "resolved",
    classification: "resolved",
    state_version: "server-state-finished",
    last_checked_at: "2026-10-29T12:03:00Z",
    resolution: {
      ...state.snapshot.resolution!,
      eligibility: "resolved",
      reason_code: "RECOVERY_ALREADY_RESOLVED",
      missing_evidence: [],
      next_action: {
        type: "review_readiness",
        label: "Conferir revisão atual",
      },
      blocked_code: "RECOVERY_ALREADY_RESOLVED",
      blocked_reason: "Esta tentativa foi encerrada.",
    },
    request: {
      ...state.snapshot.request!,
      state: "resolved",
      completed_at: "2026-10-29T12:03:00Z",
      reason_code: "RECOVERY_ALREADY_RESOLVED",
    },
  };
}

const resolveButton = (page: Page) =>
  panel(page).getByRole("button", { name: "Conferir e resolver", exact: true });

test("capacidade específica autoriza o comando único com consentimento contextual, CSRF e sem segunda confirmação", async ({
  page,
}) => {
  const state = await fixture(
    page,
    ["read", "recover_own_publication_no_effect"],
    true,
  );
  let release!: () => void;
  state.resolveGate = new Promise((resolve) => {
    release = resolve;
  });
  await open(page);
  await expect(resolveButton(page)).toBeEnabled();
  await expect(panel(page)).toContainText(
    "Ao continuar, verificamos as evidências e resolvemos a pendência quando for seguro. Nenhum comando de banco será repetido.",
  );
  await resolveButton(page).evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  await expect.poll(() => state.resolveRequests.length).toBe(1);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.resolveRequests[0]).toEqual({
    body: {
      expected_state_version: "server-state-original",
      action: "check_and_close_if_no_effect",
      consent: "verify_and_close_failed_attempt_when_proven_safe",
    },
    csrf: "recovery-test-csrf",
    idempotency: expect.stringMatching(/^[a-z0-9-]{36}$/i),
  });
  release();
  await expect(
    panel(page).getByRole("button", {
      name: "Verificando evidências…",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.requests).toHaveLength(0);
  expect(state.otherMutations).toEqual([]);
});

test("publish sozinho não habilita a recuperação sem capacidade resolutiva do servidor", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  state.snapshot.capabilities!.can_check_and_resolve = false;
  state.snapshot.resolution!.blocked_code = "ACCESS_DENIED";
  state.snapshot.resolution!.blocked_reason =
    "Seu acesso não permite recuperar esta publicação.";
  await open(page);
  await expect(resolveButton(page)).toBeDisabled();
  await expect(panel(page)).toContainText("Seu acesso não permite recuperar");
  expect(state.resolveRequests).toHaveLength(0);
});

test("resolução pública atualiza prontidão, preserva fases reais e link antigo, e exige publicação separada da revisão atual", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  state.currentRevisionId = "current-saved-revision-v3";
  state.allowPublish = true;
  state.operation.result_ref.publication_failure = {
    code: "PUBLICATION_REJECTED",
    message: "A migration foi recusada.",
    stage: "publication_migrating",
  };
  await open(page);
  await resolveButton(page).click();
  await expect.poll(() => state.resolveRequests.length).toBe(1);
  state.snapshot.request!.state = "resolving";
  state.snapshot.resolution!.eligibility = "eligible";
  await expect(
    panel(page).getByRole("button", {
      name: "Recuperando tentativa…",
      exact: true,
    }),
  ).toBeDisabled();
  finishDeveloperRecovery(state);
  await expect(panel(page)).toContainText(
    "Pendência encerrada. Nenhuma migration foi reaplicada.",
  );
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeEnabled();
  const phases = page.getByRole("list", {
    name: "Etapas da publicação",
    exact: true,
  });
  await expect(
    phases.getByRole("listitem").filter({ hasText: "Enviar à hospedagem" }),
  ).toContainText("Não iniciada");
  await expect(
    phases.getByRole("listitem").filter({ hasText: "Verificar publicação" }),
  ).toContainText("Não iniciada");
  await expect(
    page.getByRole("link", { name: "Abrir publicação", exact: true }),
  ).toHaveAttribute("href", publishedUrl);
  expect(state.publications).toHaveLength(0);
  await page
    .getByRole("button", { name: "Publicar esta revisão", exact: true })
    .click();
  await expect.poll(() => state.publications.length).toBe(1);
  expect(state.publications[0]).toEqual({
    revision_id: "current-saved-revision-v3",
  });
  expect(state.resolveRequests).toHaveLength(1);
  expect(state.otherMutations).toEqual([]);
});

test("reload acompanha a solicitação aceita e não reenvia comando resolutivo", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  await open(page);
  await resolveButton(page).click();
  await expect.poll(() => state.resolveRequests.length).toBe(1);
  await page.reload();
  await expect(
    panel(page).getByRole("button", {
      name: "Verificando evidências…",
      exact: true,
    }),
  ).toBeDisabled();
  const reads = state.reads;
  await expect.poll(() => state.reads).toBeGreaterThan(reads);
  expect(state.resolveRequests).toHaveLength(1);
  finishDeveloperRecovery(state);
  await expect(panel(page)).toContainText(
    "Pendência encerrada. Nenhuma migration foi reaplicada.",
  );
  expect(state.resolveRequests).toHaveLength(1);
});

test("DML incerto mostra prova ausente e exportação sem oferecer repetição em loop", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  await open(page);
  await resolveButton(page).click();
  await expect.poll(() => state.resolveRequests.length).toBe(1);
  state.snapshot.request!.state = "attention";
  state.snapshot.request!.completed_at = "2026-10-29T12:02:00Z";
  state.snapshot.resolution = {
    ...state.snapshot.resolution!,
    eligibility: "ineligible",
    reason_code: "DML_UNCERTAIN",
    missing_evidence: [
      "Não há comprovação de rollback dos comandos de alteração de dados.",
    ],
    next_action: { type: "export_diagnostic", label: "Exportar diagnóstico" },
    blocked_code: "DML_UNCERTAIN",
    blocked_reason: "O efeito sobre dados não foi confirmado.",
  };
  await expect(resolveButton(page)).toBeDisabled();
  await expect(panel(page)).toContainText("Não há comprovação de rollback");
  await expect(panel(page)).toContainText(
    "A estrutura do banco, sozinha, não comprova ausência de efeitos.",
  );
  await expect(
    panel(page).getByRole("button", {
      name: "Exportar diagnóstico",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
  expect(state.resolveRequests).toHaveLength(1);
});

test("encerrar a pendência não ignora outros bloqueios de prontidão", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  state.readinessBlocked = true;
  await open(page);
  await resolveButton(page).click();
  await expect.poll(() => state.resolveRequests.length).toBe(1);
  finishDeveloperRecovery(state);
  await expect(panel(page)).toContainText("Pendência encerrada.");
  await expect(
    page.getByRole("button", { name: "Publicar esta revisão", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("#publication-reason")).toContainText(
    "A validação da conexão ainda está pendente.",
  );
  expect(state.publications).toHaveLength(0);
});

test("ver evidências abre e foca os dados; cabeçalho IA somente navega; V3 mantém cards e acessibilidade", async ({
  page,
}, info) => {
  const state = await fixture(page, undefined, true);
  state.currentRevisionId = "current-saved-revision-v3";
  await open(page);
  const evidence = panel(page).getByRole("button", {
    name: "Ver evidências",
    exact: true,
  });
  await evidence.focus();
  await page.keyboard.press("Enter");
  await expect(evidence).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#publication-recovery-evidence")).toBeFocused();
  await page.getByRole("button", { name: "Análise IA", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Análise IA da publicação", exact: true }),
  ).toBeFocused();
  expect(state.otherMutations).toEqual([]);
  await expect(
    page.getByRole("region", { name: "Diagnóstico da tentativa" }),
  ).toContainText("Conferir e resolver neste projeto");
  const violations = await new AxeBuilder({ page })
    .include(".publication-page")
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(violations.violations).toEqual([]);
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth + 1,
      ),
    ).toBe(true);
    await expect(resolveButton(page)).toBeVisible();
  }
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.screenshot({
    path: info.outputPath("publication-recovery-v3-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 375, height: 900 });
  await page.screenshot({
    path: info.outputPath("publication-recovery-v3-mobile.png"),
    fullPage: true,
  });
});

test("contrato resolutivo incompleto ou ação diferente não habilita comando", () => {
  const base = developerSnapshot();
  for (const value of [
    { ...base, state_version: "" },
    {
      ...base,
      capabilities: { ...base.capabilities, can_check_and_resolve: "true" },
    },
    { ...base, resolution: { ...base.resolution, action: "adopt_schema" } },
    { ...base, request: { id: "mismatched", state: "running" } },
  ])
    expect(
      recoverySnapshot(value, operationId, projectId, revisionId),
    ).toBeNull();
  expect(
    recoverySnapshot(base, operationId, projectId, revisionId)?.resolution
      ?.action,
  ).toBe("close_no_effect");
});

test("resposta resolutiva perdida é recuperada pelo identificador persistido sem reenviar nem depender do check legado", async ({
  page,
}) => {
  const state = await fixture(page, undefined, true);
  state.resolveLostResponses = 1;
  await open(page);
  await resolveButton(page).click();
  await expect(panel(page)).toContainText(
    "A solicitação de recuperação foi confirmada.",
  );
  await expect(panel(page).getByRole("alert")).toHaveCount(0);
  await expect(
    panel(page).getByRole("button", {
      name: "Verificando evidências…",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.snapshot.requested_at).toBeNull();
  expect(state.resolveRequests).toHaveLength(1);
  expect(state.requests).toHaveLength(0);
});

test("cooldown próprio da recuperação permite nova avaliação só após consulta sem POST automático", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-29T12:00:00Z") });
  const state = await fixture(page, undefined, true);
  state.snapshot.capabilities!.can_check_and_resolve = false;
  state.snapshot.resolution = {
    ...state.snapshot.resolution!,
    eligibility: "ineligible",
    reason_code: "REMOTE_UNAVAILABLE",
    blocked_code: "RECOVERY_COOLDOWN",
    blocked_reason: "Aguarde o intervalo antes de tentar novamente.",
    cooldown_until: "2026-10-29T12:01:00Z",
  };
  await open(page);
  await expect(resolveButton(page)).toBeDisabled();
  await expect(panel(page)).toContainText("Próxima solicitação após");
  const reads = state.reads;
  state.snapshot.capabilities!.can_check_and_resolve = true;
  state.snapshot.resolution.blocked_code = null;
  state.snapshot.resolution.blocked_reason = null;
  await page.clock.fastForward(61000);
  await expect(resolveButton(page)).toBeEnabled();
  expect(state.reads).toBeGreaterThan(reads);
  expect(state.resolveRequests).toHaveLength(0);
  expect(state.requests).toHaveLength(0);
});
