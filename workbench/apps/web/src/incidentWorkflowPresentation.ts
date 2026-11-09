// Presentation policy: verification has a separate read-only endpoint. Its
// observations must not be presented as application recovery or a repair.
export function workflowCheckAction(check: { check_id: string; verification_only: boolean }): 'verify' | `checks/${string}/run` {
  return check.verification_only ? 'verify' : `checks/${encodeURIComponent(check.check_id)}/run`;
}

export function groupWorkflowEvidence<T extends { evidence_id: string }>(
  evidence: T[],
  checks: { status: string; evidence_ids: string[] }[],
  recoveryEvidenceIds: string[],
): { checked: T[]; initial: T[] } {
  const returnedByChecks = new Set([
    ...checks.filter(check => check.status !== 'proposed').flatMap(check => check.evidence_ids),
    ...recoveryEvidenceIds,
  ]);
  // Preserve the exact returned records and their order. Missing references do
  // not become invented evidence; proposed checks do not claim new evidence.
  return {
    checked: evidence.filter(record => returnedByChecks.has(record.evidence_id)),
    initial: evidence.filter(record => !returnedByChecks.has(record.evidence_id)),
  };
}

export function groupWorkflowEvents<T extends { kind: string }>(events: T[]): { current: T[]; earlier: T[] } {
  let start = -1;
  for (let index = 0; index < events.length; index++) if (events[index].kind === 'requested') start = index;
  // A request may contain several model turns and checks. Keep that full
  // execution group together; preserve all preceding activity for inspection.
  return start < 0 ? { current: events, earlier: [] } : { current: events.slice(start), earlier: events.slice(0, start) };
}
