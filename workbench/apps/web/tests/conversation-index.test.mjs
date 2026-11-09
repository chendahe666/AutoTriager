import test from 'node:test';
import assert from 'node:assert/strict';
import { conversationThreadPath, filterConversationThreads, parseConversationIndex } from '../src/conversationIndex.ts';

const thread = (overrides = {}) => ({ investigation_id: 'inv_1', title: 'Recorded checkout investigation', incident_id: 'incident_1', turn_count: 2, last_turn_id: 'turn_2', last_question: 'Which observation still needs verification?', last_status: 'COMPLETED', updated_at: '2026-11-05T10:00:00Z', context_run_id: 'run_source', ...overrides });

test('conversation index distinguishes an empty disabled service from an enabled service with no threads', () => {
  assert.deepEqual(parseConversationIndex({ enabled: false, threads: [], truncated: false }), { enabled: false, threads: [], truncated: false });
  assert.equal(parseConversationIndex({ enabled: true, threads: [], truncated: false }).enabled, true);
});

test('loaded index search preserves original records and does not imply a query of all historical turns', () => {
  const threads = [thread(), thread({ investigation_id: 'inv_2', title: 'Currency service', incident_id: 'incident_2', last_question: 'What is missing?', context_run_id: 'run_two' })];
  assert.equal(filterConversationThreads(threads, 'CURRENCY')[0], threads[1]);
  assert.equal(filterConversationThreads(threads, 'run_source')[0], threads[0]);
  assert.equal(filterConversationThreads(threads, 'verification')[0], threads[0]);
  assert.deepEqual(filterConversationThreads(threads, 'not in this loaded index'), []);
  assert.equal(filterConversationThreads(threads, '  '), threads);
});

test('thread links reopen the saved source run and explicitly request chat focus', () => {
  const path = conversationThreadPath(thread({ investigation_id: 'inv/one', context_run_id: 'run two' }));
  assert.equal(path, '/investigations/inv%2Fone/runs/run%20two/chat');
  assert.equal(decodeURIComponent(path.split('/')[2]), 'inv/one');
  assert.equal(decodeURIComponent(path.split('/')[4]), 'run two');
});

test('index rejects impossible thread identities, statuses, and more than the declared hundred records', () => {
  for (const invalid of [thread({ turn_count: 0 }), thread({ last_status: 'UNKNOWN' }), thread({ context_run_id: '' }), thread({ updated_at: 'not a time' })]) {
    assert.throws(() => parseConversationIndex({ enabled: true, threads: [invalid], truncated: false }), /Invalid saved/);
  }
  assert.throws(() => parseConversationIndex({ enabled: true, threads: [thread(), thread()], truncated: false }), /Invalid saved/);
  assert.throws(() => parseConversationIndex({ enabled: true, threads: Array.from({ length: 101 }, (_, i) => thread({ investigation_id: `inv_${i}` })), truncated: true }), /scope/);
});

test('a latest failed reply remains failed and the server ordering is retained', () => {
  const recent = thread({ investigation_id: 'inv_recent', last_status: 'FAILED', last_question: 'Unanswered question', turn_count: 3 });
  const older = thread({ updated_at: '2026-11-05T09:00:00Z' });
  const index = parseConversationIndex({ enabled: true, threads: [recent, older], truncated: true });
  assert.equal(index.threads[0].last_status, 'FAILED');
  assert.equal(index.threads[0].turn_count, 3);
  assert.equal(index.truncated, true);
});
