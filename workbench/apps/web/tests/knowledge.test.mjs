import test from 'node:test';
import assert from 'node:assert/strict';
import { filterMemories, knowledgePath, memoryActions, memoryDecision, memoryStates, parseKnowledgeIndex, parseMemoryEntry, reconcileMemoryEntry, reviewKind } from '../src/knowledgeModel.ts';
import { dictionary } from '../src/i18n.ts';

const fact = { fact_id: 'fact_1', modality: 'logs', service_id: 'service_a', evidence_id: 'ev_source', record: { message: 'A literal source observation.' }, unit: 'unknown', source_artifact_digest: 'a'.repeat(64), record_locator: 'row:1' };
const entry = (change = {}) => ({ memory_id: 'memory_1', revision: 1, state: 'CANDIDATE', title: 'Recorded observations', summary: 'One preserved observation.', producer: 'deterministic', claim_kind: 'recorded_observation', causal_status: 'not_assessed', source: { kind: 'recorded_run', investigation_id: 'inv_source', run_id: 'run_source', incident_id: 'incident_source', data_revision: 'data_1', digest: 'b'.repeat(64), evidence_refs: [{ evidence_id: 'ev_source', source_artifact_digest: 'a'.repeat(64), record_locator: 'row:1' }], source_artifact_digests: ['a'.repeat(64)], group_id: null }, scope: { system_id: 'RE2-OB', service_ids: ['service_a'], data_revision: 'data_1' }, facts: [fact], checks: [{ id: 'exact_source_match', status: 'pass', detail: 'Matches preserved record.' }], capture: { automatic: true, at: '2026-11-05T10:00:00Z' }, review: null, publication: null, ...change });
const summary = value => { const { facts, ...rest } = value; return { ...rest, fact_count: facts.length }; };
const counts = change => ({ CANDIDATE: 0, REVIEWED: 0, PUBLISHED: 0, QUARANTINED: 0, REVOKED: 0, ...change });

test('knowledge summaries and details distinguish automatic observation capture from unreviewed hypotheses', () => {
  const original = entry();
  assert.equal(parseMemoryEntry(original).capture.automatic, true);
  assert.equal(parseMemoryEntry(original).causal_status, 'not_assessed');
  const index = parseKnowledgeIndex({ enabled: true, items: [summary(original)], truncated: false, counts: counts({ CANDIDATE: 1 }) });
  assert.equal(index.items[0].fact_count, 1);
  assert.equal('facts' in index.items[0], false);
});

test('disabled knowledge and failed reads cannot be converted into an invented nonempty source list', () => {
  assert.deepEqual(parseKnowledgeIndex({ enabled: false, items: [], truncated: false, counts: {}, reason: 'Not enabled.' }).items, []);
  assert.throws(() => parseKnowledgeIndex(null), /Invalid knowledge/);
  assert.throws(() => parseMemoryEntry(summary(entry())), /source observation/);
});

test('answer injection status distinguishes enabled, disabled, and an older server that omits the flag', () => {
  const base = { enabled: true, items: [], truncated: false, counts: counts() };
  assert.equal(parseKnowledgeIndex({ ...base, answer_injection_enabled: true }).answer_injection_enabled, true);
  assert.equal(parseKnowledgeIndex({ ...base, answer_injection_enabled: false }).answer_injection_enabled, false);
  assert.equal(parseKnowledgeIndex(base).answer_injection_enabled, undefined);
  assert.match(dictionary.en.memoryAnswerInjectionOff, /not included in new AI answers/);
  assert.match(dictionary.en.memoryAnswerInjectionUnknown, /has not reported/);
});

test('a non-boolean answer injection flag cannot masquerade as an enabled or disabled answer path', () => {
  for (const flag of [null, 0, 1, 'false', 'true']) {
    assert.throws(() => parseKnowledgeIndex({ enabled: true, items: [], truncated: false, counts: counts(), answer_injection_enabled: flag }), /answer integration status/);
  }
});

test('owner totals, unique record IDs, and the latest-hundred bound are checked before display', () => {
  const record = summary(entry());
  assert.throws(() => parseKnowledgeIndex({ enabled: true, items: [record], truncated: false, counts: counts() }), /owner totals/);
  assert.throws(() => parseKnowledgeIndex({ enabled: true, items: [record, record], truncated: false, counts: counts({ CANDIDATE: 2 }) }), /duplicate identity/);
  assert.throws(() => parseKnowledgeIndex({ enabled: true, items: Array.from({ length: 101 }, (_, i) => ({ ...record, memory_id: `memory_${i}` })), truncated: true, counts: counts({ CANDIDATE: 101 }) }), /index scope/);
});

test('state and source-type checks gate decisions, rather than a model or operator approval alone', () => {
  assert.deepEqual(memoryActions(entry()), { review: true, publish: false, revoke: false });
  assert.deepEqual(memoryActions(entry({ state: 'QUARANTINED', producer: 'ai_proposed' })), { review: true, publish: false, revoke: false });
  const reviewed = entry({ state: 'REVIEWED', review: { actor_type: 'human', note: 'Inspected sources.', at: 'now', basis: 'owner decision' } });
  assert.equal(memoryActions(reviewed).publish, true);
  for (const change of [
    { producer: 'ai_proposed' }, { claim_kind: 'root_cause' }, { causal_status: 'verified' },
    { checks: [] }, { checks: [{ id: 'source', status: 'fail', detail: 'Mismatch.' }] }, { checks: [{ id: 'source', status: 'unknown', detail: 'Not established.' }] },
  ]) assert.equal(memoryActions({ ...reviewed, ...change }).publish, false);
});

test('publication and revocation remain distinct from review and do not permit silent restoration', () => {
  assert.deepEqual(memoryActions(entry({ state: 'PUBLISHED' })), { review: false, publish: false, revoke: true });
  assert.deepEqual(memoryActions(entry({ state: 'REVOKED' })), { review: false, publish: false, revoke: false });
});

test('decision bodies retain optimistic revision and do not claim an operator or policy identity', () => {
  assert.deepEqual(memoryDecision(entry({ revision: 7 }), 'review', '  Sources inspected.  '), { expected_revision: 7, note: 'Sources inspected.' });
  assert.deepEqual(memoryDecision(entry({ revision: 8, state: 'REVIEWED' }), 'publish', 'Reuse this observation.'), { expected_revision: 8, reason: 'Reuse this observation.' });
  assert.throws(() => memoryDecision(entry(), 'publish', 'Force it.'), /unavailable/);
  assert.throws(() => memoryDecision(entry(), 'review', ' '), /requires a note/);
  assert.throws(() => memoryDecision(entry(), 'review', 'a'.repeat(1001)), /requires a note/);
});

test('older GETs or idempotent decision receipts cannot undo a later revocation', () => {
  const revoked = entry({ revision: 4, state: 'REVOKED' });
  for (const earlier of [entry({ revision: 1 }), entry({ revision: 2, state: 'REVIEWED' }), entry({ revision: 3, state: 'PUBLISHED' })]) assert.equal(reconcileMemoryEntry(revoked, earlier), revoked);
  assert.equal(reconcileMemoryEntry(revoked, entry({ revision: 4, state: 'PUBLISHED' })), revoked);
  assert.equal(reconcileMemoryEntry(revoked, entry({ revision: 5, state: 'PUBLISHED' })), revoked);
  assert.throws(() => reconcileMemoryEntry(revoked, entry({ memory_id: 'memory_foreign' })), /another record/);
});

test('a legitimate newer decision can advance a candidate without rewriting the existing snapshot', () => {
  const candidate = entry(); const published = entry({ revision: 3, state: 'PUBLISHED' });
  assert.equal(reconcileMemoryEntry(candidate, published), published);
  assert.equal(candidate.state, 'CANDIDATE');
  assert.equal(reconcileMemoryEntry(null, candidate), candidate);
});

test('policy source matching is not labeled as human review or expert certification', () => {
  assert.equal(reviewKind('policy'), 'policy'); assert.equal(reviewKind('human'), 'human');
  assert.equal(reviewKind('automation'), 'other'); assert.equal(reviewKind(undefined), 'none');
  assert.notEqual(dictionary.en.memoryPolicyReview, dictionary.en.memoryHumanReview);
  assert.match(dictionary.en.memoryMeaningText, /does not independently verify a root cause/);
  assert.deepEqual(Object.keys(dictionary.en).sort(), Object.keys(dictionary.zh).sort());
});

test('loaded search and historical version links preserve source identities without broadening scope', () => {
  const values = [summary(entry()), summary(entry({ memory_id: 'memory_2', state: 'PUBLISHED', title: 'Other event', scope: { system_id: 'RE2-OB', service_ids: ['service_b'], data_revision: 'data_1' } }))];
  assert.equal(filterMemories(values, 'SERVICE_B', 'PUBLISHED')[0], values[1]);
  assert.deepEqual(filterMemories(values, 'service_b', 'CANDIDATE'), []);
  assert.equal(filterMemories(values, 'run_source').length, 2);
  assert.equal(knowledgePath('memory/one', 3), '/knowledge/memory%2Fone/reference/3');
  assert.equal(memoryStates.length, 5);
});
