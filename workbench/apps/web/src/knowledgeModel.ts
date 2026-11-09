import type { KnowledgeIndex, MemoryEntry, MemoryState, MemorySummary } from './knowledgeTypes';

export const memoryStates: MemoryState[] = ['CANDIDATE', 'REVIEWED', 'PUBLISHED', 'QUARANTINED', 'REVOKED'];
const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 0;

function validateMemory(value: unknown): asserts value is MemorySummary {
  if (!value || typeof value !== 'object') throw new Error('Invalid knowledge record.');
  const entry = value as MemorySummary;
  if (typeof entry.memory_id !== 'string' || !entry.memory_id || !isCount(entry.revision) || entry.revision < 1 || !memoryStates.includes(entry.state) || typeof entry.title !== 'string' || typeof entry.summary !== 'string' || !['deterministic', 'ai_proposed'].includes(entry.producer) || typeof entry.claim_kind !== 'string' || typeof entry.causal_status !== 'string') throw new Error('Invalid knowledge identity or state.');
  if (!entry.source || typeof entry.source.digest !== 'string' || !Array.isArray(entry.source.evidence_refs) || !Array.isArray(entry.source.source_artifact_digests) || !entry.scope || !Array.isArray(entry.scope.service_ids) || typeof entry.scope.system_id !== 'string') throw new Error('Invalid knowledge source scope.');
  if (!Array.isArray(entry.checks) || !entry.checks.every(check => check && typeof check.id === 'string' && ['pass', 'fail', 'unknown'].includes(check.status) && typeof check.detail === 'string') || !entry.capture || typeof entry.capture.automatic !== 'boolean' || typeof entry.capture.at !== 'string') throw new Error('Invalid knowledge admission metadata.');
}

export function parseKnowledgeIndex(value: unknown): KnowledgeIndex {
  if (!value || typeof value !== 'object') throw new Error('Invalid knowledge index.');
  const index = value as KnowledgeIndex;
  if (typeof index.enabled !== 'boolean' || typeof index.truncated !== 'boolean' || !Array.isArray(index.items) || index.items.length > 100 || !index.counts || Object.entries(index.counts).some(([state, count]) => !memoryStates.includes(state as MemoryState) || !isCount(count))) throw new Error('Invalid knowledge index scope.');
  if ('answer_injection_enabled' in index && typeof index.answer_injection_enabled !== 'boolean') throw new Error('Invalid knowledge answer integration status.');
  const seen = new Set<string>();
  for (const entry of index.items) {
    validateMemory(entry);
    if (!isCount(entry.fact_count) || seen.has(entry.memory_id)) throw new Error('Invalid knowledge summary count or duplicate identity.');
    seen.add(entry.memory_id);
  }
  if (index.enabled && memoryStates.some(state => !isCount(index.counts[state]) || index.counts[state]! < index.items.filter(entry => entry.state === state).length)) throw new Error('Invalid knowledge owner totals.');
  return index;
}

export function parseMemoryEntry(value: unknown): MemoryEntry {
  validateMemory(value);
  const entry = value as unknown as MemoryEntry;
  if (!Array.isArray(entry.facts) || !entry.facts.every(fact => fact && typeof fact.fact_id === 'string' && typeof fact.modality === 'string' && fact.record && typeof fact.record === 'object' && !Array.isArray(fact.record))) throw new Error('Invalid source observation facts.');
  return entry;
}

export function filterMemories(items: MemorySummary[], query: string, state = ''): MemorySummary[] {
  const text = query.trim().toLocaleLowerCase();
  return items.filter(entry => (!state || entry.state === state) && (!text || [entry.title, entry.summary, entry.memory_id, entry.source.incident_id ?? '', entry.source.run_id ?? '', ...entry.scope.service_ids].some(value => value.toLocaleLowerCase().includes(text))));
}

export function memoryActions(entry: MemoryEntry) {
  return {
    review: ['CANDIDATE', 'QUARANTINED'].includes(entry.state),
    publish: entry.state === 'REVIEWED' && entry.producer === 'deterministic' && entry.claim_kind === 'recorded_observation' && entry.causal_status === 'not_assessed' && entry.checks.length > 0 && entry.checks.every(check => check.status === 'pass'),
    revoke: entry.state === 'PUBLISHED',
  };
}

export function memoryDecision(entry: MemoryEntry, action: 'review' | 'publish' | 'revoke', reason: string) {
  if (!memoryActions(entry)[action]) throw new Error('This decision is unavailable for the current memory state and source checks.');
  const text = reason.trim();
  if (!text || text.length > 1000) throw new Error('A decision requires a note of 1–1,000 characters.');
  return action === 'review' ? { expected_revision: entry.revision, note: text } : { expected_revision: entry.revision, reason: text };
}

/** Idempotency replays are historical receipts, not authority to undo a later decision. */
export function reconcileMemoryEntry(current: MemoryEntry | null, incoming: MemoryEntry): MemoryEntry {
  if (!current) return incoming;
  if (current.memory_id !== incoming.memory_id) throw new Error('The knowledge response belongs to another record.');
  if (incoming.revision <= current.revision || (current.state === 'REVOKED' && incoming.state !== 'REVOKED')) return current;
  return incoming;
}

export function knowledgePath(memoryId: string, referenceRevision?: number): string {
  return `/knowledge/${encodeURIComponent(memoryId)}${referenceRevision === undefined ? '' : `/reference/${referenceRevision}`}`;
}

export function reviewKind(actorType: string | undefined | null): 'policy' | 'human' | 'other' | 'none' {
  return actorType === 'policy' ? 'policy' : actorType === 'human' ? 'human' : actorType ? 'other' : 'none';
}
