import type { ConversationCapabilities, ConversationCitation, ConversationTurn } from './conversationTypes';

const activeStatuses = new Set(['QUEUED', 'RUNNING']);
const terminalStatuses = new Set(['COMPLETED', 'FAILED']);
const terminalRunStates = new Set(['COMPLETED', 'FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED', 'CANCELLED']);

export const activeConversationTurn = (turns: ConversationTurn[]) => turns.find(turn => activeStatuses.has(turn.status));

/** Preserve immutable context and completed answers across overlapping polling reads. */
export function reconcileConversation(
  current: ConversationTurn[], incoming: ConversationTurn[], investigationId: string,
): ConversationTurn[] {
  const merged = new Map(current.map(turn => [turn.turn_id, turn]));
  const incomingIds = new Set<string>();
  for (const turn of incoming) {
    if (turn.investigation_id !== investigationId || incomingIds.has(turn.turn_id)) {
      throw new Error('The conversation response has an invalid investigation or duplicate turn.');
    }
    incomingIds.add(turn.turn_id);
    const previous = merged.get(turn.turn_id);
    if (previous) {
      if (previous.context_run_id !== turn.context_run_id || previous.question !== turn.question || previous.created_at !== turn.created_at) {
        throw new Error('The saved conversation context changed unexpectedly.');
      }
      if (terminalStatuses.has(previous.status) || turn.updated_at < previous.updated_at) continue;
      if (previous.status === 'RUNNING' && turn.status === 'QUEUED') continue;
    }
    merged.set(turn.turn_id, turn);
  }
  return [...merged.values()].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.turn_id.localeCompare(b.turn_id)).slice(-100);
}

export type ConversationBlocked = 'unavailable' | 'no_context' | 'fixture' | 'running_context' | 'active' | 'empty' | 'too_long' | null;

/** Browser gates improve feedback; the server remains authoritative for every request. */
export function conversationBlocked(
  caps: ConversationCapabilities | null,
  context: { run_id: string; state: string; mode: string } | null,
  turns: ConversationTurn[], question: string,
): ConversationBlocked {
  if (!caps?.enabled) return 'unavailable';
  if (!context) return 'no_context';
  if (context.mode === 'fixture') return 'fixture';
  if (!terminalRunStates.has(context.state)) return 'running_context';
  if (activeConversationTurn(turns)) return 'active';
  if (!question.trim()) return 'empty';
  if (question.trim().length > Math.min(caps.max_question_chars, 4000)) return 'too_long';
  return null;
}

/** Do not make another run's citation look like evidence from this turn. */
export function scopedCitations(turn: ConversationTurn): { accepted: ConversationCitation[]; rejected: number } {
  const citations = turn.answer?.citations ?? [];
  const seen = new Set<string>();
  const accepted: ConversationCitation[] = [];
  let rejected = 0;
  for (const citation of citations) {
    if (citation.run_id !== turn.context_run_id || !citation.evidence_id) { rejected += 1; continue; }
    const signature = JSON.stringify([citation.run_id, citation.evidence_id]);
    if (!seen.has(signature)) { accepted.push(citation); seen.add(signature); }
  }
  return { accepted, rejected };
}
