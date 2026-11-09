import test from 'node:test';
import assert from 'node:assert/strict';
import { activeConversationTurn, conversationBlocked, reconcileConversation, scopedCitations } from '../src/conversation.ts';
import { dictionary } from '../src/i18n.ts';

const caps = { enabled: true, model_id: 'test-model', disabled_reason: null, max_question_chars: 4000, max_history_turns: 12, budget: { max_calls: 100, max_nominal_usd: 2, used_calls: 0, reserved_nominal_usd: 0 }, evidence_scope: 'recorded_run' };
const context = { run_id: 'run_source', state: 'COMPLETED', mode: 'offline_replay' };
const makeTurn = (overrides = {}) => ({
  turn_id: 'turn_1', investigation_id: 'inv_1', context_run_id: 'run_source', question: 'Which evidence supports this?',
  status: 'QUEUED', created_at: '2026-11-05T10:00:00Z', updated_at: '2026-11-05T10:00:00Z', answer: null, error: null,
  context_digest: null, ...overrides,
});
const answer = { text: 'A recorded observation.', citations: [{ run_id: 'run_source', evidence_id: 'ev_1' }], limitations: ['Cause is unverified.'], next_checks: [], model_id: 'test-model' };

test('completed replies survive overlapping polling reads and stale queue snapshots', () => {
  const queued = makeTurn();
  const running = makeTurn({ status: 'RUNNING', updated_at: '2026-11-05T10:00:01Z' });
  const completed = makeTurn({ status: 'COMPLETED', answer, updated_at: '2026-11-05T10:00:02Z' });
  const state = reconcileConversation([running], [completed], 'inv_1');
  assert.deepEqual(reconcileConversation(state, [queued, makeTurn({ turn_id: 'turn_2', created_at: '2026-11-05T10:00:03Z' })], 'inv_1')[0], completed);
  assert.equal(reconcileConversation(state, [makeTurn({ status: 'RUNNING', updated_at: '2026-11-05T10:00:04Z' })], 'inv_1')[0], completed);
});

test('running state is not downgraded to queued by a newer overlapping read', () => {
  const running = makeTurn({ status: 'RUNNING' });
  assert.equal(reconcileConversation([running], [makeTurn({ updated_at: '2026-11-05T10:00:01Z' })], 'inv_1')[0], running);
});

test('failed turns remain durable failures rather than becoming invented successful replies', () => {
  const failed = makeTurn({ status: 'FAILED', error: { code: 'PROVIDER_UNAVAILABLE', message: 'Test failure.' } });
  const merged = reconcileConversation([failed], [makeTurn({ status: 'COMPLETED', answer, updated_at: '2026-11-05T10:00:02Z' })], 'inv_1');
  assert.equal(merged[0], failed);
  assert.equal(activeConversationTurn(merged), undefined);
});

test('another investigation or duplicate response identity is rejected', () => {
  assert.throws(() => reconcileConversation([], [makeTurn({ investigation_id: 'inv_foreign' })], 'inv_1'), /invalid investigation/);
  assert.throws(() => reconcileConversation([], [makeTurn(), makeTurn()], 'inv_1'), /duplicate turn/);
});

test('an existing turn cannot silently change its source run, question, or creation identity', () => {
  for (const changed of [{ context_run_id: 'run_other' }, { question: 'A different question' }, { created_at: '2026-11-05T09:59:00Z' }]) {
    assert.throws(() => reconcileConversation([makeTurn()], [makeTurn(changed)], 'inv_1'), /context changed/);
  }
});

test('history is ordered and retains failed questions alongside successful multi-turn replies', () => {
  const first = makeTurn({ status: 'COMPLETED', answer });
  const second = makeTurn({ turn_id: 'turn_2', status: 'FAILED', created_at: '2026-11-05T10:01:00Z' });
  assert.deepEqual(reconcileConversation([second], [first], 'inv_1').map(turn => turn.turn_id), ['turn_1', 'turn_2']);
});

test('a long-running open pane keeps the same latest-hundred history bound as the server', () => {
  const turns = Array.from({ length: 101 }, (_, index) => makeTurn({ turn_id: `turn_${String(index).padStart(3, '0')}`, created_at: `2026-11-05T10:${String(Math.floor(index / 60)).padStart(2, '0')}:${String(index % 60).padStart(2, '0')}Z` }));
  const merged = reconcileConversation(turns.slice(0, 100), turns.slice(100), 'inv_1');
  assert.equal(merged.length, 100);
  assert.equal(merged[0].turn_id, 'turn_001');
  assert.equal(merged.at(-1).turn_id, 'turn_100');
});

test('citations stay bound to their own turn source even when the interface selects another run', () => {
  const turn = makeTurn({ status: 'COMPLETED', answer: { ...answer, citations: [
    { run_id: 'run_source', evidence_id: 'ev_1' },
    { run_id: 'run_currently_selected_elsewhere', evidence_id: 'ev_2' },
    { run_id: 'run_source', evidence_id: 'ev_1' },
    { run_id: 'run_source', evidence_id: '' },
  ] } });
  assert.deepEqual(scopedCitations(turn), { accepted: [{ run_id: 'run_source', evidence_id: 'ev_1' }], rejected: 2 });
});

test('chat requires a separately enabled capability and a real terminal evidence context', () => {
  assert.equal(conversationBlocked(null, context, [], 'Why?'), 'unavailable');
  assert.equal(conversationBlocked({ ...caps, enabled: false }, context, [], 'Why?'), 'unavailable');
  assert.equal(conversationBlocked(caps, null, [], 'Why?'), 'no_context');
  assert.equal(conversationBlocked(caps, { ...context, mode: 'fixture' }, [], 'Why?'), 'fixture');
  for (const state of ['CREATED', 'QUEUED', 'RUNNING', 'VALIDATING', 'CANCEL_REQUESTED']) {
    assert.equal(conversationBlocked(caps, { ...context, state }, [], 'Why?'), 'running_context');
  }
  for (const state of ['COMPLETED', 'FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED', 'CANCELLED']) {
    assert.equal(conversationBlocked(caps, { ...context, state }, [], 'Why?'), null);
  }
});

test('one active conversation blocks another generation; a completed or failed turn does not', () => {
  for (const status of ['QUEUED', 'RUNNING']) assert.equal(conversationBlocked(caps, context, [makeTurn({ status })], 'Why?'), 'active');
  for (const status of ['COMPLETED', 'FAILED']) assert.equal(conversationBlocked(caps, context, [makeTurn({ status })], 'Why?'), null);
});

test('question limits respect the server and the local four-thousand-character ceiling', () => {
  assert.equal(conversationBlocked(caps, context, [], '  \n '), 'empty');
  assert.equal(conversationBlocked(caps, context, [], 'a'.repeat(4000)), null);
  assert.equal(conversationBlocked(caps, context, [], 'a'.repeat(4001)), 'too_long');
  assert.equal(conversationBlocked({ ...caps, max_question_chars: 20 }, context, [], 'a'.repeat(21)), 'too_long');
  assert.equal(conversationBlocked({ ...caps, max_question_chars: 8000 }, context, [], 'a'.repeat(4001)), 'too_long');
});

test('both interface languages cover the same conversation and existing application controls', () => {
  assert.deepEqual(Object.keys(dictionary.en).sort(), Object.keys(dictionary.zh).sort());
  assert.match(dictionary.en.chatScopeText, /selected run/);
  assert.notEqual(dictionary.en.chatTitle, dictionary.zh.chatTitle);
});
