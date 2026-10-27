import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { productionPolicy } from "./productionPolicy";
import { workspace } from "./workspaceHelper";

test.skip(
  process.env.VIRA_E2E_WORKSPACE !== "true",
  "Use backend/script/test-workspace.py.",
);
test.beforeEach(async ({ page }) => {
  await productionPolicy(page);
});

async function draft(
  page: Page,
  projectId: string,
  changes: Record<string, string>,
) {
  await page.evaluate(
    async ({ projectId, changes }) => {
      const me = await (await fetch("/api/v1/me")).json();
      const path = `/api/v1/projects/${projectId}/draft`;
      const draft = await (await fetch(path)).json();
      const revision = await (
        await fetch(
          `/api/v1/projects/${projectId}/revisions/${draft.data.base_revision_id}`,
        )
      ).json();
      const entries = await Promise.all(
        revision.files.map(async (file: { id: string }) =>
          (
            await fetch(
              `/api/v1/projects/${projectId}/revisions/${draft.data.base_revision_id}/files/${file.id}`,
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
      const response = await fetch(path, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": me.csrf_token,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": String(draft.version),
        },
        body: JSON.stringify({
          base_revision_id: draft.data.base_revision_id,
          prompt: draft.data.prompt,
          files: { ...files, ...changes },
        }),
      });
      if (!response.ok) throw Error(await response.text());
    },
    { projectId, changes },
  );
  await page.reload();
  await expect(
    page.getByRole("tree", { name: "Arquivos do projeto" }),
  ).toBeVisible();
}

test("editor com CSP de produção, formatação reversível e sintaxe preservada", async ({
  page,
}, info) => {
  const project = await workspace(page);
  const minified =
    '<!doctype html><html lang="pt-BR"><head><title>Teste</title></head><body><main><h1>Olá</h1><p>Aprendendo a construir</p></main></body></html>';
  await draft(page, project.id, {
    "public/index.html": minified,
    "public/broken.js": "const = ;",
  });
  const surface = page.locator(".cm-editor");
  await expect(surface).toBeVisible();
  const presentation = await surface.evaluate((editor) => {
    const scroller = editor.querySelector(".cm-scroller")!;
    return {
      display: getComputedStyle(editor).display,
      font: getComputedStyle(scroller).fontFamily,
      whitespace: getComputedStyle(editor.querySelector(".cm-line")!)
        .whiteSpace,
      hasNonce: Array.from(document.querySelectorAll("style")).some(
        (style) =>
          style.nonce ===
          document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]')
            ?.content,
      ),
    };
  });
  expect(presentation.display).toBe("flex");
  expect(presentation.font).toMatch(/monospace/);
  expect(presentation.whitespace).toMatch(/pre/);
  expect(presentation.hasNonce).toBe(true);
  expect(await page.locator(".cm-line").count()).toBeGreaterThan(10);
  const stored = await page.evaluate(
    async (pid) =>
      (await (await fetch(`/api/v1/projects/${pid}/draft`)).json()).data.files[
        "public/index.html"
      ],
    project.id,
  );
  expect(stored).toBe(minified);
  await page.getByRole("button", { name: "Formatar arquivo" }).click();
  await expect(page.locator(".format-message")).toContainText(
    "Arquivo formatado no rascunho",
  );
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  const formatted = await page.locator(".code-editor").inputValue();
  expect(formatted.split("\n").length).toBeGreaterThan(10);
  expect(formatted).toContain("<h1>Olá</h1>");
  expect(
    await page.evaluate(
      async (pid) =>
        (await (await fetch(`/api/v1/projects/${pid}`)).json())
          .current_revision_id,
      project.id,
    ),
  ).toBe(project.current_revision_id);
  await page
    .getByRole("button", { name: "Salvar revisão", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Salvar revisão", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText("Revisão salva.", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Usar texto simples" }).click();
  await expect(page.locator(".code-editor")).toHaveValue(formatted);
  await page.getByRole("treeitem", { name: "broken.js", exact: true }).click();
  await page.getByRole("button", { name: "Formatar arquivo" }).click();
  await expect(page.locator(".format-message")).toContainText(
    "confira a sintaxe",
  );
  await expect(page.locator(".code-editor")).toHaveValue("const = ;");
  await page.getByRole("treeitem", { name: "index.html", exact: true }).click();
  await page.getByRole("button", { name: "Editor com destaque" }).click();
  await page.screenshot({
    path: info.outputPath("ide-formatada-csp.png"),
    fullPage: true,
  });
});

test("prévia executa HTML, CSS, módulos locais e interação sem alterar a revisão", async ({
  page,
}, info) => {
  const project = await workspace(page);
  await draft(page, project.id, {
    "public/index.html":
      '<!doctype html><html lang="pt-BR"><head><title>Loja</title><link rel="stylesheet" href="/style.css"></head><body><h1>Loja de teste</h1><button id="count">Adicionar</button><output id="total">0</output><div id="loads"></div><img alt="Marca" src="/brand.svg"><a href="/sobre.html">Sobre</a><script type="module" src="/app.js"></script></body></html>',
    "public/style.css":
      '@import "./colors.css"; body { font: 18px system-ui; padding: 20px; } h1 { color: rgb(20, 60, 100); }',
    "public/colors.css": "body { background: rgb(240, 245, 250); }",
    "public/brand.svg":
      '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><circle cx="12" cy="12" r="10" fill="navy" /></svg>',
    "public/amount.js": "export const amount = 2;",
    "public/app.js":
      'import { amount } from "./amount.js"; document.getElementById("count").onclick = async () => { const { amount: next } = await import("./amount.js"); document.getElementById("total").textContent = String(amount + next); }; let loads = 0; document.addEventListener("DOMContentLoaded", () => { document.getElementById("loads").textContent = `Inicializações: ${++loads}`; });',
    "public/sobre.html":
      '<!doctype html><html lang="pt-BR"><head><title>Sobre</title></head><body><h1>Sobre a loja</h1><a href="/index.html">Início</a></body></html>',
  });
  await page.getByRole("tab", { name: "Prévia", exact: true }).click();
  const preview = page.frameLocator(
    'iframe[title="Prévia interativa do projeto"]',
  );
  await expect(
    preview.getByRole("heading", { name: "Loja de teste" }),
  ).toBeVisible();
  await expect(preview.locator("body")).toHaveCSS(
    "background-color",
    "rgb(240, 245, 250)",
  );
  await expect(preview.locator("h1")).toHaveCSS("color", "rgb(20, 60, 100)");
  await preview.getByRole("button", { name: "Adicionar" }).click();
  await expect(preview.locator("#total")).toHaveText("4");
  await expect(preview.locator("#loads")).toHaveText("Inicializações: 1");
  await expect(preview.locator("img")).toHaveJSProperty("naturalWidth", 24);
  await expect(page.locator(".preview-issues")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("previa-funcionando.png"),
    fullPage: true,
  });
  await preview.getByRole("link", { name: "Sobre", exact: true }).click();
  await expect(
    preview.getByRole("heading", { name: "Sobre a loja" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Atualizar prévia" }).click();
  await expect(
    preview.getByRole("heading", { name: "Loja de teste" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      async (pid) =>
        (await (await fetch(`/api/v1/projects/${pid}`)).json())
          .current_revision_id,
      project.id,
    ),
  ).toBe(project.current_revision_id);
});

test("prévia isola sessão, bloqueia rede e informa dependência do backend", async ({
  page,
}) => {
  const project = await workspace(page);
  let externalRequests = 0;
  await page.route("https://external.invalid/**", async (route) => {
    externalRequests++;
    await route.abort();
  });
  await draft(page, project.id, {
    "public/index.html":
      '<html lang="pt-BR"><head><title>Isolamento</title></head><body><h1>Isolamento</h1><output id="checks"></output><button id="api">Consultar API</button><button id="escape">Navegar fora</button><script src="/app.js"></script></body></html>',
    "public/app.js": `const blocked = [];
      for (const [name, attempt] of [
        ['parent', () => parent.document.body], ['cookie', () => document.cookie], ['storage', () => localStorage.getItem('session')]
      ]) { try { attempt(); } catch { blocked.push(name); } }
      document.getElementById('checks').textContent = blocked.join(',');
      document.getElementById('api').onclick = () => fetch('/api/v1/me').catch(() => {});
      document.getElementById('escape').onclick = () => { location.href = 'https://external.invalid/capture'; };
      const img = new Image(); img.src = 'https://external.invalid/image';
      const xhr = new XMLHttpRequest(); xhr.open('GET', 'https://external.invalid/data'); xhr.send();`,
  });
  await page.getByRole("tab", { name: "Prévia", exact: true }).click();
  const preview = page.frameLocator("iframe");
  await expect(preview.locator("#checks")).toHaveText("parent,cookie,storage");
  await preview.getByRole("button", { name: "Consultar API" }).click();
  await expect(page.locator(".preview-issues")).toContainText(
    "APIs e banco de dados",
  );
  await preview.getByRole("button", { name: "Navegar fora" }).click();
  await expect(page.locator(".preview-issues")).toContainText(
    "A navegação para fora da prévia foi bloqueada",
  );
  expect(externalRequests).toBe(0);
  await page.getByRole("button", { name: "Atualizar prévia" }).click();
  await expect(
    preview.getByRole("heading", { name: "Isolamento" }),
  ).toBeVisible();
  expect(
    await page.evaluate(async () => (await fetch("/api/v1/me")).status),
  ).toBe(200);
  await expect(page.locator("iframe")).toHaveAttribute(
    "sandbox",
    "allow-scripts",
  );
});

test("prévia em celular, erros de JavaScript e arquivo ausente são visíveis", async ({
  page,
}, info) => {
  const project = await workspace(page);
  await draft(page, project.id, {
    "public/index.html":
      '<html lang="pt-BR"><head><title>Erro</title></head><body><h1>Conteúdo preservado</h1><script>throw Error("Erro de teste no projeto");</script><script src="/ausente.js"></script></body></html>',
  });
  await page.setViewportSize({ width: 375, height: 812 });
  await page
    .getByRole("tablist", { name: "Área do projeto" })
    .getByRole("tab", { name: "Prévia", exact: true })
    .click();
  await expect(
    page
      .frameLocator("iframe")
      .getByRole("heading", { name: "Conteúdo preservado" }),
  ).toBeVisible();
  await expect(page.locator(".preview-issues")).toContainText(
    "Erro de teste no projeto",
  );
  await expect(page.locator(".preview-issues")).toContainText(
    "public/ausente.js",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const accessibility = await new AxeBuilder({ page })
    .include(".project-workspace")
    .exclude("iframe")
    .withTags(["wcag2a", "wcag2aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
  await page.screenshot({
    path: info.outputPath("previa-mobile-avisos.png"),
    fullPage: true,
  });
});
