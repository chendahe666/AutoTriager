import test from 'node:test';
import assert from 'node:assert/strict';
import { RequestScope } from '../src/requestScope.ts';

test('settlements from a previous investigation cannot update the next investigation', () => {
  const guard = new RequestScope();
  const a = guard.enter('inv_A/run_A');
  const b = guard.enter('inv_B/run_B');
  assert.equal(guard.isCurrent(a), false);
  assert.equal(guard.isCurrent(b), true);
  // A late cleanup must not invalidate the already active B scope.
  guard.invalidate(a);
  assert.equal(guard.isCurrent(b), true);
});

test('switching runs or leaving and reopening the same route invalidates an old read or cancellation receipt', () => {
  const guard = new RequestScope();
  const first = guard.enter('inv_A/run_A');
  guard.enter('inv_A/run_other');
  const reopened = guard.enter('inv_A/run_A');
  assert.equal(guard.isCurrent(first), false);
  assert.equal(guard.isCurrent(reopened), true);
  guard.invalidate(reopened);
  assert.equal(guard.isCurrent(reopened), false);
  assert.equal(guard.capture(), null);
  const fresh = guard.enter('inv_A/run_A');
  assert.equal(guard.isCurrent(first), false);
  assert.equal(guard.isCurrent(fresh), true);
});

test('ordinary renders of the same source do not discard its pending secondary read', () => {
  const guard = new RequestScope();
  const ticket = guard.enter('inv_A/run_A');
  assert.deepEqual(guard.enter('inv_A/run_A'), ticket);
  assert.deepEqual(guard.capture(), ticket);
  assert.equal(guard.isCurrent(ticket), true);
});
