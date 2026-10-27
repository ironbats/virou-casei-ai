export type Access = {
  status: string;
  allowed_actions: string[];
  period_id: string | null;
  starts_at?: string;
  access_expires_at?: string;
  purge_at?: string;
};
export type User = {
  id: string;
  name: string;
  login: string;
  role: string;
  csrf_token: string;
  access: Access;
};
export type Offer = {
  amount_minor: string;
  currency: string;
  access_days: number;
  whatsapp_url: string;
};
export type Project = {
  id: string;
  name: string;
  objective: string;
  stack: string;
  version: number;
  current_revision_id: string | null;
  state: string;
  created_at: string;
};
export type Revision = {
  id: string;
  project_id: string;
  origin: string;
  candidate: boolean;
  created_at: string;
  files: { id: string; path: string; size: number }[];
};
export type Operation = {
  id: string;
  type: string;
  project_id: string;
  state: string;
  progress_stage: string;
  billing_status: string;
  pending_reconciliation: boolean;
  cancel_requested: boolean;
  error_code: string | null;
  model_label?: string | null;
  request_text?: string | null;
  progress_message?: string;
  activity?: { stage: string; at: string; message: string }[];
  created_at: string;
  updated_at?: string;
  result_ref: {
    kind?: "implementation" | "clarification";
    message?: string;
    apply_conflict?: boolean;
    working_files?: string[];
    applied_revision_id?: string;
    revision_id?: string;
    export_id?: string;
    candidate?: boolean;
    download_path?: string;
    changed_files?: { path: string; change: "created" | "modified" }[];
    validation?: string;
  };
};
export type Wallet = {
  available_ratio: number;
  reserved_ratio: number;
  consumed_ratio: number;
  can_start_operation: boolean;
  pending_reconciliation: boolean;
  usage_status: string;
};
export type Capability = { enabled: boolean; reason_code?: string };
export type Capabilities = {
  visual_preview?: Capability & {
    mode: "browser_static";
    backend_execution: false;
  };
  limits: {
    max_projects: number;
    max_files: number;
    max_file_bytes: number;
    max_project_bytes: number;
    max_path_length: number;
    max_prompt_chars: number;
    max_revisions: number;
  };
  policy_version: string;
  generation: Capability;
  publication: Capability;
  preview: Capability;
};
export type Guide = {
  template_version: string;
  search_terms?: string;
  application_template: string;
  fields: {
    key: string;
    label: string;
    required: boolean;
    max_length: number;
  }[];
  slug: string;
  title: string;
  category: string;
  objective: string;
  version: string;
  security_summary: string;
  concept: string;
  when_to_use: string;
  steps: string[];
  template: string;
  checklist: string[];
  application: string;
  security_notes: string[];
  limitations: string;
  sources: { title: string; url: string }[];
  reviewed_at: string;
};
export type Page<T> = { items: T[]; next_cursor?: string | null };
export type ProjectDraft = {
  base_revision_id: string | null;
  files: Record<string, string> | null;
  prompt: string;
  provenance?: { slug: string; version: string }[];
};
export type LearningDraft = {
  guide: Guide;
  fields: Record<string, string>;
  prompt: string;
  manual: boolean;
};
export type Draft<T> = { version: number; updated_at: string | null; data: T };
