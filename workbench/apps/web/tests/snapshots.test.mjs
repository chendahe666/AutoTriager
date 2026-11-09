import assert from 'node:assert/strict';
import test from 'node:test';
import { reconcileRun, reconcileRunList } from '../src/snapshots.ts';

const running = { run_id: 'run_a', revision: 2, last_sequence: 4, state: 'RUNNING', answer: null };
const completed = { run_id: 'run_a', revision: 4, last_sequence: 10, state: 'COMPLETED', answer: { answer_id: 'answer_a' } };

test('late running response cannot downgrade completed answer after terminal recovery', () => {
  let displayed = reconcileRun(running, completed);
  displayed = reconcileRun(displayed, running);
  assert.equal(displayed, completed);
  assert.equal(displayed.answer.answer_id, 'answer_a');
});

test('decreased revision or cursor is rejected even when the other version grows', () => {
  assert.equal(reconcileRun(completed, { ...completed, revision: 3, last_sequence: 11 }), completed);
  assert.equal(reconcileRun(completed, { ...completed, revision: 5, last_sequence: 9 }), completed);
});

test('an identical snapshot version cannot overwrite the displayed answer', () => {
  assert.equal(reconcileRun(completed, { ...completed, answer: null }), completed);
  assert.equal(reconcileRun(running, { ...running, state: 'COMPLETED' }), running);
});

test('late usage can advance terminal cursor without changing immutable prediction state', () => {
  const lateUsage = { ...completed, last_sequence: 11 };
  assert.equal(reconcileRun(completed, lateUsage), lateUsage);
  assert.equal(reconcileRun(completed, { ...lateUsage, revision: 5, state: 'RUNNING' }), completed);
  assert.equal(reconcileRun(completed, { ...lateUsage, revision: 5, state: 'FAILED' }), completed);
});

test('paginated and refreshed lists preserve newer matching snapshots and add new branches', () => {
  const child = { ...running, run_id: 'run_child' };
  assert.deepEqual(reconcileRunList([completed], [running, child], true), [completed, child]);
  assert.deepEqual(reconcileRunList([completed, child], [running], false), [completed]);
});
