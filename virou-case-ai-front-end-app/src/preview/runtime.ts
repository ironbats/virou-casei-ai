import { init, parse } from "es-module-lexer/js";

const nonce = (document.currentScript as HTMLScriptElement | null)?.nonce ?? "";
const send = (type: string, detail = "") =>
  parent.postMessage({ source: "viracase-preview", type, detail }, "*");
const notices = new Set<string>();
function notice(detail: string) {
  if (notices.size < 30 && !notices.has(detail)) {
    notices.add(detail);
    send("notice", detail.slice(0, 400));
  }
}
window.addEventListener("error", (event) => {
  if (event instanceof ErrorEvent)
    notice(event.message || "Erro no JavaScript da prévia.");
});
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  notice(
    event.reason instanceof Error
      ? event.reason.message
      : "Uma ação do projeto não pôde ser concluída.",
  );
});
window.addEventListener("securitypolicyviolation", () =>
  notice(
    "Um recurso externo foi bloqueado. Use arquivos locais em public para a prévia.",
  ),
);

let received = false;
window.addEventListener("message", async (event) => {
  if (
    event.source !== parent ||
    received ||
    event.data?.type !== "render-project"
  )
    return;
  received = true;
  try {
    if (window === parent) throw Error("Abra a prévia pelo seu projeto.");
    const files: Record<string, string> = Object.create(null);
    let bytes = 0;
    for (const [path, source] of Object.entries(event.data.files ?? {})) {
      if (
        !path.startsWith("public/") ||
        path.includes("..") ||
        typeof source !== "string"
      )
        continue;
      bytes += new TextEncoder().encode(source).length;
      if (bytes > 2000000 || Object.keys(files).length >= 80)
        throw Error("A prévia excedeu o limite de arquivos.");
      files[path.slice("public/".length)] = source;
    }
    const entry = String(event.data.entry ?? "index.html");
    if (!(entry in files) || !/\.html?$/.test(entry))
      throw Error("Adicione public/index.html para abrir a prévia.");
    await init();

    const resolve = (raw: string, from = entry) => {
      const url = new URL(raw, "https://preview.invalid/" + from);
      if (url.origin !== "https://preview.invalid")
        throw Error("Recursos externos não estão disponíveis nesta prévia.");
      const path = decodeURIComponent(url.pathname).slice(1);
      if (!(path in files))
        throw Error("Arquivo não encontrado na prévia: public/" + path);
      return path;
    };
    const mime = (path: string) =>
      /\.css$/.test(path)
        ? "text/css"
        : /\.[cm]?js$/.test(path)
          ? "text/javascript"
          : /\.json$/.test(path)
            ? "application/json"
            : /\.svg$/.test(path)
              ? "image/svg+xml"
              : /\.html?$/.test(path)
                ? "text/html"
                : "text/plain";
    const urls = new Map<string, string>();
    const blob = (value: string, type: string) =>
      URL.createObjectURL(new Blob([value], { type }));
    const asset = (raw: string, from = entry): string => {
      if (
        raw.startsWith("#") ||
        /^data:(?:image|font)\//i.test(raw) ||
        [...urls.values()].includes(raw)
      )
        return raw;
      try {
        const path = resolve(raw, from);
        if (!urls.has(path)) urls.set(path, blob(files[path], mime(path)));
        return urls.get(path)!;
      } catch (error) {
        notice((error as Error).message);
        return "data:,";
      }
    };
    const styles = (
      source: string,
      from: string,
      chain: string[] = [],
    ): string => {
      if (chain.includes(from) || chain.length > 20) {
        notice("Importação circular de CSS: " + from);
        return "";
      }
      const next = [...chain, from];
      return source
        .replace(
          /@import\s+(?:url\(\s*)?["']([^"']+)["']\s*\)?\s*([^;]*);/gi,
          (_, ref, media) => {
            try {
              const path = resolve(ref, from),
                css = styles(files[path], path, next);
              return media.trim() ? `@media ${media} {${css}}` : css;
            } catch (error) {
              notice((error as Error).message);
              return "";
            }
          },
        )
        .replace(
          /url\(\s*(["']?)([^)"']+)\1\s*\)/gi,
          (_, _quote, ref) => `url("${asset(ref.trim(), from)}")`,
        );
    };
    const moduleSource = (source: string, path: string) => {
      const [imports] = parse(source, path);
      let result = source;
      for (const item of [...imports].reverse()) {
        if (item.type === "import-meta") continue;
        if (!item.specifier || (item.type === "dynamic" && item.glob)) {
          throw Error(
            "Use caminhos literais nas importações da prévia: " + path,
          );
        }
        if (!/^(\.{1,2}\/|\/)/.test(item.specifier)) {
          throw Error(
            "A prévia precisa de módulos locais já preparados para o navegador: " +
              item.specifier,
          );
        }
        const target = resolve(item.specifier, path),
          specifier = "vfs:/" + target;
        const value =
          item.type === "dynamic" ? JSON.stringify(specifier) : specifier;
        result = result.slice(0, item.start) + value + result.slice(item.end);
      }
      return result;
    };
    const imports: Record<string, string> = {};
    for (const path of Object.keys(files).filter((path) =>
      /\.[cm]?js$/.test(path),
    )) {
      try {
        imports["vfs:/" + path] = blob(
          moduleSource(files[path], path),
          "text/javascript",
        );
      } catch (error) {
        notice((error as Error).message);
      }
    }

    window.fetch = async (input, options) => {
      const raw = input instanceof Request ? input.url : String(input);
      if (
        (
          options?.method ?? (input instanceof Request ? input.method : "GET")
        ).toUpperCase() !== "GET"
      ) {
        const message =
          "Esta ação depende do backend. A prévia visual não executa a API nem o banco de dados.";
        notice(message);
        throw new TypeError(message);
      }
      try {
        const path = resolve(raw);
        return new Response(files[path], {
          headers: { "Content-Type": mime(path) },
        });
      } catch {
        const message =
          "Esta ação depende de um recurso não disponível na prévia visual. APIs e banco de dados precisam do ambiente de desenvolvimento do projeto.";
        notice(message);
        throw new TypeError(message);
      }
    };
    const doc = new DOMParser().parseFromString(files[entry], "text/html");
    doc
      .querySelectorAll("base, meta[http-equiv], iframe, frame, object, embed")
      .forEach((element) => element.remove());
    const scripts = Array.from(doc.querySelectorAll("script"));
    scripts.forEach((script) => script.remove());
    doc.querySelectorAll("style").forEach((style) => {
      style.textContent = styles(style.textContent ?? "", entry);
    });
    doc.querySelectorAll<HTMLLinkElement>("link").forEach((link) => {
      if (link.rel !== "stylesheet") {
        link.remove();
        return;
      }
      const style = doc.createElement("style");
      style.media = link.media;
      try {
        const path = resolve(link.getAttribute("href") ?? "");
        style.textContent = styles(files[path], path);
      } catch (error) {
        notice((error as Error).message);
      }
      link.replaceWith(style);
    });
    doc
      .querySelectorAll<HTMLElement>("[style]")
      .forEach((element) =>
        element.setAttribute(
          "style",
          styles(element.getAttribute("style") ?? "", entry),
        ),
      );
    doc.querySelectorAll("[src], [poster], [srcset]").forEach((element) => {
      for (const name of ["src", "poster"]) {
        const ref = element.getAttribute(name);
        if (ref) element.setAttribute(name, asset(ref));
      }
      if (element.hasAttribute("srcset")) {
        const candidates = (element.getAttribute("srcset") ?? "")
          .split(",")
          .map((value) => {
            const [url, ...size] = value.trim().split(/\s+/);
            return `${asset(url)} ${size.join(" ")}`;
          });
        element.setAttribute("srcset", candidates.join(", "));
      }
    });
    document.replaceChild(
      document.adoptNode(doc.documentElement),
      document.documentElement,
    );
    document.addEventListener("click", (event) => {
      const anchor = (event.target as Element)?.closest("a[href]");
      if (!anchor) return;
      event.preventDefault();
      const href = anchor.getAttribute("href")!;
      if (href.startsWith("#")) {
        document.getElementById(href.slice(1))?.scrollIntoView();
        return;
      }
      try {
        const path = resolve(href);
        if (/\.html?$/.test(path)) send("navigate", path);
        else notice("Abra este arquivo na aba Código: public/" + path);
      } catch (error) {
        notice((error as Error).message);
      }
    });
    document.addEventListener("submit", (event) => {
      if (!event.defaultPrevented) {
        event.preventDefault();
        notice("O envio deste formulário depende do backend do projeto.");
      }
    });
    const map = document.createElement("script");
    map.type = "importmap";
    map.nonce = nonce;
    map.textContent = JSON.stringify({ imports });
    document.head.append(map);
    for (const original of scripts) {
      const type = original.getAttribute("type");
      if (
        type &&
        !["module", "text/javascript", "application/javascript"].includes(type)
      ) {
        if (type !== "importmap")
          document.body.append(document.adoptNode(original));
        continue;
      }
      try {
        const src = original.getAttribute("src"),
          path = src ? resolve(src) : entry;
        const source = src ? files[path] : (original.textContent ?? "");
        const script = document.createElement("script");
        script.nonce = nonce;
        if (type === "module") script.type = "module";
        script.src = blob(moduleSource(source, path), "text/javascript");
        await new Promise<void>((resolveScript, reject) => {
          const timer = setTimeout(
            () =>
              reject(
                Error(
                  "O JavaScript excedeu o tempo de carregamento da prévia.",
                ),
              ),
            10000,
          );
          script.onload = () => {
            clearTimeout(timer);
            resolveScript();
          };
          script.onerror = () => {
            clearTimeout(timer);
            reject(Error("Falha no JavaScript: public/" + path));
          };
          document.body.append(script);
        });
      } catch (error) {
        notice((error as Error).message);
      }
    }
    document.dispatchEvent(new Event("DOMContentLoaded", { bubbles: true }));
    window.dispatchEvent(new Event("load"));
    send("rendered");
  } catch (error) {
    send("error", (error as Error).message);
  }
});
send("ready");
