import type { Operation } from "../shared/types";

export type ProposalFile = {
  id: string;
  path: string;
  change: "created" | "modified" | "deleted";
  before_hash: string | null;
  after_hash: string | null;
  before_bytes: number;
  after_bytes: number;
  content_type: string;
};

export type Proposal = {
  proposal_id: string;
  proposal_version: string;
  base_project_revision: string;
  candidate_revision_id: string;
  current_project_revision: string;
  expected_project_version: number;
  state: "generated" | "applied" | "discarded" | "conflict";
  files: ProposalFile[];
  verification_results: {
    kind: string;
    state: "not_run" | "running" | "passed" | "failed";
    message: string;
  }[];
  warnings: string[];
  conflict_paths: string[];
  applied_revision_id?: string | null;
  discarded_at?: string | null;
};

export type ProposalContent = ProposalFile & {
  proposal_id: string;
  proposal_version: string;
  before: string | null;
  after: string | null;
};

export type ReviewSelection = { op: Operation; proposal: Proposal } | null;

export type ReviewView = {
  file: string;
  page: number;
  scroll: number;
  wrap: boolean;
  expanded: boolean;
  mode: "unified" | "split";
};
