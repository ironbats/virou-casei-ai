export type Provider = "cloudflare" | "jev";
export type Connection = {
  id: string;
  connection_id: string;
  name: string;
  is_default: boolean;
  configuration_digest?: string;
  executor_digest?: string;
  version: number;
  state: string;
  settings: Record<string, unknown>;
  defaults?: Record<string, unknown>;
  validation: Record<string, unknown>;
  has_token: boolean;
  validated_at?: string;
};
export const stateLabels: Record<string, string> = {
  draft: "Rascunho",
  validated: "Validada",
  active: "Ativa",
  disabled: "Desativada",
  invalid: "Inválida",
  missing: "Não cadastrada",
};
export const providerLabels: Record<Provider, string> = {
  cloudflare: "Cloudflare",
  jev: "Jev",
};
