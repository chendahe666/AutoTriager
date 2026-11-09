// Explicit zero-model policies. Never infer a provider grant from UI state.
export function inspectionPolicy(mode: string | undefined, allowed: string[]): string | undefined {
  const policy = mode === 'fixture' ? 'synthetic_readonly' : mode === 'offline_replay' ? 'offline_inspection' : undefined;
  return policy && allowed.includes(policy) ? policy : undefined;
}
