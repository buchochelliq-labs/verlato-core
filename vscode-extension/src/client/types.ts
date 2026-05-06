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
  grouping_reason?: string;
  status: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  edited_summary: string | null;
  summary_provenance?: SummaryProvenance | null;
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

export interface ClearWorkingStateResult {
  removed_working_items: number;
  removed_feedback_events: number;
  preserved_published_items: number;
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
  summaryProvenance?: SummaryProvenance;
}

export interface SummaryProvenance {
  source: string;
  provider?: string;
  model?: string;
  prompt_sha?: string;
  tone?: string;
  style_profile?: SuggestionStyleProfile;
  suggestion_index?: number;
  similarity_to_current?: number;
  specificity_score?: number;
  response_metadata?: Record<string, unknown>;
  source_evidence_count?: number;
  reasoning_exposed?: boolean;
  applied_at?: string;
  edited_at?: string;
}

export interface DraftPromptPreview {
  item_id: string;
  title: string;
  provider: string;
  tone?: string;
  style_profile?: SuggestionStyleProfile;
  prompt: string;
  prompt_budget?: DraftPromptBudget;
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

export type RepositoryPullRequestState = "OPEN" | "MERGED" | "CLOSED" | "UNKNOWN";

export interface RepositoryPullRequest {
  number: number;
  title?: string;
  state: RepositoryPullRequestState;
  mergedAt?: string | null;
}

export interface ReleaseMarker {
  version: string;
  itemCount: number;
  items?: ReleaseNoteItem[];
}

export interface ReleaseNoteItem {
  id: string;
  title: string;
  summary: string;
  status?: string;
  source?: string;
}

export interface RepositoryOverview {
  branches: RepositoryRef[];
  tags: RepositoryRef[];
  commits: RepositoryCommit[];
  pullRequests?: RepositoryPullRequest[];
  releases: ReleaseMarker[];
  unpublishedCount: number;
}

export interface ModelSettings {
  provider: string;
  model: string;
  endpoint: string;
  ollamaTimeoutSeconds: number;
  maxModelItems: number;
  suggestionCount: number;
  suggestionParallelism: number;
  suggestionTone: string;
  styleGuidance: string;
  projectVocabulary: string[];
  bannedPhrases: string[];
  specificityFloor: number;
  feedbackIncludeText: boolean;
  includePromptPayloads: boolean;
  includeReasoningPayloads: boolean;
  redactPayloads: boolean;
  debugModelLogPath: string;
  ollamaStreamResponses: boolean;
}

export interface DraftSuggestionItem {
  item_id: string;
  title: string;
  current_summary: string;
  provider: string;
  prompt_sha?: string;
  tone?: string;
  style_profile?: SuggestionStyleProfile;
  provider_metadata?: Record<string, unknown>;
  provider_reasoning?: string;
  source_evidence?: DraftSuggestionEvidence[];
  prompt_budget?: DraftPromptBudget;
  suggestion_details?: DraftSuggestionDetail[];
  rejected_candidates?: DraftRejectedSuggestion[];
  timings?: DraftSuggestionTimings;
  suggestions: string[];
}

export interface DraftPromptBudget {
  profile?: string;
  prompt_chars?: number;
  files_included?: number;
  files_total?: number;
  files_skipped?: number;
  commits_included?: number;
  commits_total?: number;
  commits_skipped?: number;
  pr_body_chars_included?: number;
  pr_body_chars_total?: number;
  pr_body_chars_skipped?: number;
  diff_summary_chars_included?: number;
  diff_summary_chars_total?: number;
  diff_summary_chars_skipped?: number;
  hunk_files_included?: number;
  hunk_files_total?: number;
  hunk_files_skipped?: number;
  hunks_included?: number;
  hunks_total?: number;
  hunks_skipped?: number;
  hunk_chars_included?: number;
  hunk_chars_total?: number;
  hunk_chars_skipped?: number;
}

export interface DraftSuggestionTimings {
  evidence_ms?: number;
  prompt_ms?: number;
  model_ms?: number;
  parse_ms?: number;
  total_ms?: number;
  load_ms?: number;
  prompt_eval_ms?: number;
  eval_ms?: number;
  ollama_total_ms?: number;
  queue_ms?: number;
  prompt_tokens?: number;
  eval_tokens?: number;
  tokens_per_second?: number;
}

export interface DraftSuggestionEvidence {
  kind: string;
  label: string;
  detail: string;
}

export interface DraftSuggestionDetail {
  summary: string;
  similarity_to_current: number;
  specificity_score?: number;
  comparison: {
    current: string;
    suggested: string;
  };
}

export interface DraftRejectedSuggestion {
  summary: string;
  reason: string;
  similarity_to_current: number;
  specificity_score?: number;
}

export interface SuggestionStyleProfile {
  guidance?: string;
  project_vocabulary?: string[];
  banned_phrases?: string[];
  specificity_floor?: number;
}

export interface DraftSuggestionPreview {
  base_ref: string;
  head_ref: string;
  provider: string;
  count: number;
  tone?: string;
  style_profile?: SuggestionStyleProfile;
  visible_item_count?: number;
  fanout_guard?: number;
  batch_size?: number;
  batch_count?: number;
  performance?: DraftSuggestionPerformance;
  items: DraftSuggestionItem[];
}

export interface DraftSuggestionPerformance {
  parallelism?: number;
  target_selection_ms?: number;
  preparation_ms?: number;
  prompt_chars?: number;
  model_ms?: number;
  parse_ms?: number;
  total_ms?: number;
}

export type ClientMode = "cli";

export interface ClientStatus {
  mode: ClientMode;
  label: string;
  detail: string;
}

export type CommandRequestStatus = "running" | "completed" | "failed" | "cancelled";

export interface CommandRequestSnapshot {
  id: number;
  command: string;
  args: string[];
  label: string;
  status: CommandRequestStatus;
  startedAt: string;
  endedAt?: string;
  stdout: string;
  stderr: string;
  activity?: CommandLogEntry[];
  exitCode?: number | null;
  error?: string;
}

export interface CommandLogEntry {
  id: number;
  stream: "stdout" | "stderr";
  category: "model" | "progress" | "suggestion" | "process";
  message: string;
  receivedAt: string;
  event?: string;
  attributes?: Record<string, unknown>;
  payload?: string;
}
