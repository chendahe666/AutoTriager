import test from 'node:test';
import assert from 'node:assert/strict';
import { historicalMemorySources } from '../src/historicalMemory.ts';
import { scopedCitations } from '../src/conversation.ts';
import { investigationExport } from '../src/conversationExport.ts';

const item = { memory_id: 'memory_1', revision: 3, title: 'Historical observation', summary: 'An observation from another source.', source: { kind: 'public_packet', incident_id: 'incident_old', run_id: null, digest: 'digest_old' }, scope: { system_id: 'RE2-OB', service_ids: ['service_a'], data_revision: 'data_old' }, facts: [{ record: { message: 'DO_NOT_EXPORT_RAW_TABLE' } }], review: { actor_type: 'policy', note: 'Exact source match.', at: '2026-11-05T10:00:00Z', basis: 'checked' }, match_reason: ['shared service'], score: 2 };
const citation = { memory_id: 'memory_1', revision: 3, source_incident_id: 'incident_old', source_run_id: null };
const answer = { text: 'A historical observation may inform the next check.', citations: [{ run_id: 'run_current', evidence_id: 'ev_current' }], limitations: ['Not proof of the current cause.'], next_checks: [], model_id: 'test-model', memory_citations: [citation], memory_retrieval: { snapshot_id: 'snapshot_1', items: [item], limitations: ['Different source case.'] } };

test('old turns without memory metadata remain compatible and do not acquire invented history', () => {
  assert.deepEqual(historicalMemorySources(null), { snapshot: undefined, cited: [], rejected: [] });
  assert.equal(historicalMemorySources({ text: 'An old reply.' }).cited.length, 0);
});

test('historical sources join the exact pinned memory version and original source identities', () => {
  const joined = historicalMemorySources(answer);
  assert.equal(joined.cited.length, 1);
  assert.equal(joined.cited[0].item.review.actor_type, 'policy');
  assert.equal(joined.cited[0].citation.revision, 3);
  assert.equal(joined.snapshot.snapshot_id, 'snapshot_1');
  assert.equal(joined.rejected.length, 0);
});

test('a changed version, incident, run, or missing snapshot cannot be silently linked as the cited source', () => {
  for (const change of [{ revision: 4 }, { source_incident_id: 'incident_current' }, { source_run_id: 'run_foreign' }]) {
    const result = historicalMemorySources({ ...answer, memory_citations: [{ ...citation, ...change }] });
    assert.equal(result.cited.length, 0); assert.equal(result.rejected.length, 1);
  }
  assert.equal(historicalMemorySources({ ...answer, memory_retrieval: undefined }).rejected.length, 1);
});

test('historical references do not enter the current-run evidence namespace', () => {
  const turn = { context_run_id: 'run_current', answer };
  assert.deepEqual(scopedCitations(turn), { accepted: [{ run_id: 'run_current', evidence_id: 'ev_current' }], rejected: 0 });
  const joined = historicalMemorySources({ ...answer, memory_citations: [citation, citation] });
  assert.equal(joined.cited.length, 1);
});

test('a zero-result retrieval snapshot is distinguishable from no memory lookup on a legacy turn', () => {
  const result = historicalMemorySources({ ...answer, memory_citations: [], memory_retrieval: { snapshot_id: 'empty_snapshot', items: [], limitations: ['No eligible memory.'] } });
  assert.equal(result.snapshot.snapshot_id, 'empty_snapshot');
  assert.deepEqual(result.cited, []);
});

test('actual provider-shaped compact traces need no scope, fact table, score, or note fields', () => {
  const compactItem = { memory_id: item.memory_id, revision: item.revision, title: item.title, summary: item.summary, claim_kind: 'recorded_observation', causal_status: 'not_assessed', source: { kind: item.source.kind, incident_id: item.source.incident_id, run_id: null, investigation_id: null, data_revision: 'data_old', digest: item.source.digest }, review: { actor_type: 'policy', at: item.review.at, basis: 'Exact source match.' }, match_reason: 'Shared service.' };
  const compactAnswer = { ...answer, memory_retrieval: { snapshot_id: 'compact_snapshot', items: [compactItem], limitations: [] } };
  assert.equal(historicalMemorySources(compactAnswer).cited[0].item.memory_id, 'memory_1');
  const turn = { turn_id: 'turn_1', investigation_id: 'inv_current', context_run_id: 'run_current', question: 'What next?', status: 'COMPLETED', created_at: 'now', updated_at: 'now', context_digest: 'digest_current', answer: compactAnswer, error: null };
  const exported = investigationExport('inv_current', null, [turn], false, 'now').conversation.turns[0].answer;
  assert.equal(exported.memory_retrieval.snapshot_id, 'compact_snapshot');
  assert.equal('scope' in exported.memory_retrieval.items[0], false);
  assert.equal('facts' in exported.memory_retrieval.items[0], false);
});

test('record exports preserve pinned memory metadata separately and omit raw historical fact tables', () => {
  const turn = { turn_id: 'turn_1', investigation_id: 'inv_current', context_run_id: 'run_current', question: 'What next?', status: 'COMPLETED', created_at: 'now', updated_at: 'now', context_digest: 'digest_current', answer, error: null };
  const record = investigationExport('inv_current', null, [turn], false, 'now');
  const exported = record.conversation.turns[0].answer;
  assert.deepEqual(exported.memory_citations, [citation]);
  assert.equal(exported.memory_retrieval.snapshot_id, 'snapshot_1');
  assert.deepEqual(exported.citations, [{ run_id: 'run_current', evidence_id: 'ev_current' }]);
  assert.equal('facts' in exported.memory_retrieval.items[0], false);
  assert.doesNotMatch(JSON.stringify(record), /DO_NOT_EXPORT_RAW_TABLE/);
});
