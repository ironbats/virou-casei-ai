import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const fixtures = [
  {
    path: "public/modal.ts",
    before: null,
    after:
      'const button: string = "Entrar";\nfunction openModal() { return true; }\n',
  },
  {
    path: "src/auth.py",
    before: "def login():\n    return False\n",
    after: "def login():\n    # proposta de teste\n    return True\n",
  },
  {
    path: "public/index.html",
    before: "<h1>Home</h1>\n",
    after:
      '<h1>Home</h1>\n<button onclick="window.hostile = true">Entrar</button>\n<script>window.hostile = true</script>\n',
  },
  {
    path: "public/style.css",
    before: "button { color: black; }\n",
    after: "button { color: navy; padding: 12px; }\n",
  },
  {
    path: "migrations/0002.sql",
    before: "SELECT id FROM users;\n",
    after: "SELECT id, name FROM users WHERE active = true;\n",
  },
  {
    path: "config.json",
    before: '{"enabled":false}\n',
    after: '{"enabled":true,"count":2}\n',
  },
  {
    path: "notes.unknown",
    before: "texto anterior\n",
    after: "texto seguro com {{chaves}}\n",
  },
  { path: "public/obsolete.txt", before: "conteúdo removido\n", after: null },
  {
    path: "public/long.js",
    before: Array.from(
      { length: 1600 },
      (_, index) => `const original${index} = ${index};\n`,
    ).join(""),
    after: Array.from(
      { length: 1600 },
      (_, index) => `const novo${index} = ${index};\n`,
    ).join(""),
  },
];

async function open(page: Page, query = "") {
  await page.route(
    "**/api/v1/projects/fixture-project/proposals/fixture-proposal/files/*",
    async (route) => {
      const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
      const file = fixtures[Number(id)];
      await route.fulfill({
        json: {
          id,
          ...file,
          proposal_id: "fixture-proposal",
          proposal_version: "synthetic-version",
          change: id === "0" ? "created" : id === "7" ? "deleted" : "modified",
          before_hash: null,
          after_hash: null,
          before_bytes: 0,
          after_bytes: 0,
          content_type: "text",
        },
      });
    },
  );
  await page.goto(`/tests/proposal-review.fixture.html${query}`);
  await page
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Diff de public/modal.ts" }),
  ).toBeVisible();
}

test("revisão exibe um arquivo por vez e aplicar confirma todo o conjunto", async ({
  page,
}) => {
  let payload: unknown,
    calls = 0;
  await page.route("**/proposals/fixture-proposal/apply", async (route) => {
    calls++;
    payload = route.request().postDataJSON();
    await route.fulfill({ json: { revision_id: "candidate-revision" } });
  });
  await open(page);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Testes do projeto gerado não executados");
  await expect(dialog.getByRole("region", { name: /^Diff de/ })).toHaveCount(1);
  await dialog.getByRole("button", { name: /public\/index.html/ }).click();
  await expect(
    dialog.getByRole("region", { name: "Diff de public/index.html" }),
  ).toBeVisible();
  await expect(dialog).toContainText("conjunto de 9 arquivo(s)");
  await dialog
    .getByRole("button", { name: "Aplicar alterações revisadas", exact: true })
    .dblclick();
  await expect(dialog).toHaveCount(0);
  expect(calls).toBe(1);
  expect(payload).toEqual({
    expected_proposal_version: "synthetic-version",
    expected_project_revision: "base-revision",
  });
  await expect(page.getByText("Conjunto confirmado")).toBeVisible();
});

test("fecha sem descartar, devolve foco e reabre no mesmo arquivo", async ({
  page,
}) => {
  await open(page);
  await page.getByRole("button", { name: /src\/auth.py/ }).click();
  await expect(
    page.getByRole("region", { name: "Diff de src/auth.py" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Revisar alterações", exact: true }),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Diff de src/auth.py" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Revise as alterações propostas" }),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Descartar proposta", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Confirmar descarte", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continuar revisão" }).click();
  await expect(
    page.getByRole("region", { name: "Diff de src/auth.py" }),
  ).toBeVisible();
});

test("sintaxe é realçada nas linguagens e conteúdo hostil permanece texto", async ({
  page,
}) => {
  await open(page);
  for (const fixture of fixtures.slice(0, 6)) {
    await page
      .getByRole("button", {
        name: new RegExp(fixture.path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      })
      .click();
    const diff = page.getByRole("region", { name: `Diff de ${fixture.path}` });
    await expect(diff).toBeVisible();
    await expect(diff.locator('[class*="tok-"]').first()).toBeVisible();
  }
  expect(
    await page.evaluate(
      () => (window as unknown as { hostile?: boolean }).hostile,
    ),
  ).toBeUndefined();
  await page.getByRole("button", { name: /notes.unknown/ }).click();
  await expect(
    page.getByRole("region", { name: "Diff de notes.unknown" }),
  ).toContainText("texto seguro com {{chaves}}");
  await expect(
    page.getByText("Texto · sem realce", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: /public\/obsolete.txt/ }).click();
  await expect(
    page.getByRole("region", { name: "Diff de public/obsolete.txt" }),
  ).toContainText("conteúdo removido");
  await expect(page.locator(".proposal-diff-line.removed")).toHaveCount(1);
});

test("diff longo é paginado, pesquisável e preserva seleção e rolagem", async ({
  page,
}) => {
  await open(page);
  await page.getByRole("button", { name: /public\/long.js/ }).click();
  await expect(
    page.getByRole("region", { name: "Diff de public/long.js" }),
  ).toBeVisible();
  await expect(
    page.getByText("comparação por blocos completos", { exact: false }),
  ).toBeVisible();
  await expect(page.locator(".proposal-diff-line")).toHaveCount(240);
  await page.getByRole("button", { name: "Próxima página do diff" }).click();
  await expect(
    page.getByRole("combobox", { name: "Página do diff" }),
  ).toHaveValue("1");
  await page
    .getByRole("searchbox", { name: "Buscar texto no arquivo" })
    .fill("novo1599");
  await page.getByRole("button", { name: "Próxima ocorrência" }).click();
  await expect(
    page.getByRole("region", { name: "Diff de public/long.js" }),
  ).toContainText("const novo1599 = 1599;");
  await page.getByRole("checkbox", { name: "Quebrar linhas" }).check();
  await page.getByRole("button", { name: "Voltar", exact: true }).click();
  await page
    .getByRole("button", { name: "Revisar alterações", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Diff de public/long.js" }),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "Quebrar linhas" }),
  ).toBeChecked();
});

test("revisão usa foco acessível, contraste e um único scroll vertical para o diff", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await open(page);
  await expect(
    page.getByRole("heading", { name: "Revise as alterações propostas" }),
  ).toBeFocused();
  const results = await new AxeBuilder({ page })
    .include(".proposal-review")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(results.violations).toEqual([]);
  await page.getByRole("button", { name: /public\/long.js/ }).click();
  const diff = page.getByRole("region", { name: "Diff de public/long.js" });
  await expect(diff).toBeVisible();
  const scroll = await page.evaluate(() => {
    const dialog = document.querySelector(".proposal-review")!;
    return Array.from(dialog.querySelectorAll("*"))
      .filter((element) => {
        const style = getComputedStyle(element);
        return (
          ["auto", "scroll"].includes(style.overflowY) &&
          element.scrollHeight > element.clientHeight + 1
        );
      })
      .map((element) => element.className);
  });
  expect(
    scroll.filter((name) => name.includes("proposal-diff-viewport")),
  ).toHaveLength(1);
  expect(
    scroll.some(
      (name) =>
        name.includes("proposal-line-code") ||
        name.includes("proposal-review-body"),
    ),
  ).toBe(false);
  await page
    .getByRole("combobox", { name: "Modo de comparação" })
    .selectOption("split");
  await expect(page.locator(".proposal-split-line").first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("proposal-desktop.png") });
});

test("mobile e reflow mantêm ações visíveis sem overflow da página", async ({
  page,
  browser,
}, testInfo) => {
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: width < 800 ? 800 : 960 });
    await open(page);
    const apply = page.getByRole("button", {
      name: "Aplicar alterações revisadas",
      exact: true,
    });
    await expect(apply).toBeVisible();
    const box = await apply.boundingBox();
    expect(box!.y + box!.height).toBeLessThanOrEqual(width < 800 ? 800 : 960);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if (width < 800) {
      await expect(
        page.getByRole("combobox", { name: "Arquivo em revisão" }),
      ).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: "Modo de comparação" }),
      ).toHaveCount(0);
    }
    if (width === 320)
      await page.screenshot({
        path: testInfo.outputPath("proposal-mobile.png"),
      });
    await page.getByRole("button", { name: "Voltar", exact: true }).click();
  }
  const zoomedContext = await browser.newContext({
    viewport: { width: 720, height: 480 },
    deviceScaleFactor: 2,
  });
  try {
    const zoomed = await zoomedContext.newPage();
    await open(zoomed);
    const apply = zoomed.getByRole("button", {
      name: "Aplicar alterações revisadas",
      exact: true,
    });
    await expect(apply).toBeVisible();
    const actions = await apply.boundingBox();
    expect(actions!.y + actions!.height).toBeLessThanOrEqual(480);
    const content = await zoomed
      .getByRole("region", { name: "Diff de public/modal.ts" })
      .boundingBox();
    expect(content!.height).toBeGreaterThan(40);
    expect(
      await zoomed.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await zoomed.screenshot({
      path: testInfo.outputPath("proposal-reflow-200.png"),
    });
  } finally {
    await zoomedContext.close();
  }
});

test("conflito impede aplicar sem esconder a proposta", async ({ page }) => {
  await open(page, "?conflict=1");
  await expect(
    page.getByRole("button", {
      name: "Aplicar alterações revisadas",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText("public/index.html");
  await expect(
    page.getByRole("region", { name: "Diff de public/modal.ts" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Comparar revisão atual" }).click();
  await expect(page.getByText("Comparação solicitada")).toBeVisible();
});

test("falha ao ler um arquivo bloqueia aplicação e permite recuperação sem mutações", async ({
  page,
}) => {
  let mutationCount = 0;
  page.on("request", (request) => {
    if (request.method() === "POST") mutationCount++;
  });
  await open(page);
  await page.route(
    "**/proposals/fixture-proposal/files/1?*",
    async (route) => {
      await route.fulfill({
        status: 503,
        json: {
          error: {
            code: "TEMPORARY",
            message: "Leitura temporariamente indisponível",
          },
        },
      });
    },
    { times: 1 },
  );
  await page.getByRole("button", { name: /src\/auth.py/ }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Leitura temporariamente indisponível",
  );
  const apply = page.getByRole("button", {
    name: "Aplicar alterações revisadas",
    exact: true,
  });
  await expect(apply).toBeDisabled();
  await page.getByRole("button", { name: "Tentar carregar arquivo" }).click();
  await expect(
    page.getByRole("region", { name: "Diff de src/auth.py" }),
  ).toBeVisible();
  await expect(apply).toBeEnabled();
  await page.getByRole("button", { name: /public\/modal.ts/ }).click();
  await expect(
    page.getByRole("region", { name: "Diff de public/modal.ts" }),
  ).toBeVisible();
  await expect(apply).toBeEnabled();
  expect(mutationCount).toBe(0);
});

test("proposta distingue migrations preservadas e novas antes da aplicação", async ({
  page,
}) => {
  await open(page, "?migration-history=1");
  const history = page.locator(".proposal-migration-history");
  await expect(history).toContainText("1 preservada(s) · 1 nova(s)");
  await history.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("region", { name: "Migrations preservadas" }),
  ).toContainText("migrations/0001.sql");
  await expect(
    page.getByRole("region", { name: "Novas migrations" }),
  ).toContainText("migrations/0002.sql");
  await expect(history).toContainText(
    "O SQL só será enviado ao banco durante a publicação.",
  );
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const button = page.getByRole("button", {
    name: "Aplicar alterações revisadas",
    exact: true,
  });
  await expect(button).toBeVisible();
  expect(
    (await button.boundingBox())!.y + (await button.boundingBox())!.height,
  ).toBeLessThanOrEqual(900);
});

test("rejeição do histórico apresenta arquivo, operação e revisão-base sem esconder a proposta", async ({
  page,
}) => {
  await open(page, "?migration-error=1");
  const diagnostic = page.locator(".migration-history-error");
  await expect(diagnostic).toContainText("base-revision");
  await expect(diagnostic).toContainText("migrations/0001.sql");
  await expect(diagnostic).toContainText("Alteração de conteúdo");
  await expect(diagnostic).toContainText("A revisão salva permanece intacta.");
  await expect(
    page.getByRole("region", { name: "Diff de public/modal.ts" }),
  ).toBeVisible();
});
