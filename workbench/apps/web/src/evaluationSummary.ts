export interface EvaluationArm {
  id: string;
  planned: number;
  attempted: number;
  completed: number;
  failed: number;
  ac_at_1: number;
  ac_at_3: number;
  ac_at_5: number;
  avg_at_5: number;
  nominal_usd: number;
  median_seconds: number;
}

export interface RecordedEvaluation {
  available: true;
  model_id: string;
  cohort_size: number;
  completed_at: string;
  arms: EvaluationArm[];
  usage: {
    generations: number;
    input_tokens: number;
    output_and_thinking_tokens: number;
    nominal_usd: number;
    unresolved_calls: number;
  };
  limitations: string[];
  provenance: { path: string; sha256: string }[];
}

export type EvaluationSummary = RecordedEvaluation | { available: false; reason: string };

const nonnegative = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const count = (value: unknown): value is number => nonnegative(value) && Number.isInteger(value);
const fraction = (value: unknown): value is number => nonnegative(value) && value <= 1;
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string');

/** Reject malformed displays; this does not independently validate diagnostic correctness. */
export function parseEvaluationSummary(value: unknown): EvaluationSummary {
  if (!value || typeof value !== 'object') throw new Error('Invalid recorded evaluation summary.');
  const result = value as Record<string, unknown>;
  if (result.available === false && typeof result.reason === 'string') return { available: false, reason: result.reason };
  if (result.available !== true || typeof result.model_id !== 'string' || !count(result.cohort_size) || result.cohort_size === 0 || typeof result.completed_at !== 'string' || !Number.isFinite(Date.parse(result.completed_at))) throw new Error('Invalid recorded evaluation identity.');
  if (!Array.isArray(result.arms) || result.arms.length === 0) throw new Error('Recorded evaluation methods are missing.');
  const seen = new Set<string>();
  for (const arm of result.arms) {
    if (!arm || typeof arm !== 'object' || typeof arm.id !== 'string' || !arm.id || seen.has(arm.id)) throw new Error('Invalid recorded evaluation method.');
    seen.add(arm.id);
    if (![arm.planned, arm.attempted, arm.completed, arm.failed].every(count) || arm.planned !== result.cohort_size || arm.attempted > arm.planned || arm.completed + arm.failed !== arm.attempted) throw new Error('Inconsistent recorded evaluation case counts.');
    if (![arm.ac_at_1, arm.ac_at_3, arm.ac_at_5, arm.avg_at_5].every(fraction) || arm.ac_at_1 > arm.ac_at_3 || arm.ac_at_3 > arm.ac_at_5 || arm.avg_at_5 < arm.ac_at_1 || arm.avg_at_5 > arm.ac_at_5 || arm.ac_at_5 * arm.planned > arm.completed + 1e-8) throw new Error('Invalid recorded localization metrics.');
    if (!nonnegative(arm.nominal_usd) || !nonnegative(arm.median_seconds)) throw new Error('Invalid recorded resource units.');
  }
  const usage = result.usage as RecordedEvaluation['usage'] | undefined;
  if (!usage || ![usage.generations, usage.input_tokens, usage.output_and_thinking_tokens, usage.unresolved_calls].every(count) || !nonnegative(usage.nominal_usd)) throw new Error('Invalid recorded usage summary.');
  if (!strings(result.limitations) || !Array.isArray(result.provenance) || !result.provenance.every(item => item && typeof item.path === 'string' && typeof item.sha256 === 'string' && /^[0-9a-f]{64}$/i.test(item.sha256))) throw new Error('Invalid recorded evaluation provenance.');
  return result as unknown as RecordedEvaluation;
}

/** Explicit English numeric formatting keeps decimal and cost units unambiguous in either UI language. */
export const percentage = (value: number) => `${(value * 100).toFixed(2)}%`;
export const nominalUsd = (value: number | undefined) => value !== undefined && nonnegative(value) ? `US$${value.toFixed(6)}` : null;
export const seconds = (value: number) => `${value.toFixed(2)} s`;
