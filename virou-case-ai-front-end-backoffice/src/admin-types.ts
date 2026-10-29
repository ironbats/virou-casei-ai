export type User = { id: string; name: string; csrf_token: string };
export type Account = {
  id: string;
  name: string;
  login: string;
  access: {
    status: string;
    entitlement_version?: number;
    starts_at?: string;
    access_expires_at?: string;
    purge_at?: string;
  };
  suspended: boolean;
};
export type Page<T> = { items: T[]; next_cursor?: string | null };
export type PublicationMigrationDiagnostic = {
  format_version: 1;
  category: "migration";
  code: string;
  issues: {
    code: string;
    reason: string;
    path?: string;
    expected_sha256?: string;
    actual_sha256?: string | null;
    receipt_source: "inventory" | "remote" | "candidate";
    message: string;
    hint: string;
  }[];
  issue_count: number;
  published_revision_id?: string;
};
export type Operation = {
  id: string;
  state: string;
  account_id: string;
  type: string;
  billing_status: string;
  model_label?: string;
  progress_message?: string;
  error_code?: string | null;
  result_ref?: {
    publication_failure?: {
      code?: string;
      message?: string;
      stage?: string;
      at?: string;
      migration?: PublicationMigrationDiagnostic;
    };
    publication_recovery?: {
      state: "scheduled" | "running" | "retry_wait" | "attention" | "resolved";
      message: string;
      attempts: number;
      last_attempt_at: string | null;
      last_checked_at: string | null;
      next_attempt_at: string | null;
      last_error_code: string | null;
      resolution?: "pre_upload_failure" | "published" | "resume_safe";
    };
  };
  policy: Record<string, unknown>;
  attempt?: {
    state: string;
    usage: unknown;
    actual_brl: string;
    external_id: string;
  };
};
export type Audit = {
  id: string;
  action: string;
  actor_id: string;
  resource_id: string;
  reason: string;
  created_at: string;
};
export type Purge = {
  account_id: string;
  fence: string;
  destination: string;
  state: string;
  evidence: unknown;
};
export type Status = {
  ai_enabled: boolean;
  restore_hold: boolean;
  lifecycle_enabled: boolean;
  publication_enabled: boolean;
  purges: Purge[];
};
export type Wallet = {
  available_brl: string;
  reserved_brl: string;
  consumed_brl: string;
  grants: Record<string, string | boolean>[];
};
export type AdminModal = {
  action: string;
  account?: Account;
  op?: Operation;
  purge?: Purge;
} | null;
