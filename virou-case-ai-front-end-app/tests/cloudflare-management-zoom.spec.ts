import { chromium, expect, test } from "@playwright/test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

declare const chrome: {
  tabs: {
    query: (query: object) => Promise<{ id: number; url?: string }[]>;
    setZoom: (tabId: number, factor: number) => Promise<void>;
    getZoom: (tabId: number) => Promise<number>;
  };
};

test("zoom nativo de 200% mantém fechamento, abas e conteúdo sem overflow", async () => {
  const extension = mkdtempSync(join(tmpdir(), "viroucase-zoom-extension-"));
  const profile = mkdtempSync(join(tmpdir(), "viroucase-zoom-profile-"));
  writeFileSync(join(extension, "manifest.json"), JSON.stringify({
    manifest_version: 3,
    name: "Local accessibility zoom validation",
    version: "1.0",
    permissions: ["tabs"],
    host_permissions: ["http://127.0.0.1/*"],
    background: { service_worker: "background.js" },
  }));
  writeFileSync(join(extension, "background.js"), "chrome.runtime.onInstalled.addListener(() => {});");
  const context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
    headless: true,
    viewport: { width: 1280, height: 1040 },
    timezoneId: "America/Sao_Paulo",
    ignoreDefaultArgs: ["--disable-extensions"],
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
  });
  try {
    const config = JSON.parse(readFileSync(new URL("./fixtures/cloudflare-management-connection.json", import.meta.url), "utf8"));
    const diagnostic = JSON.parse(readFileSync(new URL("./fixtures/cloudflare-management-api.json", import.meta.url), "utf8"));
    const page = context.pages()[0];
    await page.route(`**/admin-api/v1/integrations/cloudflare/connections/${config.connection_id}`, route => route.fulfill({ json: config }));
    await page.route(`**/admin-api/v1/integration-connections/${config.connection_id}/publication-diagnostics`, route => route.fulfill({ json: diagnostic }));
    await page.route("**/admin-api/v1/publication-qualification-requirements", route => route.fulfill({ json: { checks: [] } }));
    await page.goto("http://127.0.0.1:5175/tests/cloudflare-manager.fixture.html");
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const zoom = await worker.evaluate(async () => {
      const tabs = await chrome.tabs.query({});
      const tab = tabs.find(item => item.url?.includes("127.0.0.1"));
      if (!tab) throw new Error("Local validation tab not found");
      await chrome.tabs.setZoom(tab.id, 2);
      return await chrome.tabs.getZoom(tab.id);
    });
    expect(zoom).toBe(2);
    await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(2);
    await page.evaluate(value => window.dispatchEvent(new CustomEvent("fixture-connection", { detail: value })), config);
    await expect(page.getByRole("tab", { name: "Visão geral", exact: true })).toBeVisible();
    const observations: unknown[] = [];
    for (const tab of ["Visão geral", "Verificação", "Conexão", "Qualificação"]) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      const dimensions = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, devicePixelRatio, documentWidth: document.documentElement.scrollWidth }));
      expect(dimensions.width).toBe(640);
      expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.width + 1);
      const close = page.getByRole("button", { name: "Fechar", exact: true });
      await expect(close).toBeInViewport();
      const closeBounds = await close.boundingBox();
      observations.push({ tab, dimensions, close: closeBounds });
      const dialogOverflow = await page.getByRole("dialog").evaluate(element => element.scrollWidth > element.clientWidth + 1);
      expect(dialogOverflow).toBe(false);
    }
    await page.getByRole("tab", { name: "Verificação", exact: true }).click();
    await page.screenshot({ path: "test-results/cloudflare-native-zoom200.png" });
    writeFileSync("test-results/cloudflare-native-zoom200.json", JSON.stringify({ zoom, method: "Chromium native chrome.tabs.setZoom in an isolated temporary browser profile", observations }, null, 2));
  } finally {
    await context.close();
    rmSync(extension, { recursive: true, force: true });
    rmSync(profile, { recursive: true, force: true });
  }
});
