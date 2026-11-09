import type { MemoryCitation, MemoryRetrieval } from './knowledgeTypes';
export type ConversationStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED';

export interface ConversationCitation {
  run_id: string;
  evidence_id: string;
}

export interface ConversationAnswer {
  text: string;
  citations: ConversationCitation[];
  limitations: string[];
  next_checks: string[];
  model_id: string;
  memory_citations?: MemoryCitation[];
  memory_retrieval?: MemoryRetrieval;
}

export interface ConversationTurn {
  turn_id: string;
  investigation_id: string;
  context_run_id: string;
  question: string;
  status: ConversationStatus;
  created_at: string;
  updated_at: string;
  answer: ConversationAnswer | null;
  error: { code: string; message: string } | null;
  context_digest: string | null;
}

export interface ConversationHistory {
  investigation_id: string;
  turns: ConversationTurn[];
  truncated: boolean;
}

export interface ConversationCapabilities {
  enabled: boolean;
  model_id: string | null;
  disabled_reason: string | null;
  max_question_chars: number;
  max_history_turns: number;
  budget: {
    max_calls: number;
    max_nominal_usd: number;
    used_calls: number;
    reserved_nominal_usd: number;
    observed_nominal_usd?: number;
  };
  evidence_scope: 'recorded_run';
}
