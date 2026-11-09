export type MemoryState = 'CANDIDATE' | 'REVIEWED' | 'PUBLISHED' | 'QUARANTINED' | 'REVOKED';
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export interface MemoryEvidenceRef { evidence_id: string; source_artifact_digest: string | null; record_locator: string | null; content_digest?: string | null }
export interface MemorySource {
  kind: string; investigation_id: string | null; run_id: string | null; incident_id: string | null;
  data_revision: string | null; digest: string; evidence_refs: MemoryEvidenceRef[];
  source_artifact_digests: string[]; group_id: string | null;
}
export interface MemoryScope { system_id: string; service_ids: string[]; data_revision: string | null }
export interface MemoryFact {
  fact_id: string; modality: string; service_id: string | null; evidence_id: string | null;
  record: Record<string, JsonValue>; unit: string | null;
  source_artifact_digest: string | null; record_locator: string | null;
}
export interface MemoryCheck { id: string; status: 'pass' | 'fail' | 'unknown'; detail: string }
export interface MemoryReview { actor_type: string; note: string; at: string; basis: JsonValue }
export interface MemoryPublication { published_at: string | null; revoked_at: string | null; reason: string }
export interface MemorySummary {
  memory_id: string; revision: number; state: MemoryState; title: string; summary: string;
  producer: 'deterministic' | 'ai_proposed'; claim_kind: string; causal_status: string;
  source: MemorySource; scope: MemoryScope; checks: MemoryCheck[];
  capture: { automatic: boolean; at: string }; review: MemoryReview | null;
  publication: MemoryPublication | null; fact_count: number;
}
export interface MemoryEntry extends Omit<MemorySummary, 'fact_count'> { facts: MemoryFact[]; fact_count?: number }
export interface KnowledgeIndex { enabled: boolean; items: MemorySummary[]; truncated: boolean; counts: Partial<Record<MemoryState, number>>; reason?: string; answer_injection_enabled?: boolean }
export interface MemoryRetrievalItem {
  memory_id: string; revision: number; title: string; summary: string;
  source: Pick<MemorySource, 'kind' | 'incident_id' | 'run_id' | 'digest'> & Partial<MemorySource>;
  scope?: MemoryScope; facts?: MemoryFact[];
  review: (Pick<MemoryReview, 'actor_type' | 'at' | 'basis'> & Partial<MemoryReview>) | null;
  match_reason: JsonValue; score?: number; claim_kind?: string; causal_status?: string;
}
export interface MemoryCitation { memory_id: string; revision: number; source_incident_id: string | null; source_run_id: string | null }
export interface MemoryRetrieval { snapshot_id: string; items: MemoryRetrievalItem[]; limitations: string[] }
