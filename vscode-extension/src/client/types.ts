export interface DraftTarget {
  name: string;
  path: string;
  format: string;
}

export interface DraftItem {
  id: string;
  title: string;
  summary: string;
  kind: string;
  area: string;
  audience: string;
  semver: string;
  confidence: number;
  pr_number: number | null;
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

export type ClientMode = "cli";

export interface ClientStatus {
  mode: ClientMode;
  label: string;
  detail: string;
}
