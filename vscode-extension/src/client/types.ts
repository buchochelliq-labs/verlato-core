export interface DraftTarget {
  name: string;
  path: string;
  format: string;
}

export interface DraftItem {
  id: string;
  title: string;
  summary: string;
  original_summary?: string;
  summary_source?: string;
  classification_source?: string;
  evidence_summary?: string;
  kind: string;
  area: string;
  audience: string;
  semver: string;
  confidence: number;
  pr_number: number | null;
  commit_shas?: string[];
  base_ref: string;
  head_ref: string;
  files: string[];
  commit_signals: string[];
  status: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  edited_summary: string | null;
  hidden: boolean;
}

export interface DraftSection {
  kind: string;
  title: string;
  items: DraftItem[];
}

export interface DraftDocument {
  version: string;
  generated_on: string;
  repository_url?: string | null;
  base_ref: string;
  head_ref: string;
  targets: DraftTarget[];
  sections: DraftSection[];
  rendered: Record<string, string>;
  item_count: number;
}

export interface ApplyResult {
  target: string;
  path: string;
  format: string;
  action: string;
  bytes_written: number;
}

export interface DraftRangeRequest {
  base: string;
  head?: string;
  version?: string;
  includeConfidence?: boolean;
  includeProvenance?: boolean;
}

export interface ReviewUpdateRequest {
  status?: "needs-review" | "ready" | "accepted" | "edited" | "hidden";
  summary?: string;
  reviewer?: string;
}

export interface DraftPromptPreview {
  item_id: string;
  title: string;
  provider: string;
  prompt: string;
}

export interface RepositoryRef {
  name: string;
  detail?: string;
}

export interface RepositoryCommit {
  sha: string;
  subject: string;
  author: string;
  prNumber: number | null;
  release: string | null;
}

export interface ReleaseMarker {
  version: string;
  itemCount: number;
}

export interface RepositoryOverview {
  branches: RepositoryRef[];
  tags: RepositoryRef[];
  commits: RepositoryCommit[];
  releases: ReleaseMarker[];
  unpublishedCount: number;
}

export interface ModelSettings {
  provider: string;
  model: string;
  endpoint: string;
  maxModelItems: number;
  suggestionCount: number;
}

export interface DraftSuggestionItem {
  item_id: string;
  title: string;
  current_summary: string;
  provider: string;
  suggestions: string[];
}

export interface DraftSuggestionPreview {
  base_ref: string;
  head_ref: string;
  provider: string;
  count: number;
  items: DraftSuggestionItem[];
}

export type ClientMode = "cli";

export interface ClientStatus {
  mode: ClientMode;
  label: string;
  detail: string;
}
