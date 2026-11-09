import assert from 'node:assert/strict';
import test from 'node:test';
import { parseSymptomComparison } from '../src/symptomComparisonRecord.ts';

function record() {
  const ids = ['B0', 'B1', 'B2'];
  return {
    schema_version: 1,
    dataset_label: 'Test record',
    dataset_url: 'https://example.org/benchmark',
    cohort_size: 2,
    methodology: 'Test protocol',
    limitations: ['Test limitation'],
    arms: ids.map(id => ({ id, label: `Test ${id}`, completed: 1, top1_correct: 0, top3_correct: 1 })),
    pairs: [{
      id: 'pair-1', symptom: 'Matched symptom', match_reason: 'Test profile',
      cases: ['service-a', 'service-b'].map((truth, index) => ({
        id: `case-${index}`, system: 'Test system', signal: 'Test signal',
        evidence: [{ source: 'Test evidence', detail: 'Test telemetry' }], truth,
        predictions: ids.map(arm_id => index === 0
          ? { arm_id, status: 'completed', ranking: ['frontend', truth] }
          : { arm_id, status: 'missing', ranking: [] }),
      })),
    }],
  };
}

test('reads missing and completed predictions without dropping cases from the denominator', () => {
  const parsed = parseSymptomComparison(record());
  assert.equal(parsed.cohort_size, 2);
  assert.equal(parsed.pairs[0].cases[1].predictions[0].status, 'missing');
});

test('rejects an invalid matched pair or inconsistent score counts', () => {
  const sameRoot = record();
  sameRoot.pairs[0].cases[1].truth = sameRoot.pairs[0].cases[0].truth;
  assert.throws(() => parseSymptomComparison(sameRoot), /different labeled root services/);
  const counts = record();
  counts.arms[0].top3_correct = 2;
  assert.throws(() => parseSymptomComparison(counts), /Inconsistent comparison counts/);
});

test('rejects unscored rankings presented as returned candidates', () => {
  const data = record();
  data.pairs[0].cases[1].predictions[0].ranking = ['service-b'];
  assert.throws(() => parseSymptomComparison(data), /Invalid case prediction/);
});
