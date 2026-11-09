export type ArmId = 'B0' | 'B1' | 'B2';

export interface ComparisonArm {
  id: ArmId;
  label: string;
  completed: number;
  top1_correct: number;
  top3_correct: number;
  both_services_top1?: number;
  run_mode?: string;
}

export interface CasePrediction {
  arm_id: ArmId;
  status: 'completed' | 'failed' | 'missing';
  ranking: string[];
  reference_rank?: number | null;
  observation?: string;
}

export interface ComparisonCase {
  id: string;
  system: string;
  signal: string;
  evidence: { source: string; detail: string }[];
  truth: string;
  fault?: string;
  review_note?: string;
  predictions: CasePrediction[];
}

export interface SymptomPair {
  id: string;
  symptom: string;
  match_reason: string;
  distance?: number;
  cases: [ComparisonCase, ComparisonCase];
}

export interface SymptomComparisonRecord {
  schema_version: 1;
  dataset_label: string;
  dataset_url: string;
  cohort_size: number;
  methodology: string;
  limitations: string[];
  source_run_date?: string;
  selection_date?: string;
  featured_pair_id?: string;
  arms: ComparisonArm[];
  pairs: SymptomPair[];
}

const armIds = ['B0', 'B1', 'B2'] as const;
const predictionStatuses = ['completed', 'failed', 'missing'];
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
const optionalText = (value: unknown) => value === undefined || nonempty(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const count = (value: unknown): value is number => finite(value) && Number.isInteger(value) && value >= 0;
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid comparison record.');
  return value as Record<string, unknown>;
};

export function parseSymptomComparison(value: unknown): SymptomComparisonRecord {
  const record = object(value);
  const { cohort_size: cohortSize, dataset_url: datasetUrl } = record;
  if (record.schema_version !== 1 || !count(cohortSize) || cohortSize < 2 || !nonempty(record.dataset_label)
    || !nonempty(record.methodology) || !nonempty(datasetUrl) || new URL(datasetUrl).protocol !== 'https:'
    || !Array.isArray(record.limitations) || !record.limitations.every(nonempty)
    || !optionalText(record.source_run_date) || !optionalText(record.selection_date)) {
    throw new Error('Invalid comparison identity.');
  }

  const arms = Array.isArray(record.arms) ? record.arms.map(object) : [];
  if (arms.length !== armIds.length || !armIds.every(id => arms.some(arm => arm.id === id))) throw new Error('Invalid comparison methods.');
  for (const arm of arms) {
    const { completed, top1_correct: top1, top3_correct: top3, both_services_top1: both } = arm;
    if (!nonempty(arm.label) || !optionalText(arm.run_mode) || !count(completed) || !count(top1) || !count(top3)
      || top1 > top3 || top3 > completed || completed > cohortSize || (both !== undefined && (!count(both) || both * 2 > top1))) {
      throw new Error('Inconsistent comparison counts.');
    }
  }

  if (!Array.isArray(record.pairs) || !record.pairs.length) throw new Error('Missing comparison examples.');
  const caseIds = new Set<string>();
  const pairIds = new Set<string>();
  for (const item of record.pairs) {
    const pair = object(item);
    if (!nonempty(pair.id) || pairIds.has(pair.id) || !nonempty(pair.symptom) || !nonempty(pair.match_reason)
      || (pair.distance !== undefined && (!finite(pair.distance) || pair.distance < 0))
      || !Array.isArray(pair.cases) || pair.cases.length !== 2) throw new Error('Invalid symptom pair.');
    pairIds.add(pair.id);
    const [left, right] = pair.cases.map(object);
    if (left.truth === right.truth) throw new Error('A comparison pair must have two different labeled root services.');
    for (const entry of [left, right]) {
      if (!nonempty(entry.id) || caseIds.has(entry.id) || !nonempty(entry.system) || !nonempty(entry.signal)
        || !nonempty(entry.truth) || !optionalText(entry.fault) || !optionalText(entry.review_note) || !Array.isArray(entry.evidence)) throw new Error('Invalid case evidence.');
      caseIds.add(entry.id);
      for (const raw of entry.evidence) {
        const evidence = object(raw);
        if (!nonempty(evidence.source) || !nonempty(evidence.detail)) throw new Error('Invalid evidence item.');
      }
      const predictions = Array.isArray(entry.predictions) ? entry.predictions.map(object) : [];
      if (predictions.length !== armIds.length || !armIds.every(id => predictions.some(prediction => prediction.arm_id === id))) {
        throw new Error('Missing case predictions.');
      }
      for (const prediction of predictions) {
        const rank = prediction.reference_rank;
        if (!nonempty(prediction.status) || !predictionStatuses.includes(prediction.status)
          || !Array.isArray(prediction.ranking) || !prediction.ranking.every(nonempty)
          || new Set(prediction.ranking).size !== prediction.ranking.length
          || (prediction.status !== 'completed' && prediction.ranking.length > 0)
          || !optionalText(prediction.observation)) throw new Error('Invalid case prediction.');
        if (rank !== undefined && rank !== null && (!count(rank) || rank < 1 || prediction.status !== 'completed'
          || (rank <= prediction.ranking.length && prediction.ranking[rank - 1] !== entry.truth)
          || (prediction.ranking.includes(entry.truth) && prediction.ranking.indexOf(entry.truth) + 1 !== rank))) {
          throw new Error('Invalid reference rank.');
        }
      }
    }
  }
  if (caseIds.size > cohortSize) throw new Error('Comparison displays more cases than the cohort contains.');
  if (record.featured_pair_id !== undefined && (!nonempty(record.featured_pair_id) || !pairIds.has(record.featured_pair_id))) {
    throw new Error('Invalid featured case pair.');
  }
  return record as unknown as SymptomComparisonRecord;
}
