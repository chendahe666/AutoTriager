import test from 'node:test';
import assert from 'node:assert/strict';
import { investigationExport } from '../src/conversationExport.ts';

const recordedAnswer = { answer_id: 'answer_1', status: 'insufficient_evidence', candidates: [], observations: [{ text: 'A recorded observation', evidence_ids: ['ev_1'] }], unknowns: ['Cause not established'], next_checks: [], structure_valid: true, reference_valid: true, semantic_support_status: 'not_assessed', raw_output_digest: 'digest_1' };
const run = {
  run_id: 'run_source', investigation_id: 'inv_1', incident_id: 'incident_1', parent_run_id: null,
  mode: 'offline_replay', state: 'COMPLETED', created_at: '2026-11-05T10:00:00Z', updated_at: '2026-11-05T10:00:01Z',
  question: 'What is recorded?', termination_code: null, config_digest: 'config_digest', answer: recordedAnswer,
  config: { resolved_model_id: 'recorded-model', policy_id: 'offline_inspection', policy_version: '1', data_revision: 'revision_1', adapter_version: '1', api_key: 'DO_NOT_EXPORT_KEY' },
  raw_provider_payload: 'DO_NOT_EXPORT_PROVIDER_PAYLOAD', private_labels: 'DO_NOT_EXPORT_LABELS',
};
const turn = {
  turn_id: 'turn_1', investigation_id: 'inv_1', context_run_id: 'run_source', question: 'What is unknown?', status: 'COMPLETED',
  created_at: '2026-11-05T10:01:00Z', updated_at: '2026-11-05T10:01:01Z', context_digest: 'context_digest', error: null,
  answer: { text: 'Cause remains unknown.', citations: [{ run_id: 'run_source', evidence_id: 'ev_1' }], limitations: ['Not a causal verification'], next_checks: ['Compare the candidates.'], model_id: 'test-model', secret_provider_fields: 'DO_NOT_EXPORT_SECRET' },
  raw_provider_payload: 'DO_NOT_EXPORT_TURN_PAYLOAD',
};

test('JSON export preserves selected recorded context and multi-turn evidence while excluding unlisted server fields', () => {
  const exported = investigationExport('inv_1', run, [turn], false, '2026-11-05T11:00:00Z');
  assert.equal(exported.selected_run.run_id, 'run_source');
  assert.deepEqual(exported.selected_run.answer.observations, recordedAnswer.observations);
  assert.equal(exported.conversation.turns[0].answer.text, turn.answer.text);
  assert.deepEqual(exported.conversation.turns[0].answer.citations, turn.answer.citations);
  assert.doesNotMatch(JSON.stringify(exported), /DO_NOT_EXPORT/);
  assert.match(exported.scope, /Raw telemetry.*excluded/);
});

test('exports cannot combine another investigation and do not silently include cross-run citations', () => {
  assert.throws(() => investigationExport('inv_foreign', run, [turn], false, 'now'), /different investigations/);
  assert.throws(() => investigationExport('inv_1', run, [{ ...turn, investigation_id: 'inv_foreign' }], false, 'now'), /different investigations/);
  const exported = investigationExport('inv_1', run, [{ ...turn, answer: { ...turn.answer, citations: [{ run_id: 'run_foreign', evidence_id: 'ev_foreign' }] } }], false, 'now');
  assert.deepEqual(exported.conversation.turns[0].answer.citations, []);
  assert.equal(exported.conversation.turns[0].omitted_out_of_scope_citations, 1);
});

test('export declares a truncated history and retains failures without inventing answers', () => {
  const history = Array.from({ length: 101 }, (_, index) => ({ ...turn, turn_id: `turn_${index}`, status: 'FAILED', answer: null, error: { code: 'PROVIDER_ERROR', message: 'The request failed.' } }));
  const exported = investigationExport('inv_1', null, history, false, 'now');
  assert.equal(exported.selected_run, null);
  assert.equal(exported.conversation.truncated, true);
  assert.equal(exported.conversation.turns.length, 100);
  assert.equal(exported.conversation.turns[0].turn_id, 'turn_1');
  assert.equal(exported.conversation.turns[0].answer, null);
  assert.equal(exported.conversation.turns[0].error.code, 'PROVIDER_ERROR');
});
