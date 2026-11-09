import test from 'node:test';
import assert from 'node:assert/strict';
import { workflowCheckAction, groupWorkflowEvidence, groupWorkflowEvents } from '../src/incidentWorkflowPresentation.ts';

test('a proposed verification check uses the observation verifier, never the generic check or a repair endpoint', () => {
  assert.equal(workflowCheckAction({ check_id: 'later-window', verification_only: true }), 'verify');
  assert.equal(workflowCheckAction({ check_id: 'contacts/latency', verification_only: false }), 'checks/contacts%2Flatency/run');
});

test('latest request retains its complete multi-step investigation and earlier failed attempts remain inspectable', () => {
  const events = [
    { id: 'opened', kind: 'opened' }, { id: 'request-old', kind: 'requested' },
    { id: 'failure', kind: 'failed' }, { id: 'request-new', kind: 'requested' },
    { id: 'model-1', kind: 'analysis_started' }, { id: 'check', kind: 'check_finished' },
    { id: 'model-2', kind: 'analysis_started' }, { id: 'verification', kind: 'check_finished' },
    { id: 'review', kind: 'review_requested' },
  ];
  const groups = groupWorkflowEvents(events);
  assert.deepEqual(groups.current.map(event => event.id), ['request-new', 'model-1', 'check', 'model-2', 'verification', 'review']);
  assert.deepEqual(groups.earlier.map(event => event.id), ['opened', 'request-old', 'failure']);
  assert.deepEqual([...groups.earlier, ...groups.current], events);
  assert.deepEqual(groupWorkflowEvents(events.slice(0, 1)), { current: events.slice(0, 1), earlier: [] });
});

test('completed and recovery evidence is foregrounded without converting proposals or missing references into collected records', () => {
  const evidence = [
    { evidence_id: 'original', record: { source: 'archived request' } },
    { evidence_id: 'trace-check', record: { source: 'bounded trace query' }, observed_window: { start: '2022-08-01T00:00:00Z', end: '2022-08-01T00:01:00Z' } },
    { evidence_id: 'only-proposed', record: { source: 'initial context' } },
    { evidence_id: 'later-window', record: { source: 'later archived samples' } },
  ];
  const checks = [
    { status: 'completed', evidence_ids: ['trace-check', 'not-returned'], completed_at: '2026-11-06T12:00:00Z' },
    { status: 'proposed', evidence_ids: ['only-proposed'] },
  ];
  const groups = groupWorkflowEvidence(evidence, checks, ['later-window']);
  assert.deepEqual(groups.checked.map(value => value.evidence_id), ['trace-check', 'later-window']);
  assert.deepEqual(groups.initial.map(value => value.evidence_id), ['original', 'only-proposed']);
  assert.strictEqual(groups.checked[0], evidence[1]);
  assert.equal(groups.checked[0].observed_window.start, '2022-08-01T00:00:00Z');
  assert.equal('completed_at' in groups.checked[0], false);
  assert.equal(groups.checked.length + groups.initial.length, evidence.length);
});
