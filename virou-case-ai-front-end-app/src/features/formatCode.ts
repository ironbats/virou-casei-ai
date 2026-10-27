export const canFormat = (path: string) =>
  /\.(?:[cm]?[jt]sx?|html?|css|scss|less|jsonc?|md|markdown)$/i.test(path);

export function formatCode(path: string, source: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL("./formatCode.worker.ts", import.meta.url),
      { type: "module" },
    );
    const finish = () => {
      clearTimeout(timeout);
      worker.terminate();
    };
    const timeout = setTimeout(() => {
      finish();
      reject(
        new Error(
          "A formatação excedeu o tempo disponível. Seu conteúdo foi preservado.",
        ),
      );
    }, 10000);
    worker.onmessage = (event) => {
      finish();
      if (typeof event.data.formatted === "string")
        resolve(event.data.formatted);
      else reject(new Error(event.data.error));
    };
    worker.onerror = () => {
      finish();
      reject(
        new Error(
          "Não foi possível carregar o formatador. Recarregue e tente novamente.",
        ),
      );
    };
    worker.postMessage({ path, source });
  });
}
