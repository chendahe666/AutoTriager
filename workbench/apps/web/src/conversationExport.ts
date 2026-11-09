import { scopedCitations } from './conversation.ts';
import type { ConversationAnswer, ConversationTurn } from './conversationTypes';
import type { Answer, Run } from './types';

function recordedAnswer(answer: Answer | null) {
  if (!answer) return null;
  return {
    answer_id: answer.answer_id, status: answer.status,
    candidates: answer.candidates.map(candidate => ({
      rank: candidate.rank, service_id: candidate.service_id, hypothesis: candidate.hypothesis,
      supporting_evidence_ids: candidate.supporting_evidence_ids,
      contradicting_evidence_ids: candidate.contradicting_evidence_ids,
      missing_checks: candidate.missing_checks,
    })),
    observations: answer.observations.map(item => ({ text: item.text, evidence_ids: item.evidence_ids })),
    unknowns: answer.unknowns, next_checks: answer.next_checks,
    structure_valid: answer.structure_valid, reference_valid: answer.reference_valid,
    semantic_support_status: answer.semantic_support_status, raw_output_digest: answer.raw_output_digest,
  };
}

function conversationAnswer(answer: ConversationAnswer | null, turn: ConversationTurn) {
  if (!answer) return null;
  return {
    text: answer.text, citations: scopedCitations(turn).accepted, limitations: answer.limitations, next_checks: answer.next_checks, model_id: answer.model_id,
    ...(answer.memory_citations ? { memory_citations: answer.memory_citations.map(item => ({ memory_id: item.memory_id, revision: item.revision, source_incident_id: item.source_incident_id, source_run_id: item.source_run_id })) } : {}),
    ...(answer.memory_retrieval ? { memory_retrieval: {
      snapshot_id: answer.memory_retrieval.snapshot_id,
      items: answer.memory_retrieval.items.map(item => ({
        memory_id: item.memory_id, revision: item.revision, title: item.title, summary: item.summary,
        source: { kind: item.source.kind, incident_id: item.source.incident_id, run_id: item.source.run_id, digest: item.source.digest },
        ...(item.scope ? { scope: { system_id: item.scope.system_id, service_ids: item.scope.service_ids, data_revision: item.scope.data_revision } } : {}),
        review: item.review ? { actor_type: item.review.actor_type, note: item.review.note, at: item.review.at } : null,
        match_reason: item.match_reason, score: item.score,
      })), limitations: answer.memory_retrieval.limitations,
    } } : {}),
  };
}

/** Export displayed records only; never serialize whole server responses or provider configuration. */
export function investigationExport(
  investigationId: string, run: Run | null, turns: ConversationTurn[], truncated: boolean, exportedAt: string,
) {
  if ((run && run.investigation_id !== investigationId) || turns.some(turn => turn.investigation_id !== investigationId)) {
    throw new Error('An export cannot combine records from different investigations.');
  }
  return {
    format: 'autotriager.investigation-record.v1', exported_at: exportedAt,
    investigation_id: investigationId,
    scope: 'Selected recorded run and loaded conversation only. Raw telemetry, evaluator labels, credentials and provider payloads are excluded.',
    selected_run: run ? {
      run_id: run.run_id, investigation_id: run.investigation_id, incident_id: run.incident_id,
      parent_run_id: run.parent_run_id, mode: run.mode, state: run.state,
      created_at: run.created_at, updated_at: run.updated_at, question: run.question,
      termination_code: run.termination_code, config_digest: run.config_digest,
      configuration: {
        resolved_model_id: run.config.resolved_model_id, policy_id: run.config.policy_id,
        policy_version: run.config.policy_version, data_revision: run.config.data_revision,
        adapter_version: run.config.adapter_version,
      },
      answer: recordedAnswer(run.answer),
    } : null,
    conversation: {
      truncated: truncated || turns.length > 100,
      turns: turns.slice(-100).map(turn => ({
        turn_id: turn.turn_id, investigation_id: turn.investigation_id,
        context_run_id: turn.context_run_id, question: turn.question, status: turn.status,
        created_at: turn.created_at, updated_at: turn.updated_at, context_digest: turn.context_digest,
        answer: conversationAnswer(turn.answer, turn),
        error: turn.error ? { code: turn.error.code, message: turn.error.message } : null,
        omitted_out_of_scope_citations: scopedCitations(turn).rejected,
      })),
    },
    limitations: [
      'Saved answers and citations do not independently verify causal or semantic correctness.',
      'Suggested checks have not been executed by this conversation feature.',
    ],
  };
}
