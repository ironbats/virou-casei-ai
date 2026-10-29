import type { Draft } from "../shared/types";
import type { AttachmentIssue, BuildDraft, FieldValue } from "./guidedTypes";

export type PreparationState =
  | "queued"
  | "filling"
  | "ready"
  | "needs_input"
  | "failed"
  | "cancelled"
  | "cancellation_requested"
  | "reconciling"
  | "stale";

export type FieldSuggestion = {
  field_id: string;
  value: FieldValue;
  state: "suggested" | "confirmed" | "invalid" | "applied";
  source_type:
    "request" | "project_context" | "user_answer" | "policy_suggestion";
  source_refs: string[];
  suggestion_version: number;
  original_value: FieldValue;
  compatible: boolean;
  reason?: string;
};

export type PreparationQuestion = {
  id: string;
  question: string;
  field_ids: string[];
  required: boolean;
  answer?: string;
};

export type PreparationItem = {
  attachment_id: string;
  source_version_id: string;
  attachment_revision: number;
  title: string;
  state: PreparationState;
  suggested_fields: FieldSuggestion[];
  questions: PreparationQuestion[];
  errors: AttachmentIssue[];
};

export type TemplatePreparation = {
  id: string;
  project_id: string;
  state: PreparationState;
  state_version: number;
  draft_revision: number;
  project_revision: string;
  attachment_revisions: Record<string, number>;
  policy_version: number;
  items: PreparationItem[];
  progress: { completed: number; total: number };
  correlation_id: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_code: string | null;
  message: string;
  can_apply: boolean;
  context_summary?: {
    included_files: number;
    omitted_files: number;
    reason: "CONTEXT_PARTIAL" | null;
    included_conversation_messages: number;
    omitted_conversation_messages: number;
  };
};

export type AcceptedField = { attachment_id: string; field_id: string };
export type PreparationApplication = {
  preparation: TemplatePreparation;
  draft: Draft<BuildDraft>;
  applied_fields: AcceptedField[];
  conflicts: AttachmentIssue[];
};

export const preparationActive = (value: TemplatePreparation | null) =>
  !!value &&
  ["queued", "filling", "cancellation_requested", "reconciling"].includes(
    value.state,
  );

export const preparationStates: Record<PreparationState, string> = {
  queued: "Aguardando preparação",
  filling: "Preparando campos",
  ready: "Sugestões para revisar",
  needs_input: "Precisa de informação",
  failed: "Não foi possível preparar",
  cancelled: "Preparação cancelada",
  cancellation_requested: "Cancelamento solicitado",
  reconciling: "Conferindo resultado",
  stale: "Contexto alterado",
};

export const fieldSources: Record<string, string> = {
  request: "Seu pedido",
  project_context: "Contexto da revisão salva",
  user_answer: "Sua resposta",
  policy_suggestion: "Proposta para você confirmar",
  manual: "Edição manual",
  user_edit: "Edição manual",
};
