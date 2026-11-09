import test from 'node:test';
import assert from 'node:assert/strict';
import { investigationSummary, recordedModel } from '../src/summaryProjection.ts';

const investigation = { investigation_id: 'inv_1', incident_id: 'incident_1', latest_run_id: 'run_other' };
const incident = { incident_id: 'incident_1', system_id: 'RE2-OB', time_window: { start: '2026-11-05T09:00:00Z', end: '2026-11-05T10:00:00Z' }, service_ids: ['checkoutservice', 'currencyservice'], modalities: { metrics: { availability: 'present', record_count: 0 }, logs: { availability: 'absent', record_count: null }, traces: { availability: 'unknown', record_count: null } }, coverage_warnings: ['Logs unavailable.'] };
const run = { run_id: 'run_selected', investigation_id: 'inv_1', incident_id: 'incident_1', state: 'COMPLETED', mode: 'offline_replay', question: 'Which records are available?', updated_at: '2026-11-05T10:00:00Z', config: { policy_id: 'offline_inspection', resolved_model_id: null, data_revision: 'data_1' }, answer: { status: 'insufficient_evidence', observations: [{ text: 'A saved metric change.', evidence_ids: ['ev_original'] }], candidates: [], unknowns: ['Causal mechanism unverified.'], next_checks: ['Compare contemporaneous records.'], semantic_support_status: 'not_assessed' }, severity: 'invented', root_cause: 'invented', customer_impact: 'invented' };

test('the selected saved run is summarized, rather than a later run or a newly generated diagnosis', () => {
  const value = investigationSummary(investigation, incident, run);
  assert.equal(value.run_id, 'run_selected');
  assert.deepEqual(value.observations, run.answer.observations);
  assert.deepEqual(value.candidates, []);
  assert.deepEqual(value.unknowns, ['Causal mechanism unverified.']);
  assert.equal(value.semantic_support_status, 'not_assessed');
  for (const invented of ['root_cause', 'severity', 'customer_impact']) assert.equal(invented in value, false);
});

test('zero-model inspection, unrecorded model identity, and an actual saved model are different states', () => {
  assert.deepEqual(recordedModel(run), { kind: 'no_model', modelId: null });
  assert.deepEqual(recordedModel(null), { kind: 'no_run', modelId: null });
  assert.deepEqual(recordedModel({ ...run, config: { ...run.config, policy_id: 'model_policy' } }), { kind: 'unknown', modelId: null });
  assert.deepEqual(recordedModel({ ...run, config: { ...run.config, resolved_model_id: 'saved-model' } }), { kind: 'recorded', modelId: 'saved-model' });
});

test('saved summaries reject another investigation or incident instead of combining their evidence', () => {
  assert.throws(() => investigationSummary(investigation, incident, { ...run, investigation_id: 'inv_foreign' }), /same investigation/);
  assert.throws(() => investigationSummary(investigation, incident, { ...run, incident_id: 'incident_foreign' }), /same investigation/);
  assert.throws(() => investigationSummary(investigation, { ...incident, incident_id: 'incident_foreign' }, run), /same investigation/);
});

test('missing answers and unavailable telemetry remain distinct from no matching records', () => {
  const value = investigationSummary(investigation, incident, { ...run, state: 'FAILED', answer: null });
  assert.equal(value.answer_status, null);
  assert.equal(value.state, 'FAILED');
  assert.deepEqual(value.candidates, []);
  assert.equal(value.modalities.metrics.availability, 'present');
  assert.equal(value.modalities.metrics.record_count, 0);
  assert.equal(value.modalities.logs.availability, 'absent');
  assert.equal(value.modalities.logs.record_count, null);
});
