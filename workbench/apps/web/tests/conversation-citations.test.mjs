import test from 'node:test';
import assert from 'node:assert/strict';
import { conversationCitations } from '../src/conversationCitations.ts';

const turn = (text, citations) => ({ context_run_id: 'run_source', answer: { text, citations } });
const source = evidence_id => ({ run_id: 'run_source', evidence_id });

test('citation numbers follow the accepted source order and stay stable across repeated mentions', () => {
  const value = conversationCitations(turn('ev_two supports the comparison with ev_one; revisit ev_two.', [source('ev_one'), source('ev_two'), source('ev_one')]));
  assert.deepEqual(value.sources.map(item => [item.number, item.evidence_id]), [[1, 'ev_one'], [2, 'ev_two']]);
  assert.deepEqual(value.fragments.filter(item => item.kind === 'citation').map(item => item.number), [2, 1, 2]);
  assert.deepEqual(value.unknownInlineIds, []);
});

test('a foreign-run or unknown evidence ID is never promoted to a clickable source', () => {
  const value = conversationCitations(turn('ev_valid differs from ev_foreign and ev_unknown.', [source('ev_valid'), { run_id: 'run_foreign', evidence_id: 'ev_foreign' }]));
  assert.deepEqual(value.sources.map(item => item.evidence_id), ['ev_valid']);
  assert.deepEqual(value.fragments.filter(item => item.kind === 'citation').map(item => item.evidence_id), ['ev_valid']);
  assert.deepEqual(value.unknownInlineIds, ['ev_foreign', 'ev_unknown']);
  assert.match(value.fragments.filter(item => item.kind === 'text').map(item => item.text).join(''), /ev_foreign and ev_unknown/);
});

test('only a whole known identifier is linked, not a prefix inside another evidence ID', () => {
  const value = conversationCitations(turn('ev_one_suffix, ev_one.', [source('ev_one')]));
  assert.equal(value.fragments.filter(item => item.kind === 'citation').length, 1);
  assert.deepEqual(value.unknownInlineIds, ['ev_one_suffix']);
});

test('dotted or repeated-punctuation suffixes stay wholly unlinked, while a sentence period is punctuation', () => {
  for (const unknown of ['ev_one.other', 'ev_one..other', 'ev_one-other']) {
    const value = conversationCitations(turn(unknown, [source('ev_one')]));
    assert.equal(value.fragments.filter(item => item.kind === 'citation').length, 0);
    assert.deepEqual(value.unknownInlineIds, [unknown]);
    assert.equal(value.fragments[0].text, unknown);
  }
  const dotted = conversationCitations(turn('ev_a.b.suffix', [source('ev_a.b')]));
  assert.equal(dotted.fragments.filter(item => item.kind === 'citation').length, 0);
  assert.deepEqual(dotted.unknownInlineIds, ['ev_a.b.suffix']);
  for (const known of ['ev_one', 'ev_a.b']) {
    const sentence = conversationCitations(turn(`${known}.`, [source(known)]));
    assert.equal(sentence.fragments.filter(item => item.kind === 'citation').length, 1);
    assert.deepEqual(sentence.unknownInlineIds, []);
    assert.equal(sentence.fragments.at(-1).text, '.');
  }
});

test('citation wrappers become one numbered link without interpreting generated HTML', () => {
  const original = '<script>[`ev_one`]</script> and `ev_two`.';
  const record = turn(original, [source('ev_one'), source('ev_two')]);
  const value = conversationCitations(record);
  assert.deepEqual(value.fragments.map(item => item.kind === 'text' ? item.text : `[${item.number}]`), ['<script>', '[1]', '</script> and ', '[2]', '.']);
  assert.equal(record.answer.text, original);
});

test('identifier punctuation is matched literally and original source-run binding is preserved', () => {
  const value = conversationCitations(turn('ev_a.b is distinct from ev_axb.', [source('ev_a.b')]));
  const linked = value.fragments.filter(item => item.kind === 'citation');
  assert.equal(linked.length, 1);
  assert.equal(linked[0].run_id, 'run_source');
  assert.equal(linked[0].evidence_id, 'ev_a.b');
  assert.deepEqual(value.unknownInlineIds, ['ev_axb']);
});

test('answers without supplied sources stay plain text and do not gain invented provenance', () => {
  const value = conversationCitations(turn('Inspect ev_unknown before deciding.', []));
  assert.deepEqual(value.fragments, [{ kind: 'text', text: 'Inspect ev_unknown before deciding.' }]);
  assert.deepEqual(value.sources, []);
  assert.deepEqual(conversationCitations({ context_run_id: 'run_source', answer: null }).sources, []);
});
