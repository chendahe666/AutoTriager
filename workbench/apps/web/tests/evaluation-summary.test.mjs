import test from 'node:test';
import assert from 'node:assert/strict';
import { parseEvaluationSummary, percentage, nominalUsd, seconds } from '../src/evaluationSummary.ts';
import { dictionary } from '../src/i18n.ts';

function summary() {
  return {
    available: true, model_id: 'gemini-test', cohort_size: 90, completed_at: '2026-11-04T18:00:00Z',
    arms: [
      { id: 'B1', planned: 90, attempted: 90, completed: 90, failed: 0, ac_at_1: 85 / 90, ac_at_3: 1, ac_at_5: 1, avg_at_5: 0.9866666666666667, nominal_usd: 0.3239029, median_seconds: 2.807756 },
      { id: 'B2', planned: 90, attempted: 90, completed: 84, failed: 6, ac_at_1: 74 / 90, ac_at_3: 83 / 90, ac_at_5: 83 / 90, avg_at_5: 0.9, nominal_usd: 0.4457475, median_seconds: 12.908483 },
    ],
    usage: { generations: 288, input_tokens: 1601018, output_and_thinking_tokens: 115738, nominal_usd: 0.7696504, unresolved_calls: 0 },
    limitations: ['Development cases, not held-out confirmation.'],
    provenance: [{ path: 'verification/test/receipt.json', sha256: 'a'.repeat(64) }],
  };
}

test('recorded localization percentages retain all ninety cases, including six failed B2 answers', () => {
  const value = parseEvaluationSummary(summary());
  assert.equal(value.cohort_size, 90);
  assert.equal(value.arms[1].completed, 84);
  assert.equal(value.arms[1].failed, 6);
  assert.equal(percentage(value.arms[0].ac_at_1), '94.44%');
  assert.equal(percentage(value.arms[1].ac_at_1), '82.22%');
  assert.equal(percentage(value.arms[1].ac_at_5), '92.22%');
});

test('nominal costs and wall-clock units are formatted explicitly, without replacing missing usage by zero', () => {
  assert.equal(nominalUsd(0.3239029), 'US$0.323903');
  assert.equal(nominalUsd(0.4457475), 'US$0.445748');
  assert.equal(nominalUsd(0), 'US$0.000000');
  for (const absent of [undefined, NaN, Infinity, -1]) assert.equal(nominalUsd(absent), null);
  assert.equal(seconds(12.908483), '12.91 s');
});

test('an unavailable retained experiment remains unavailable rather than inventing score or cost values', () => {
  assert.deepEqual(parseEvaluationSummary({ available: false, reason: 'Receipts are missing.', arbitrary_provider_data: 'not displayed' }), { available: false, reason: 'Receipts are missing.' });
  assert.throws(() => parseEvaluationSummary({ available: false }), /Invalid recorded evaluation/);
});

test('inconsistent completion totals and another cohort denominator cannot be displayed', () => {
  for (const change of [{ completed: 85 }, { failed: 5 }, { attempted: 91 }, { planned: 84 }]) {
    const value = summary(); Object.assign(value.arms[1], change);
    assert.throws(() => parseEvaluationSummary(value), /case counts/);
  }
});

test('failed answers cannot be converted into top-k successes by an aggregate display', () => {
  const value = summary();
  value.arms[1].ac_at_5 = 1;
  assert.throws(() => parseEvaluationSummary(value), /localization metrics/);
});

test('nonfinite, nonmonotonic or out-of-range metrics and resource quantities are rejected', () => {
  for (const change of [{ ac_at_1: NaN }, { ac_at_3: 1.01 }, { ac_at_1: 0.99, ac_at_3: 0.9 }, { avg_at_5: 0.2 }, { nominal_usd: -1 }, { median_seconds: Infinity }]) {
    const value = summary(); Object.assign(value.arms[0], change);
    assert.throws(() => parseEvaluationSummary(value), /Invalid recorded/);
  }
});

test('method identity, usage units, and SHA-256 provenance remain explicit', () => {
  const duplicate = summary(); duplicate.arms[1].id = 'B1';
  assert.throws(() => parseEvaluationSummary(duplicate), /evaluation method/);
  const negative = summary(); negative.usage.input_tokens = -1;
  assert.throws(() => parseEvaluationSummary(negative), /usage summary/);
  const incomplete = summary(); delete incomplete.usage.output_and_thinking_tokens;
  assert.throws(() => parseEvaluationSummary(incomplete), /usage summary/);
  const badHash = summary(); badHash.provenance[0].sha256 = 'not a digest';
  assert.throws(() => parseEvaluationSummary(badHash), /provenance/);
});

test('both languages distinguish frozen CLI accuracy from current chat, invoices, and human recovery time', () => {
  assert.deepEqual(Object.keys(dictionary.en).sort(), Object.keys(dictionary.zh).sort());
  assert.match(dictionary.en.evalScopeText, /do not measure the accuracy of the current conversation/);
  assert.match(dictionary.en.evalAllowanceNote, /not an AWS account, Google invoice/);
  assert.match(dictionary.en.evalInterpretTime, /not engineer time saved or MTTR/);
});
