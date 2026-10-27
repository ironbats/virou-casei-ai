import fs from "node:fs";
import crypto from "node:crypto";
import type { Page } from "@playwright/test";

export async function productionPolicy(page: Page) {
  const config = fs.readFileSync("nginx.conf", "utf8");
  const policies = [
    ...config.matchAll(/add_header Content-Security-Policy "([^"]+)" always;/g),
  ].map((match) => match[1]);
  if (policies.length !== 2)
    throw Error("Confira as políticas da aplicação e da prévia em nginx.conf.");
  await page.route("**/*", async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    if (
      request.resourceType() !== "document" ||
      url.origin !== "http://127.0.0.1:5173"
    )
      return route.continue();
    const response = await route.fetch();
    if (url.pathname === "/preview.html") return route.fulfill({ response });
    if (!response.headers()["content-type"]?.includes("text/html"))
      return route.fulfill({ response });
    const nonce = crypto.randomBytes(16).toString("hex");
    const policy = policies[
      url.pathname === "/preview.html" ? 1 : 0
    ].replaceAll("$request_id", nonce);
    await route.fulfill({
      response,
      body: (await response.text()).replaceAll("__VIRA_CSP_NONCE__", nonce),
      headers: {
        ...response.headers(),
        "Content-Security-Policy": policy,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  });
}
