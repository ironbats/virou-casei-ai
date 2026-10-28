export type Template = {
  id: string;
  version_id: string;
  version: number;
  title: string;
  category: string;
  summary: string;
  content: string;
  available: boolean;
  fields: {
    key: string;
    label: string;
    required: boolean;
    max_length: number;
  }[];
};
export type Attachment = {
  template_id: string;
  source_version_id: string;
  edited_content: string;
  fields: Record<string, string>;
  enabled: boolean;
  title?: string;
  version?: number;
  category?: string;
  edited?: boolean;
};
export type BuildDraft = {
  stage: "idea" | "templates" | "review" | "conversation";
  objective: string;
  message_text: string;
  attachments: Attachment[];
  recommendation_id: string | null;
};
export type BuildPolicy = {
  max_objective_chars: number;
  max_message_chars: number;
  max_attachments: number;
  max_attachment_chars: number;
  max_context_bytes: number;
  autosave_ms: number;
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
  ...d,
  attachments: d.attachments.map((a) => ({
    template_id: a.template_id,
    source_version_id: a.source_version_id,
    edited_content: a.edited_content,
    fields: a.fields,
    enabled: a.enabled,
  })),
});
