/** Journal cursors may grow after a terminal prediction; its state cannot change. */
export interface RunSnapshot {
  run_id: string;
  revision: number;
  last_sequence: number;
  state: string;
}

const terminal = new Set(['COMPLETED', 'FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED', 'CANCELLED']);

/** Reject stale concurrent reads, including reads that arrive after terminal recovery. */
export function reconcileRun<T extends RunSnapshot>(current: T | null | undefined, incoming: T): T {
  if (!current || current.run_id !== incoming.run_id) return incoming;
  if (incoming.revision < current.revision || incoming.last_sequence < current.last_sequence) return current;
  if (terminal.has(current.state) && incoming.state !== current.state) return current;
  if (incoming.revision === current.revision && incoming.state !== current.state) return current;
  if (incoming.revision === current.revision && incoming.last_sequence === current.last_sequence) return current;
  return incoming;
}

export function reconcileRunList<T extends RunSnapshot>(current: T[], incoming: T[], append: boolean): T[] {
  const existing = new Map(current.map(run => [run.run_id, run]));
  const admitted = incoming.map(run => reconcileRun(existing.get(run.run_id), run));
  if (!append) return admitted;
  const updates = new Map(admitted.map(run => [run.run_id, run]));
  return [...current.map(run => updates.get(run.run_id) ?? run), ...admitted.filter(run => !existing.has(run.run_id))];
}
