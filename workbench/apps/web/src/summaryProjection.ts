import type { Incident, Investigation, Run } from './types';

export type RecordedModel = { kind: 'recorded'; modelId: string } | { kind: 'no_model' | 'unknown' | 'no_run'; modelId: null };
export function recordedModel(run: Run | null): RecordedModel {
  if (!run) return { kind: 'no_run', modelId: null };
  if (run.config.resolved_model_id) return { kind: 'recorded', modelId: run.config.resolved_model_id };
  if (['offline_inspection', 'synthetic_readonly'].includes(run.config.policy_id)) return { kind: 'no_model', modelId: null };
  return { kind: 'unknown', modelId: null };
}

/** This projection summarizes saved data; it does not infer business impact or a root cause. */
export function investigationSummary(investigation: Investigation, incident: Incident, run: Run) {
  if (investigation.incident_id !== incident.incident_id || run.investigation_id !== investigation.investigation_id || run.incident_id !== incident.incident_id) throw new Error('The summary records do not belong to the same investigation and incident.');
  return {
    investigation_id: investigation.investigation_id, incident_id: incident.incident_id,
    system_id: incident.system_id, time_window: incident.time_window, service_ids: incident.service_ids,
    state: run.state, mode: run.mode, run_id: run.run_id, question: run.question,
    data_revision: run.config.data_revision, updated_at: run.updated_at,
    model: recordedModel(run), policy_id: run.config.policy_id,
    answer_status: run.answer?.status ?? null,
    observations: run.answer?.observations ?? [], candidates: run.answer?.candidates ?? [],
    unknowns: run.answer?.unknowns ?? [], next_checks: run.answer?.next_checks ?? [],
    modalities: incident.modalities, coverage_warnings: incident.coverage_warnings,
    semantic_support_status: run.answer?.semantic_support_status ?? 'not_assessed',
  };
}
