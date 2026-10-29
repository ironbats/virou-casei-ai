export type FieldValue = string | boolean | string[] | null;
export type TemplateField = {
  key: string;
  id?: string;
  semantic_id?: string;
  label: string;
  description?: string;
  required: boolean;
  max_length: number;
  type?: "text" | "boolean" | "enum" | "string_list";
  constraints?: {
    min_length?: number;
    max_length?: number;
    enum?: string[];
    min_items?: number;
    max_items?: number;
  };
  sensitivity?: string;
  allowed_source_types?: string[];
  display_order?: number;
};
export type FieldProvenance = {
  field_id: string;
  value: FieldValue;
  state:
    | "missing"
    | "suggested"
    | "confirmed"
    | "invalid"
    | "not_applicable"
    | "applied"
    | "user_edited"
    | "empty";
  source_type: string;
  source_refs: string[];
  suggestion_version?: number | null;
  field_revision?: number;
  edited_by_user: boolean;
  updated_at: string;
};
export type FieldGeneration = {
  state:
    | "queued"
    | "filling"
    | "reconciling"
    | "succeeded"
    | "failed"
    | "stale"
    | "cancelled";
  operation_id?: string;
  message?: string;
  error_code?: string;
  request_id?: string;
};
export type AttachmentIssue = {
  attachment_id: string;
  field_id?: string;
  code: string;
  message: string;
  action?: string;
};
export type Template = {
  id: string;
  version_id: string;
  version: number;
  title: string;
  category: string;
  summary: string;
  content: string;
  available: boolean;
  fields: TemplateField[];
};
export type Attachment = {
  template_id: string;
  source_version_id: string;
  edited_content: string;
  fields: Record<string, FieldValue>;
  enabled: boolean;
  title?: string;
  version?: number;
  category?: string;
  edited?: boolean;
  attachment_revision?: number;
  schema_version?: number;
  field_values?: Record<string, FieldProvenance>;
  field_generations?: Record<string, FieldGeneration>;
  validation_errors?: AttachmentIssue[];
  fill_state?:
    | "queued"
    | "filling"
    | "ready"
    | "needs_input"
    | "failed"
    | "stale"
    | "reconciling";
  fill_message?: string;
  preparation_id?: string;
  fill_questions?: {
    id: string;
    question: string;
    field_ids: string[];
    required: boolean;
  }[];
};
export type BuildBlocker = {
  code: string;
  message: string;
  attachment_id?: string;
  field_id?: string;
};
export type ReviewSnapshot = {
  hash: string;
  draft_revision: number;
  context_revision: string | null;
  context_hash: string;
  objective: string;
  message_text: string;
  attachments: (Attachment & {
    id?: string;
    source_template_id?: string;
    content?: string;
  })[];
  empty_selection_confirmed: boolean;
  reviewed_at: string;
};
export type BuildDraft = {
  stage: "idea" | "templates" | "review" | "conversation";
  objective: string;
  message_text: string;
  attachments: Attachment[];
  recommendation_id: string | null;
  context_revision?: string | null;
  context_hash?: string;
  recommendation_status?: "ready" | "fallback" | "pending" | null;
  templates_completed?: boolean;
  review_snapshot?: ReviewSnapshot | null;
  can_review?: boolean;
  can_build?: boolean;
  blocking_reasons?: BuildBlocker[];
};
export type BuildPolicy = {
  max_objective_chars: number;
  max_message_chars: number;
  max_attachments: number;
  max_attachment_chars: number;
  max_context_bytes: number;
  autosave_ms: number;
  selection_debounce_ms?: number;
};
export type Recommendation = {
  recommendation_id?: string;
  objective_hash: string;
  source: string;
  status: string;
  fallback_reason?: string;
  items: { template_id: string; version_id: string }[];
};
export const buildPayload = (d: BuildDraft) => ({
  stage: d.stage,
  objective: d.objective,
  message_text: d.message_text,
  recommendation_id: d.recommendation_id,
  attachments: d.attachments.map((a) => ({
    template_id: a.template_id,
    source_version_id: a.source_version_id,
    edited_content: a.edited_content,
    fields: a.fields,
    enabled: a.enabled,
  })),
});
