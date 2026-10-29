import type { BuildPolicy } from "../features/guidedTypes";
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
export type PublicationBuildDiagnostic = {
  format_version: 1;
  category: "source" | "dependency" | "executor" | "output" | "unknown";
  tool: "esbuild" | "python" | "sandbox" | "executor";
  exit_code: number | null;
  duration_ms: number;
  stdout_bytes: number;
  stderr_bytes: number;
  output_truncated: boolean;
  operation_id: string;
  correlation_id: string;
  revision_id: string;
  issues: {
    code: string;
    message: string;
    hint: string;
    path?: string;
    line?: number;
    column?: number;
  }[];
};
export type CodeAnalysisIssue = {
  severity: "error" | "warning" | "info";
  path: string | null;
  line: number | null;
  title: string;
  explanation: string;
  suggestion: string;
};
export type PublicationMigrationDiagnostic = {
  format_version: 1;
  category: "migration";
  code: string;
  analysis_method?: "sql_lexical_v2";
  issues: {
    code: string;
    reason: string;
    path?: string;
    line?: number;
    column?: number;
    statement_index?: number;
    statement_type?: string;
    operation?: string;
    rule?: string;
    explanation?: string;
    expected_sha256?: string;
    actual_sha256?: string | null;
    receipt_source: "inventory" | "remote" | "candidate";
    message: string;
    hint: string;
  }[];
  issue_count: number;
  published_revision_id?: string;
};
export type PublicationProviderDiagnostic = {
  method?: string;
  route?: string;
  status?: number;
  provider_error_codes?: number[];
  cf_ray?: string;
  reason?: string;
  message?: string;
  statement_index?: number;
  line?: number;
  effect_outcome?: string;
};
export type PublicationDiagnostic = {
  version: 1;
  detail_available?: boolean;
  phase?: string;
  migration?: { name: string; sha256?: string };
  effect_status?: "not_sent" | "unknown" | "confirmed" | "none";
  initial_error?: {
    code: string;
    message?: string;
    provider?: PublicationProviderDiagnostic;
  };
};
export type MigrationHistory = {
  format_version: 1;
  policy_version: string;
  base_revision_id: string | null;
  preserved: { path: string; sha256: string }[];
  added: { path: string; sha256: string }[];
};
export type MigrationFailure = {
  code: string;
  message?: string;
  base_revision_id?: string | null;
  migration_history?: {
    format_version: 1;
    base_revision_id?: string | null;
    operation?: string;
    issue_count?: number;
    issues: { path?: string; operation?: string; after_path?: string }[];
  };
  migration?: {
    format_version?: number;
    issue_count?: number;
    issues: {
      path?: string;
      statement_index?: number;
      line?: number;
      column?: number;
      message?: string;
      hint?: string;
    }[];
  };
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
  analysis_id?: string;
  analysis_revision_id?: string;
  reply_to?: string | null;
  progress_message?: string;
  activity?: { stage: string; at: string; message: string }[];
  created_at: string;
  updated_at?: string;
  attachment_snapshots?: {
    id: string;
    title: string;
    version: number;
    content: string;
    content_hash: string;
  }[];
  result_ref: {
    migration_failure?: MigrationFailure;
    publication_diagnostic?: PublicationDiagnostic;
    publication_recovery?: {
      version?: number;
      last_error_message?: string | null;
      reason?: string | null;
      required_action?: string | null;
      owner?: "user" | "operator" | "platform";
      missing_evidence?: string[];
      state: "scheduled" | "running" | "retry_wait" | "attention" | "resolved";
      message: string;
      attempts: number;
      last_attempt_at: string | null;
      last_checked_at: string | null;
      next_attempt_at: string | null;
      last_error_code: string | null;
      resolution?:
        | "pre_upload_failure"
        | "published"
        | "resume_safe"
        | "migration_rejected"
        | "not_sent";
    };
    publication_failure?: {
      code?: string;
      message?: string;
      stage?: string;
      at?: string;
      build?: PublicationBuildDiagnostic;
      migration?: PublicationMigrationDiagnostic;
    };
    discarded_at?: string;
    url?: string;
    verified_at?: string;
    kind?: "implementation" | "clarification" | "code_analysis";
    source?: "publication" | "preview" | "editor";
    publication_operation_id?: string | null;
    correction_operation_id?: string;
    summary?: string;
    issues?: CodeAnalysisIssue[];
    limitations?: string[];
    can_request_correction?: boolean;
    message?: string;
    apply_conflict?: boolean;
    working_files?: string[];
    applied_revision_id?: string;
    revision_id?: string;
    export_id?: string;
    candidate?: boolean;
    download_path?: string;
    changed_files?: {
      path: string;
      change: "created" | "modified" | "deleted";
    }[];
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
  guided_build: BuildPolicy;
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
  code_analysis?: Capability;
  template_preparation?: Capability;
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
