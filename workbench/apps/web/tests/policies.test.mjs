import test from 'node:test';
import assert from 'node:assert/strict';
import { inspectionPolicy } from '../src/policies.ts';

test('actual-data and fixture actions select their explicit matching policy', () => {
  const allowed = ['synthetic_readonly', 'offline_inspection'];
  assert.equal(inspectionPolicy('fixture', allowed), 'synthetic_readonly');
  assert.equal(inspectionPolicy('offline_replay', allowed), 'offline_inspection');
});
test('missing or unsupported policy never falls back to a model policy or array order', () => {
  assert.equal(inspectionPolicy('offline_replay', ['synthetic_readonly', 'model_auto']), undefined);
  assert.equal(inspectionPolicy('live', ['offline_inspection']), undefined);
  assert.equal(inspectionPolicy(undefined, ['synthetic_readonly']), undefined);
});
