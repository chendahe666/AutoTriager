import type { ConversationStatus } from './conversationTypes';

export interface ConversationThread {
  investigation_id: string;
  title: string;
  incident_id: string;
  turn_count: number;
  last_turn_id: string;
  last_question: string;
  last_status: ConversationStatus;
  updated_at: string;
  context_run_id: string;
}

export interface ConversationIndex {
  enabled: boolean;
  threads: ConversationThread[];
  truncated: boolean;
}

const statuses = new Set(['QUEUED', 'RUNNING', 'COMPLETED', 'FAILED']);

/** An index read contains saved thread metadata, never a dispatched model request. */
export function parseConversationIndex(value: unknown): ConversationIndex {
  if (!value || typeof value !== 'object') throw new Error('Invalid conversation index.');
  const index = value as ConversationIndex;
  if (typeof index.enabled !== 'boolean' || typeof index.truncated !== 'boolean' || !Array.isArray(index.threads) || index.threads.length > 100) throw new Error('Invalid conversation index scope.');
  const seen = new Set<string>();
  for (const thread of index.threads) {
    if (!thread || ![thread.investigation_id, thread.incident_id, thread.last_turn_id, thread.context_run_id].every(field => typeof field === 'string' && field.length > 0) || typeof thread.title !== 'string' || typeof thread.last_question !== 'string' || !statuses.has(thread.last_status) || !Number.isInteger(thread.turn_count) || thread.turn_count < 1 || !Number.isFinite(Date.parse(thread.updated_at)) || seen.has(thread.investigation_id)) throw new Error('Invalid saved conversation thread.');
    seen.add(thread.investigation_id);
  }
  return index;
}

export function filterConversationThreads(threads: ConversationThread[], query: string): ConversationThread[] {
  const term = query.trim().toLocaleLowerCase();
  if (!term) return threads;
  return threads.filter(thread => [thread.title, thread.investigation_id, thread.incident_id, thread.last_question, thread.context_run_id].some(value => value.toLocaleLowerCase().includes(term)));
}

export function conversationThreadPath(thread: Pick<ConversationThread, 'investigation_id' | 'context_run_id'>): string {
  return `/investigations/${encodeURIComponent(thread.investigation_id)}/runs/${encodeURIComponent(thread.context_run_id)}/chat`;
}
