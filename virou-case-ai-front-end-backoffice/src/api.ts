let csrf = "",
  actor = "";
export const session = (id: string, token: string) => {
  actor = id;
  csrf = token;
};
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch("/admin-api/v1" + path, {
    credentials: "same-origin",
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401)
      window.dispatchEvent(new Event("admin-expired"));
    throw new Error(
      (path === "/auth/login" && response.status === 401
        ? "Credenciais inválidas."
        : body.error?.message) ??
        "Falha de comunicação com a API administrativa.",
    );
  }
  return response.status === 204 ? (undefined as T) : await response.json();
}
export async function command<T>(path: string, payload: unknown): Promise<T> {
  const body = JSON.stringify(payload),
    bytes = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(actor + path + body),
    );
  const slot =
    "vira-admin-intent:" +
    Array.from(new Uint8Array(bytes), (x) =>
      x.toString(16).padStart(2, "0"),
    ).join("");
  const key = sessionStorage.getItem(slot) ?? crypto.randomUUID();
  sessionStorage.setItem(slot, key);
  const result = await api<T>(path, {
    method: "POST",
    body,
    headers: { "Idempotency-Key": key },
  });
  sessionStorage.removeItem(slot);
  return result;
}
export const date = (v?: string) =>
  v ? new Date(v).toLocaleString("pt-BR") : "Não definido";
export const brl = (v: string) =>
  new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency: "BRL",
    minimumFractionDigits: 8,
  }).format(Number(v));
