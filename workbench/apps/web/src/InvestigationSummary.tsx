import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { investigationSummary } from './summaryProjection';
import type { TranslationKey } from './i18n';
import type { Incident, Investigation, Run } from './types';

type T = (key: TranslationKey) => string;
const time = (value: string) => value.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC');
function References({ ids, open, t }: { ids: string[]; open: (id: string) => void; t: T }) {
  return ids.length > 0 ? <div className="evidence-links">{[...new Set(ids)].map(value => <button type="button" className="evidence-link" key={value} onClick={() => open(value)} title={`${t('inspect')}: ${value}`}><span aria-hidden="true">↗</span><code>{value}</code></button>)}</div> : <span className="small muted">{t('noReferences')}</span>;
}

export default function InvestigationSummary({ investigation, incident, run, t, openEvidence }: { investigation: Investigation; incident: Incident; run: Run; t: T; openEvidence: (id: string) => void }) {
  let summary;
  try { summary = investigationSummary(investigation, incident, run); }
  catch (cause) { return <Alert type="error">{cause instanceof Error ? cause.message : String(cause)}</Alert>; }
  return <>
    <section className="panel summary-overview"><div className="panel-heading"><h2>{t('summary')}</h2><span className="tag">{t('summaryRecorded')}</span></div><div className="panel-content">
      <p className="small muted">{t('summaryBoundary')}</p>
      <dl className="facts summary-facts"><div className="fact"><dt>{t('incident')}</dt><dd><code>{summary.incident_id}</code></dd></div><div className="fact"><dt>{t('summarySystem')}</dt><dd>{summary.system_id}</dd></div><div className="fact"><dt>{t('summaryRunState')}</dt><dd><StatusIndicator type={summary.state === 'COMPLETED' ? 'success' : ['FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED'].includes(summary.state) ? 'error' : ['CANCELLED', 'CANCEL_REQUESTED'].includes(summary.state) ? 'stopped' : 'in-progress'}>{summary.state}</StatusIndicator></dd></div><div className="fact"><dt>{t('window')}</dt><dd>{time(summary.time_window.start)} → {time(summary.time_window.end)}</dd></div><div className="fact"><dt>{t('revision')}</dt><dd><code>{summary.data_revision}</code></dd></div><div className="fact"><dt>{t('updated')}</dt><dd>{time(summary.updated_at)}</dd></div></dl>
      <h3 className="summary-section-label">{t('services')}</h3><div className="summary-service-tags">{summary.service_ids.map(service => <code key={service}>{service}</code>)}</div>
      <h3 className="summary-section-label">{t('summaryCoverage')}</h3><div className="summary-coverage">{(['metrics', 'logs', 'traces'] as const).map(modality => <div key={modality}><span>{t(modality)}</span><strong>{t(summary.modalities[modality].availability)}</strong><span>{summary.modalities[modality].record_count === null ? t('unknown') : `${summary.modalities[modality].record_count.toLocaleString('en-US')} ${t('records')}`}</span></div>)}</div>
      {summary.coverage_warnings.length > 0 && <ul className="plain-list source-text summary-coverage-warnings">{summary.coverage_warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      <div className="summary-original-question"><h3>{t('originalQuestion')}</h3><p className="source-text">{summary.question}</p></div>
    </div></section>
    <section className="panel"><div className="panel-heading"><h2>{t('summarySavedAnalysis')}</h2><span className="small muted">{t('summarySelectedRunOnly')}</span></div><div className="panel-content">
      {!run.answer ? <div className="summary-no-answer"><h3>{t('noAnswer')}</h3><p>{t('noAnswerText')}</p>{run.termination_code && <code>{run.termination_code}</code>}</div> : <>
        <p className="summary-answer-boundary">{t(run.config.policy_id === 'offline_inspection' ? 'inspectionNote' : 'rankingNote')}</p>
        {summary.answer_status === 'insufficient_evidence' && <div className="summary-answer-kind"><strong>{t(run.config.policy_id === 'offline_inspection' ? 'inspectionTitle' : 'insufficient')}</strong><p>{t(run.config.policy_id === 'offline_inspection' ? 'inspectionText' : 'insufficientText')}</p></div>}
        {summary.candidates.length > 0 && <><h3>{t('summaryCandidateHypotheses')}</h3><ol className="summary-candidates">{summary.candidates.map(candidate => <li key={candidate.service_id}><span className="rank">{candidate.rank}</span><div><strong><code>{candidate.service_id}</code></strong><p className="source-text">{candidate.hypothesis}</p><References ids={candidate.supporting_evidence_ids} open={openEvidence} t={t} /></div></li>)}</ol></>}
        <h3 className="summary-section-label">{t('observations')}</h3>{summary.observations.length > 0 ? summary.observations.map((observation, index) => <div className="observation" key={index}><p className="source-text">{observation.text}</p><References ids={observation.evidence_ids} open={openEvidence} t={t} /></div>) : <p className="small muted">{t('summaryNoObservations')}</p>}
        <div className="summary-unknowns"><h3>{t('unknowns')}</h3>{summary.unknowns.length > 0 ? <ul className="plain-list source-text">{summary.unknowns.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="small muted">{t('summaryNoUnknownsRecorded')}</p>}</div>
        {summary.next_checks.length > 0 && <div className="summary-unknowns"><h3>{t('checks')}</h3><ul className="plain-list source-text">{summary.next_checks.map((item, index) => <li key={index}>{item}</li>)}</ul><p className="small muted">{t('nextChecksNote')}</p></div>}
      </>}
    </div></section>
  </>;
}
