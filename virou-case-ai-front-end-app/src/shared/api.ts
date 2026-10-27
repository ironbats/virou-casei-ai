let csrf = "";
let actor = "";
export function setSession(id: string, token: string) {
  actor = id;
  csrf = token;
}
export function clearSession() {
  for (const key of Object.keys(sessionStorage))
    if (key.startsWith("vira-intent:")) sessionStorage.removeItem(key);
  actor = "";
  csrf = "";
}
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public requestId = "",
  ) {
    super(message);
  }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch("/api/v1" + path, {
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
    if (response.status === 401 && actor)
      window.dispatchEvent(new Event("vira-session-expired"));
    throw new ApiError(
      body.error?.code ?? "REQUEST_FAILED",
      (path === "/auth/login" && response.status === 401
        ? "Credenciais inválidas."
        : body.error?.message) ??
        "Não foi possível concluir. Tente consultar novamente.",
      response.status,
      body.error?.request_id,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : ((await response.json()) as T);
}
export async function command<T>(
  path: string,
  payload: unknown = {},
  method = "POST",
  version?: number,
): Promise<T> {
  const body = JSON.stringify(payload);
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(actor + method + path + body + (version ?? "")),
  );
  const slot =
    "vira-intent:" +
    Array.from(new Uint8Array(digest), (n) =>
      n.toString(16).padStart(2, "0"),
    ).join("");
  const key = sessionStorage.getItem(slot) ?? crypto.randomUUID();
  sessionStorage.setItem(slot, key);
  const result = await api<T>(path, {
    method,
    body,
    headers: {
      "Idempotency-Key": key,
      ...(version === undefined ? {} : { "If-Match": `"${version}"` }),
    },
  });
  sessionStorage.removeItem(slot);
  return result;
}
export const date = (value?: string) =>
  value
    ? new Intl.DateTimeFormat("pt-BR", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "Ainda não definido";
export const percent = (value: number) =>
  new Intl.NumberFormat("pt-BR", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
export const states: Record<string, string> = {
  queued: "Iniciando",
  running: "Em andamento",
  reconciling: "Verificando resultado",
  succeeded: "Concluída",
  failed: "Não concluída",
  cancelled: "Cancelada",
  active: "Acesso ativo",
  grace: "Download restrito",
  suspended: "Uso suspenso",
  pending: "Aguardando liberação",
  purge_eligible: "Retenção encerrada",
  purging: "Expurgo em andamento",
  purged: "Expurgo confirmado",
};
